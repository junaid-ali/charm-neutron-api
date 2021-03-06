# Copyright 2016 Canonical Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#  http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from collections import OrderedDict
from copy import deepcopy
from functools import partial
import os
import shutil
import subprocess
import glob
from base64 import b64encode
from charmhelpers.contrib.openstack import context, templating
from charmhelpers.contrib.openstack.neutron import (
    neutron_plugin_attribute,
)

from charmhelpers.contrib.openstack.utils import (
    os_release,
    get_os_codename_install_source,
    git_clone_and_install,
    git_default_repos,
    git_generate_systemd_init_files,
    git_install_requested,
    git_pip_venv_dir,
    git_src_dir,
    git_yaml_value,
    configure_installation_source,
    incomplete_relation_data,
    is_unit_paused_set,
    make_assess_status_func,
    pause_unit,
    resume_unit,
    os_application_version_set,
)

from charmhelpers.contrib.python.packages import (
    pip_install,
)

from charmhelpers.core.hookenv import (
    charm_dir,
    config,
    log,
    relation_ids,
)

from charmhelpers.fetch import (
    apt_update,
    apt_install,
    apt_upgrade,
    add_source
)

from charmhelpers.core.host import (
    lsb_release,
    adduser,
    add_group,
    add_user_to_group,
    mkdir,
    service_stop,
    service_start,
    service_restart,
    write_file,
)

from charmhelpers.contrib.hahelpers.cluster import (
    get_hacluster_config,
)


from charmhelpers.core.templating import render
from charmhelpers.contrib.hahelpers.cluster import is_elected_leader

import neutron_api_context

TEMPLATES = 'templates/'

CLUSTER_RES = 'grp_neutron_vips'

# removed from original: charm-helper-sh
BASE_PACKAGES = [
    'apache2',
    'haproxy',
    'python-keystoneclient',
    'python-mysqldb',
    'python-psycopg2',
    'python-six',
    'uuid',
]

KILO_PACKAGES = [
    'python-neutron-lbaas',
    'python-neutron-fwaas',
    'python-neutron-vpnaas',
]

VERSION_PACKAGE = 'neutron-common'

BASE_GIT_PACKAGES = [
    'libffi-dev',
    'libmysqlclient-dev',
    'libssl-dev',
    'libxml2-dev',
    'libxslt1-dev',
    'libyaml-dev',
    'openstack-pkg-tools',
    'python-dev',
    'python-neutronclient',  # required for get_neutron_client() import
    'python-pip',
    'python-setuptools',
    'zlib1g-dev',
]

# ubuntu packages that should not be installed when deploying from git
GIT_PACKAGE_BLACKLIST = [
    'neutron-server',
    'neutron-plugin-ml2',
    'python-keystoneclient',
    'python-six',
]

GIT_PACKAGE_BLACKLIST_KILO = [
    'python-neutron-lbaas',
    'python-neutron-fwaas',
    'python-neutron-vpnaas',
]

BASE_SERVICES = [
    'neutron-server'
]
API_PORTS = {
    'neutron-server': 9696,
}

NEUTRON_CONF_DIR = "/etc/neutron"

NEUTRON_CONF = '%s/neutron.conf' % NEUTRON_CONF_DIR
NEUTRON_LBAAS_CONF = '%s/neutron_lbaas.conf' % NEUTRON_CONF_DIR
NEUTRON_VPNAAS_CONF = '%s/neutron_vpnaas.conf' % NEUTRON_CONF_DIR
HAPROXY_CONF = '/etc/haproxy/haproxy.cfg'
APACHE_CONF = '/etc/apache2/sites-available/openstack_https_frontend'
APACHE_24_CONF = '/etc/apache2/sites-available/openstack_https_frontend.conf'
NEUTRON_DEFAULT = '/etc/default/neutron-server'
CA_CERT_PATH = '/usr/local/share/ca-certificates/keystone_juju_ca_cert.crt'

BASE_RESOURCE_MAP = OrderedDict([
    (NEUTRON_CONF, {
        'services': ['neutron-server'],
        'contexts': [context.AMQPContext(ssl_dir=NEUTRON_CONF_DIR),
                     context.SharedDBContext(
                         user=config('database-user'),
                         database=config('database'),
                         ssl_dir=NEUTRON_CONF_DIR),
                     context.PostgresqlDBContext(database=config('database')),
                     neutron_api_context.IdentityServiceContext(
                         service='neutron',
                         service_user='neutron'),
                     context.OSConfigFlagContext(),
                     neutron_api_context.NeutronCCContext(),
                     context.SyslogContext(),
                     context.ZeroMQContext(),
                     context.NotificationDriverContext(),
                     context.BindHostContext(),
                     context.WorkerConfigContext(),
                     context.InternalEndpointContext()],
    }),
    (NEUTRON_DEFAULT, {
        'services': ['neutron-server'],
        'contexts': [neutron_api_context.NeutronCCContext()],
    }),
    (APACHE_CONF, {
        'contexts': [neutron_api_context.ApacheSSLContext()],
        'services': ['apache2'],
    }),
    (APACHE_24_CONF, {
        'contexts': [neutron_api_context.ApacheSSLContext()],
        'services': ['apache2'],
    }),
    (HAPROXY_CONF, {
        'contexts': [context.HAProxyContext(singlenode_mode=True),
                     neutron_api_context.HAProxyContext()],
        'services': ['haproxy'],
    }),
])

# The interface is said to be satisfied if anyone of the interfaces in the
# list has a complete context.
REQUIRED_INTERFACES = {
    'database': ['shared-db', 'pgsql-db'],
    'messaging': ['amqp', 'zeromq-configuration'],
    'identity': ['identity-service'],
}

LIBERTY_RESOURCE_MAP = OrderedDict([
    (NEUTRON_LBAAS_CONF, {
        'services': ['neutron-server'],
        'contexts': [],
    }),
    (NEUTRON_VPNAAS_CONF, {
        'services': ['neutron-server'],
        'contexts': [],
    }),
])


def api_port(service):
    return API_PORTS[service]


def additional_install_locations(plugin, source):
    '''
    Add any required additional package locations for the charm, based
    on the Neutron plugin being used. This will also force an immediate
    package upgrade.
    '''
    release = get_os_codename_install_source(source)
    if plugin == 'Calico':
        if config('calico-origin'):
            calico_source = config('calico-origin')
        elif release in ('icehouse', 'juno', 'kilo'):
            # Prior to the Liberty release, Calico's Nova and Neutron changes
            # were not fully upstreamed, so we need to point to a
            # release-specific PPA that includes Calico-specific Nova and
            # Neutron packages.
            calico_source = 'ppa:project-calico/%s' % release
        else:
            # From Liberty onwards, we can point to a PPA that does not include
            # any patched OpenStack packages, and hence is independent of the
            # OpenStack release.
            calico_source = 'ppa:project-calico/calico-1.4'

        add_source(calico_source)

    elif plugin == 'midonet':
        midonet_origin = config('midonet-origin')
        release_num = midonet_origin.split('-')[1]

        if midonet_origin.startswith('mem'):
            with open(os.path.join(charm_dir(),
                                   'files/midokura.key')) as midokura_gpg_key:
                priv_gpg_key = midokura_gpg_key.read()
            mem_username = config('mem-username')
            mem_password = config('mem-password')
            if release in ('juno', 'kilo', 'liberty'):
                add_source(
                    'deb http://%s:%s@apt.midokura.com/openstack/%s/stable '
                    'trusty main' % (mem_username, mem_password, release),
                    key=priv_gpg_key)
            add_source('http://%s:%s@apt.midokura.com/midonet/v%s/stable '
                       'main' % (mem_username, mem_password, release_num),
                       key=priv_gpg_key)
        else:
            with open(os.path.join(charm_dir(),
                                   'files/midonet.key')) as midonet_gpg_key:
                pub_gpg_key = midonet_gpg_key.read()
            if release in ('juno', 'kilo', 'liberty'):
                add_source(
                    'deb http://repo.midonet.org/openstack-%s stable main' %
                    release, key=pub_gpg_key)

            add_source('deb http://repo.midonet.org/midonet/v%s stable main' %
                       release_num, key=pub_gpg_key)

        apt_update(fatal=True)
        apt_upgrade(fatal=True)


def force_etcd_restart():
    '''
    If etcd has been reconfigured we need to force it to fully restart.
    This is necessary because etcd has some config flags that it ignores
    after the first time it starts, so we need to make it forget them.
    '''
    service_stop('etcd')
    for directory in glob.glob('/var/lib/etcd/*'):
        shutil.rmtree(directory)
    if not is_unit_paused_set():
        service_start('etcd')


def manage_plugin():
    return config('manage-neutron-plugin-legacy-mode')


def determine_packages(source=None):
    # currently all packages match service names
    packages = [] + BASE_PACKAGES

    for v in resource_map().values():
        packages.extend(v['services'])
        if manage_plugin():
            pkgs = neutron_plugin_attribute(config('neutron-plugin'),
                                            'server_packages',
                                            'neutron')
            packages.extend(pkgs)

    release = get_os_codename_install_source(source)

    if release >= 'kilo':
        packages.extend(KILO_PACKAGES)

    if release == 'kilo' or release >= 'mitaka':
        packages.append('python-networking-hyperv')

    if config('neutron-plugin') == 'vsp':
        nuage_pkgs = config('nuage-packages').split()
        packages += nuage_pkgs

    if git_install_requested():
        packages.extend(BASE_GIT_PACKAGES)
        # don't include packages that will be installed from git
        packages = list(set(packages))
        for p in GIT_PACKAGE_BLACKLIST:
            if p in packages:
                packages.remove(p)
        if release >= 'kilo':
            for p in GIT_PACKAGE_BLACKLIST_KILO:
                packages.remove(p)

    return list(set(packages))


def determine_ports():
    '''Assemble a list of API ports for services we are managing'''
    ports = []
    for services in restart_map().values():
        for service in services:
            try:
                ports.append(API_PORTS[service])
            except KeyError:
                pass
    return list(set(ports))


def resource_map(release=None):
    '''
    Dynamically generate a map of resources that will be managed for a single
    hook execution.
    '''
    release = release or os_release('neutron-common')

    resource_map = deepcopy(BASE_RESOURCE_MAP)
    if release >= 'liberty':
        resource_map.update(LIBERTY_RESOURCE_MAP)

    if os.path.exists('/etc/apache2/conf-available'):
        resource_map.pop(APACHE_CONF)
    else:
        resource_map.pop(APACHE_24_CONF)

    if manage_plugin():
        # add neutron plugin requirements. nova-c-c only needs the
        # neutron-server associated with configs, not the plugin agent.
        plugin = config('neutron-plugin')
        conf = neutron_plugin_attribute(plugin, 'config', 'neutron')
        ctxts = (neutron_plugin_attribute(plugin, 'contexts', 'neutron') or
                 [])
        services = neutron_plugin_attribute(plugin, 'server_services',
                                            'neutron')
        resource_map[conf] = {}
        resource_map[conf]['services'] = services
        resource_map[conf]['contexts'] = ctxts
        resource_map[conf]['contexts'].append(
            neutron_api_context.NeutronCCContext())

        # update for postgres
        resource_map[conf]['contexts'].append(
            context.PostgresqlDBContext(database=config('database')))

    else:
        resource_map[NEUTRON_CONF]['contexts'].append(
            neutron_api_context.NeutronApiSDNContext()
        )
        resource_map[NEUTRON_DEFAULT]['contexts'] = \
            [neutron_api_context.NeutronApiSDNConfigFileContext()]
    return resource_map


def register_configs(release=None):
    release = release or os_release('neutron-common')
    configs = templating.OSConfigRenderer(templates_dir=TEMPLATES,
                                          openstack_release=release)
    for cfg, rscs in resource_map().iteritems():
        configs.register(cfg, rscs['contexts'])
    return configs


def restart_map():
    return OrderedDict([(cfg, v['services'])
                        for cfg, v in resource_map().iteritems()
                        if v['services']])


def services():
    ''' Returns a list of services associate with this charm '''
    _services = []
    for v in restart_map().values():
        _services = _services + v
    return list(set(_services))


def keystone_ca_cert_b64():
    '''Returns the local Keystone-provided CA cert if it exists, or None.'''
    if not os.path.isfile(CA_CERT_PATH):
        return None
    with open(CA_CERT_PATH) as _in:
        return b64encode(_in.read())


def do_openstack_upgrade(configs):
    """
    Perform an upgrade.  Takes care of upgrading packages, rewriting
    configs, database migrations and potentially any other post-upgrade
    actions.

    :param configs: The charms main OSConfigRenderer object.
    """
    cur_os_rel = os_release('neutron-common')
    new_src = config('openstack-origin')
    new_os_rel = get_os_codename_install_source(new_src)

    log('Performing OpenStack upgrade to %s.' % (new_os_rel))

    configure_installation_source(new_src)
    dpkg_opts = [
        '--option', 'Dpkg::Options::=--force-confnew',
        '--option', 'Dpkg::Options::=--force-confdef',
    ]
    apt_update(fatal=True)
    apt_upgrade(options=dpkg_opts, fatal=True, dist=True)
    pkgs = determine_packages(new_os_rel)
    # Sort packages just to make unit tests easier
    pkgs.sort()
    apt_install(packages=pkgs,
                options=dpkg_opts,
                fatal=True)

    # set CONFIGS to load templates from new release
    configs.set_release(openstack_release=new_os_rel)
    # Before kilo it's nova-cloud-controllers job
    if is_elected_leader(CLUSTER_RES):
        # Stamping seems broken and unnecessary in liberty (Bug #1536675)
        if os_release('neutron-common') < 'liberty':
            stamp_neutron_database(cur_os_rel)
        migrate_neutron_database()


def stamp_neutron_database(release):
    '''Stamp the database with the current release before upgrade.'''
    log('Stamping the neutron database with release %s.' % release)
    plugin = config('neutron-plugin')
    cmd = ['neutron-db-manage',
           '--config-file', NEUTRON_CONF,
           '--config-file', neutron_plugin_attribute(plugin,
                                                     'config',
                                                     'neutron'),
           'stamp',
           release]
    subprocess.check_output(cmd)


def nuage_vsp_juno_neutron_migration():
    log('Nuage VSP with Juno Relase')
    nuage_migration_db_path = '/usr/lib/python2.7/dist-packages/'\
                              'neutron/db/migration/nuage'
    nuage_migrate_hybrid_file_path = os.path.join(
        nuage_migration_db_path, 'migrate_hybrid_juno.py')
    nuage_config_file = neutron_plugin_attribute(config('neutron-plugin'),
                                                 'config', 'neutron')
    if os.path.exists(nuage_migration_db_path):
        if os.path.exists(nuage_migrate_hybrid_file_path):
            if os.path.exists(nuage_config_file):
                log('Running Migartion Script for Juno Release')
                cmd = 'sudo python ' + nuage_migrate_hybrid_file_path + \
                      ' --config-file ' + nuage_config_file + \
                      ' --config-file ' + NEUTRON_CONF
                log(cmd)
                subprocess.check_output(cmd, shell=True)
            else:
                e = nuage_config_file+' doesnot exist'
                log(e)
                raise Exception(e)
        else:
            e = nuage_migrate_hybrid_file_path+' doesnot exists'
            log(e)
            raise Exception(e)
    else:
        e = nuage_migration_db_path+' doesnot exists'
        log(e)
        raise Exception(e)


def migrate_neutron_database():
    '''Initializes a new database or upgrades an existing database.'''
    log('Migrating the neutron database.')
    if(os_release('neutron-server') == 'juno' and
       config('neutron-plugin') == 'vsp'):
        nuage_vsp_juno_neutron_migration()
    else:
        plugin = config('neutron-plugin')
        cmd = ['neutron-db-manage',
               '--config-file', NEUTRON_CONF,
               '--config-file', neutron_plugin_attribute(plugin,
                                                         'config',
                                                         'neutron'),
               'upgrade',
               'head']
        subprocess.check_output(cmd)


def get_topics():
    return ['q-l3-plugin',
            'q-firewall-plugin',
            'n-lbaas-plugin',
            'ipsec_driver',
            'q-metering-plugin',
            'q-plugin',
            'neutron']


def setup_ipv6():
    ubuntu_rel = lsb_release()['DISTRIB_CODENAME'].lower()
    if ubuntu_rel < "trusty":
        raise Exception("IPv6 is not supported in the charms for Ubuntu "
                        "versions less than Trusty 14.04")

    # Need haproxy >= 1.5.3 for ipv6 so for Trusty if we are <= Kilo we need to
    # use trusty-backports otherwise we can use the UCA.
    if ubuntu_rel == 'trusty' and os_release('neutron-server') < 'liberty':
        add_source('deb http://archive.ubuntu.com/ubuntu trusty-backports '
                   'main')
        apt_update()
        apt_install('haproxy/trusty-backports', fatal=True)


def get_neutron_client():
    ''' Return a neutron client if possible '''
    env = neutron_api_context.IdentityServiceContext()()
    if not env:
        log('Unable to check resources at this time')
        return

    auth_url = '%(auth_protocol)s://%(auth_host)s:%(auth_port)s/v2.0' % env
    # Late import to avoid install hook failures when pkg hasnt been installed
    from neutronclient.v2_0 import client
    neutron_client = client.Client(username=env['admin_user'],
                                   password=env['admin_password'],
                                   tenant_name=env['admin_tenant_name'],
                                   auth_url=auth_url,
                                   region_name=env['region'])
    return neutron_client


def router_feature_present(feature):
    ''' Check For dvr enabled routers '''
    neutron_client = get_neutron_client()
    for router in neutron_client.list_routers()['routers']:
        if router.get(feature, False):
            return True
    return False

l3ha_router_present = partial(router_feature_present, feature='ha')

dvr_router_present = partial(router_feature_present, feature='distributed')


def neutron_ready():
    ''' Check if neutron is ready by running arbitrary query'''
    neutron_client = get_neutron_client()
    if not neutron_client:
        log('No neutron client, neutron not ready')
        return False
    try:
        neutron_client.list_routers()
        log('neutron client ready')
        return True
    except:
        log('neutron query failed, neutron not ready ')
        return False


def git_install(projects_yaml):
    """Perform setup, and install git repos specified in yaml parameter."""
    if git_install_requested():
        git_pre_install()
        projects_yaml = git_default_repos(projects_yaml)
        git_clone_and_install(projects_yaml, core_project='neutron')
        git_post_install(projects_yaml)


def git_pre_install():
    """Perform pre-install setup."""
    dirs = [
        '/var/lib/neutron',
        '/var/lib/neutron/lock',
        '/var/log/neutron',
    ]

    logs = [
        '/var/log/neutron/server.log',
    ]

    adduser('neutron', shell='/bin/bash', system_user=True)
    add_group('neutron', system_group=True)
    add_user_to_group('neutron', 'neutron')

    for d in dirs:
        mkdir(d, owner='neutron', group='neutron', perms=0755, force=False)

    for l in logs:
        write_file(l, '', owner='neutron', group='neutron', perms=0600)


def git_post_install(projects_yaml):
    """Perform post-install setup."""
    http_proxy = git_yaml_value(projects_yaml, 'http_proxy')
    if http_proxy:
        pip_install('mysql-python', proxy=http_proxy,
                    venv=git_pip_venv_dir(projects_yaml))
    else:
        pip_install('mysql-python',
                    venv=git_pip_venv_dir(projects_yaml))

    src_etc = os.path.join(git_src_dir(projects_yaml, 'neutron'), 'etc')
    configs = [
        {'src': src_etc,
         'dest': '/etc/neutron'},
        {'src': os.path.join(src_etc, 'neutron/plugins'),
         'dest': '/etc/neutron/plugins'},
        {'src': os.path.join(src_etc, 'neutron/rootwrap.d'),
         'dest': '/etc/neutron/rootwrap.d'},
    ]

    for c in configs:
        if os.path.exists(c['dest']):
            shutil.rmtree(c['dest'])
        shutil.copytree(c['src'], c['dest'])

    # NOTE(coreycb): Need to find better solution than bin symlinks.
    symlinks = [
        {'src': os.path.join(git_pip_venv_dir(projects_yaml),
                             'bin/neutron-rootwrap'),
         'link': '/usr/local/bin/neutron-rootwrap'},
        {'src': os.path.join(git_pip_venv_dir(projects_yaml),
                             'bin/neutron-db-manage'),
         'link': '/usr/local/bin/neutron-db-manage'},
    ]

    for s in symlinks:
        if os.path.lexists(s['link']):
            os.remove(s['link'])
        os.symlink(s['src'], s['link'])

    render('git/neutron_sudoers', '/etc/sudoers.d/neutron_sudoers', {},
           perms=0o440)

    bin_dir = os.path.join(git_pip_venv_dir(projects_yaml), 'bin')
    # Use systemd init units/scripts from ubuntu wily onward
    if lsb_release()['DISTRIB_RELEASE'] >= '15.10':
        templates_dir = os.path.join(charm_dir(), 'templates/git')
        daemon = 'neutron-server'
        neutron_api_context = {
            'daemon_path': os.path.join(bin_dir, daemon),
        }
        template_file = 'git/{}.init.in.template'.format(daemon)
        init_in_file = '{}.init.in'.format(daemon)
        render(template_file, os.path.join(templates_dir, init_in_file),
               neutron_api_context, perms=0o644)
        git_generate_systemd_init_files(templates_dir)
    else:
        neutron_api_context = {
            'service_description': 'Neutron API server',
            'charm_name': 'neutron-api',
            'process_name': 'neutron-server',
            'executable_name': os.path.join(bin_dir, 'neutron-server'),
        }

        render('git/upstart/neutron-server.upstart',
               '/etc/init/neutron-server.conf',
               neutron_api_context, perms=0o644)

    if not is_unit_paused_set():
        service_restart('neutron-server')


def get_optional_interfaces():
    """Return the optional interfaces that should be checked if the relavent
    relations have appeared.
    :returns: {general_interface: [specific_int1, specific_int2, ...], ...}
    """
    optional_interfaces = {}
    if relation_ids('ha'):
        optional_interfaces['ha'] = ['cluster']
    return optional_interfaces


def check_optional_relations(configs):
    """Check that if we have a relation_id for high availability that we can
    get the hacluster config.  If we can't then we are blocked.  This function
    is called from assess_status/set_os_workload_status as the charm_func and
    needs to return either "unknown", "" if there is no problem or the status,
    message if there is a problem.

    :param configs: an OSConfigRender() instance.
    :return 2-tuple: (string, string) = (status, message)
    """
    if relation_ids('ha'):
        try:
            get_hacluster_config()
        except:
            return ('blocked',
                    'hacluster missing configuration: '
                    'vip, vip_iface, vip_cidr')
    # return 'unknown' as the lowest priority to not clobber an existing
    # status.
    return 'unknown', ''


def is_api_ready(configs):
    return (not incomplete_relation_data(configs, REQUIRED_INTERFACES))


def assess_status(configs):
    """Assess status of current unit
    Decides what the state of the unit should be based on the current
    configuration.
    SIDE EFFECT: calls set_os_workload_status(...) which sets the workload
    status of the unit.
    Also calls status_set(...) directly if paused state isn't complete.
    @param configs: a templating.OSConfigRenderer() object
    @returns None - this function is executed for its side-effect
    """
    assess_status_func(configs)()
    os_application_version_set(VERSION_PACKAGE)


def assess_status_func(configs):
    """Helper function to create the function that will assess_status() for
    the unit.
    Uses charmhelpers.contrib.openstack.utils.make_assess_status_func() to
    create the appropriate status function and then returns it.
    Used directly by assess_status() and also for pausing and resuming
    the unit.

    NOTE: REQUIRED_INTERFACES is augmented with the optional interfaces
    depending on the current config before being passed to the
    make_assess_status_func() function.

    NOTE(ajkavanagh) ports are not checked due to race hazards with services
    that don't behave sychronously w.r.t their service scripts.  e.g.
    apache2.

    @param configs: a templating.OSConfigRenderer() object
    @return f() -> None : a function that assesses the unit's workload status
    """
    required_interfaces = REQUIRED_INTERFACES.copy()
    required_interfaces.update(get_optional_interfaces())
    return make_assess_status_func(
        configs, required_interfaces,
        charm_func=check_optional_relations,
        services=services(), ports=None)


def pause_unit_helper(configs):
    """Helper function to pause a unit, and then call assess_status(...) in
    effect, so that the status is correctly updated.
    Uses charmhelpers.contrib.openstack.utils.pause_unit() to do the work.
    @param configs: a templating.OSConfigRenderer() object
    @returns None - this function is executed for its side-effect
    """
    _pause_resume_helper(pause_unit, configs)


def resume_unit_helper(configs):
    """Helper function to resume a unit, and then call assess_status(...) in
    effect, so that the status is correctly updated.
    Uses charmhelpers.contrib.openstack.utils.resume_unit() to do the work.
    @param configs: a templating.OSConfigRenderer() object
    @returns None - this function is executed for its side-effect
    """
    _pause_resume_helper(resume_unit, configs)


def _pause_resume_helper(f, configs):
    """Helper function that uses the make_assess_status_func(...) from
    charmhelpers.contrib.openstack.utils to create an assess_status(...)
    function that can be used with the pause/resume of the unit
    @param f: the function to be used with the assess_status(...) function
    @returns None - this function is executed for its side-effect
    """
    # TODO(ajkavanagh) - ports= has been left off because of the race hazard
    # that exists due to service_start()
    f(assess_status_func(configs),
      services=services(),
      ports=None)
