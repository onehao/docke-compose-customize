from __future__ import absolute_import
from __future__ import unicode_literals

import datetime
import json
import os
import shlex
import signal
import subprocess
import time
from collections import Counter
from collections import namedtuple
from operator import attrgetter

import yaml
from docker import errors

from .. import mock
from compose.cli.command import get_project
from compose.container import Container
from compose.project import OneOffFilter
from tests.integration.testcases import DockerClientTestCase
from tests.integration.testcases import get_links
from tests.integration.testcases import pull_busybox
from tests.integration.testcases import v2_only


ProcessResult = namedtuple('ProcessResult', 'stdout stderr')


BUILD_CACHE_TEXT = 'Using cache'
BUILD_PULL_TEXT = 'Status: Image is up to date for busybox:latest'


def start_process(base_dir, options):
    proc = subprocess.Popen(
        ['docker-compose'] + options,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=base_dir)
    print("Running process: %s" % proc.pid)
    return proc


def wait_on_process(proc, returncode=0):
    stdout, stderr = proc.communicate()
    if proc.returncode != returncode:
        print("Stderr: {}".format(stderr))
        print("Stdout: {}".format(stdout))
        assert proc.returncode == returncode
    return ProcessResult(stdout.decode('utf-8'), stderr.decode('utf-8'))


def wait_on_condition(condition, delay=0.1, timeout=40):
    start_time = time.time()
    while not condition():
        if time.time() - start_time > timeout:
            raise AssertionError("Timeout: %s" % condition)
        time.sleep(delay)


def kill_service(service):
    for container in service.containers():
        container.kill()


class ContainerCountCondition(object):

    def __init__(self, project, expected):
        self.project = project
        self.expected = expected

    def __call__(self):
        return len(self.project.containers()) == self.expected

    def __str__(self):
        return "waiting for counter count == %s" % self.expected


class ContainerStateCondition(object):

    def __init__(self, client, name, status):
        self.client = client
        self.name = name
        self.status = status

    def __call__(self):
        try:
            container = self.client.inspect_container(self.name)
            return container['State']['Status'] == self.status
        except errors.APIError:
            return False

    def __str__(self):
        return "waiting for container to be %s" % self.status


class CLITestCase(DockerClientTestCase):

    def setUp(self):
        super(CLITestCase, self).setUp()
        self.base_dir = 'tests/fixtures/simple-composefile'

    def tearDown(self):
        if self.base_dir:
            self.project.kill()
            self.project.remove_stopped()

            for container in self.project.containers(stopped=True, one_off=OneOffFilter.only):
                container.remove(force=True)

            networks = self.client.networks()
            for n in networks:
                if n['Name'].startswith('{}_'.format(self.project.name)):
                    self.client.remove_network(n['Name'])

        super(CLITestCase, self).tearDown()

    @property
    def project(self):
        # Hack: allow project to be overridden
        if not hasattr(self, '_project'):
            self._project = get_project(self.base_dir)
        return self._project

    def dispatch(self, options, project_options=None, returncode=0):
        project_options = project_options or []
        proc = start_process(self.base_dir, project_options + options)
        return wait_on_process(proc, returncode=returncode)

    def execute(self, container, cmd):
        # Remove once Hijack and CloseNotifier sign a peace treaty
        self.client.close()
        exc = self.client.exec_create(container.id, cmd)
        self.client.exec_start(exc)
        return self.client.exec_inspect(exc)['ExitCode']

    def lookup(self, container, hostname):
        return self.execute(container, ["nslookup", hostname]) == 0

    def test_help(self):
        self.base_dir = 'tests/fixtures/no-composefile'
        result = self.dispatch(['help', 'up'], returncode=0)
        assert 'Usage: up [options] [SERVICE...]' in result.stdout
        # Prevent tearDown from trying to create a project
        self.base_dir = None

    def test_shorthand_host_opt(self):
        self.dispatch(
            ['-H={0}'.format(os.environ.get('DOCKER_HOST', 'unix://')),
             'up', '-d'],
            returncode=0
        )

    def test_config_list_services(self):
        self.base_dir = 'tests/fixtures/v2-full'
        result = self.dispatch(['config', '--services'])
        assert set(result.stdout.rstrip().split('\n')) == {'web', 'other'}

    def test_config_quiet_with_error(self):
        self.base_dir = None
        result = self.dispatch([
            '-f', 'tests/fixtures/invalid-composefile/invalid.yml',
            'config', '-q'
        ], returncode=1)
        assert "'notaservice' must be a mapping" in result.stderr

    def test_config_quiet(self):
        self.base_dir = 'tests/fixtures/v2-full'
        assert self.dispatch(['config', '-q']).stdout == ''

    def test_config_default(self):
        self.base_dir = 'tests/fixtures/v2-full'
        result = self.dispatch(['config'])
        # assert there are no python objects encoded in the output
        assert '!!' not in result.stdout

        output = yaml.load(result.stdout)
        expected = {
            'version': '2.0',
            'volumes': {'data': {'driver': 'local'}},
            'networks': {'front': {}},
            'services': {
                'web': {
                    'build': {
                        'context': os.path.abspath(self.base_dir),
                    },
                    'networks': {'front': None, 'default': None},
                    'volumes_from': ['service:other:rw'],
                },
                'other': {
                    'image': 'busybox:latest',
                    'command': 'top',
                    'volumes': ['/data:rw'],
                },
            },
        }
        assert output == expected

    def test_config_restart(self):
        self.base_dir = 'tests/fixtures/restart'
        result = self.dispatch(['config'])
        assert yaml.load(result.stdout) == {
            'version': '2.0',
            'services': {
                'never': {
                    'image': 'busybox',
                    'restart': 'no',
                },
                'always': {
                    'image': 'busybox',
                    'restart': 'always',
                },
                'on-failure': {
                    'image': 'busybox',
                    'restart': 'on-failure',
                },
                'on-failure-5': {
                    'image': 'busybox',
                    'restart': 'on-failure:5',
                },
            },
            'networks': {},
            'volumes': {},
        }

    def test_config_v1(self):
        self.base_dir = 'tests/fixtures/v1-config'
        result = self.dispatch(['config'])
        assert yaml.load(result.stdout) == {
            'version': '2.0',
            'services': {
                'net': {
                    'image': 'busybox',
                    'network_mode': 'bridge',
                },
                'volume': {
                    'image': 'busybox',
                    'volumes': ['/data:rw'],
                    'network_mode': 'bridge',
                },
                'app': {
                    'image': 'busybox',
                    'volumes_from': ['service:volume:rw'],
                    'network_mode': 'service:net',
                },
            },
            'networks': {},
            'volumes': {},
        }

    def test_ps(self):
        self.project.get_service('simple').create_container()
        result = self.dispatch(['ps'])
        assert 'simplecomposefile_simple_1' in result.stdout

    def test_ps_default_composefile(self):
        self.base_dir = 'tests/fixtures/multiple-composefiles'
        self.dispatch(['up', '-d'])
        result = self.dispatch(['ps'])

        self.assertIn('multiplecomposefiles_simple_1', result.stdout)
        self.assertIn('multiplecomposefiles_another_1', result.stdout)
        self.assertNotIn('multiplecomposefiles_yetanother_1', result.stdout)

    def test_ps_alternate_composefile(self):
        config_path = os.path.abspath(
            'tests/fixtures/multiple-composefiles/compose2.yml')
        self._project = get_project(self.base_dir, [config_path])

        self.base_dir = 'tests/fixtures/multiple-composefiles'
        self.dispatch(['-f', 'compose2.yml', 'up', '-d'])
        result = self.dispatch(['-f', 'compose2.yml', 'ps'])

        self.assertNotIn('multiplecomposefiles_simple_1', result.stdout)
        self.assertNotIn('multiplecomposefiles_another_1', result.stdout)
        self.assertIn('multiplecomposefiles_yetanother_1', result.stdout)

    def test_pull(self):
        result = self.dispatch(['pull'])
        assert sorted(result.stderr.split('\n'))[1:] == [
            'Pulling another (busybox:latest)...',
            'Pulling simple (busybox:latest)...',
        ]

    def test_pull_with_digest(self):
        result = self.dispatch(['-f', 'digest.yml', 'pull'])

        assert 'Pulling simple (busybox:latest)...' in result.stderr
        assert ('Pulling digest (busybox@'
                'sha256:38a203e1986cf79639cfb9b2e1d6e773de84002feea2d4eb006b520'
                '04ee8502d)...') in result.stderr

    def test_pull_with_ignore_pull_failures(self):
        result = self.dispatch([
            '-f', 'ignore-pull-failures.yml',
            'pull', '--ignore-pull-failures'])

        assert 'Pulling simple (busybox:latest)...' in result.stderr
        assert 'Pulling another (nonexisting-image:latest)...' in result.stderr
        assert 'Error: image library/nonexisting-image' in result.stderr
        assert 'not found' in result.stderr

    def test_build_plain(self):
        self.base_dir = 'tests/fixtures/simple-dockerfile'
        self.dispatch(['build', 'simple'])

        result = self.dispatch(['build', 'simple'])
        assert BUILD_CACHE_TEXT in result.stdout
        assert BUILD_PULL_TEXT not in result.stdout

    def test_build_no_cache(self):
        self.base_dir = 'tests/fixtures/simple-dockerfile'
        self.dispatch(['build', 'simple'])

        result = self.dispatch(['build', '--no-cache', 'simple'])
        assert BUILD_CACHE_TEXT not in result.stdout
        assert BUILD_PULL_TEXT not in result.stdout

    def test_build_pull(self):
        # Make sure we have the latest busybox already
        pull_busybox(self.client)
        self.base_dir = 'tests/fixtures/simple-dockerfile'
        self.dispatch(['build', 'simple'], None)

        result = self.dispatch(['build', '--pull', 'simple'])
        assert BUILD_CACHE_TEXT in result.stdout
        assert BUILD_PULL_TEXT in result.stdout

    def test_build_no_cache_pull(self):
        # Make sure we have the latest busybox already
        pull_busybox(self.client)
        self.base_dir = 'tests/fixtures/simple-dockerfile'
        self.dispatch(['build', 'simple'])

        result = self.dispatch(['build', '--no-cache', '--pull', 'simple'])
        assert BUILD_CACHE_TEXT not in result.stdout
        assert BUILD_PULL_TEXT in result.stdout

    def test_build_failed(self):
        self.base_dir = 'tests/fixtures/simple-failing-dockerfile'
        self.dispatch(['build', 'simple'], returncode=1)

        labels = ["com.docker.compose.test_failing_image=true"]
        containers = [
            Container.from_ps(self.project.client, c)
            for c in self.project.client.containers(
                all=True,
                filters={"label": labels})
        ]
        assert len(containers) == 1

    def test_build_failed_forcerm(self):
        self.base_dir = 'tests/fixtures/simple-failing-dockerfile'
        self.dispatch(['build', '--force-rm', 'simple'], returncode=1)

        labels = ["com.docker.compose.test_failing_image=true"]

        containers = [
            Container.from_ps(self.project.client, c)
            for c in self.project.client.containers(
                all=True,
                filters={"label": labels})
        ]
        assert not containers

    def test_create(self):
        self.dispatch(['create'])
        service = self.project.get_service('simple')
        another = self.project.get_service('another')
        self.assertEqual(len(service.containers()), 0)
        self.assertEqual(len(another.containers()), 0)
        self.assertEqual(len(service.containers(stopped=True)), 1)
        self.assertEqual(len(another.containers(stopped=True)), 1)

    def test_create_with_force_recreate(self):
        self.dispatch(['create'], None)
        service = self.project.get_service('simple')
        self.assertEqual(len(service.containers()), 0)
        self.assertEqual(len(service.containers(stopped=True)), 1)

        old_ids = [c.id for c in service.containers(stopped=True)]

        self.dispatch(['create', '--force-recreate'], None)
        self.assertEqual(len(service.containers()), 0)
        self.assertEqual(len(service.containers(stopped=True)), 1)

        new_ids = [c.id for c in service.containers(stopped=True)]

        self.assertNotEqual(old_ids, new_ids)

    def test_create_with_no_recreate(self):
        self.dispatch(['create'], None)
        service = self.project.get_service('simple')
        self.assertEqual(len(service.containers()), 0)
        self.assertEqual(len(service.containers(stopped=True)), 1)

        old_ids = [c.id for c in service.containers(stopped=True)]

        self.dispatch(['create', '--no-recreate'], None)
        self.assertEqual(len(service.containers()), 0)
        self.assertEqual(len(service.containers(stopped=True)), 1)

        new_ids = [c.id for c in service.containers(stopped=True)]

        self.assertEqual(old_ids, new_ids)

    def test_create_with_force_recreate_and_no_recreate(self):
        self.dispatch(
            ['create', '--force-recreate', '--no-recreate'],
            returncode=1)

    def test_down_invalid_rmi_flag(self):
        result = self.dispatch(['down', '--rmi', 'bogus'], returncode=1)
        assert '--rmi flag must be' in result.stderr

    @v2_only()
    def test_down(self):
        self.base_dir = 'tests/fixtures/v2-full'

        self.dispatch(['up', '-d'])
        wait_on_condition(ContainerCountCondition(self.project, 2))

        self.dispatch(['run', 'web', 'true'])
        self.dispatch(['run', '-d', 'web', 'tail', '-f', '/dev/null'])
        assert len(self.project.containers(one_off=OneOffFilter.only, stopped=True)) == 2

        result = self.dispatch(['down', '--rmi=local', '--volumes'])
        assert 'Stopping v2full_web_1' in result.stderr
        assert 'Stopping v2full_other_1' in result.stderr
        assert 'Stopping v2full_web_run_2' in result.stderr
        assert 'Removing v2full_web_1' in result.stderr
        assert 'Removing v2full_other_1' in result.stderr
        assert 'Removing v2full_web_run_1' in result.stderr
        assert 'Removing v2full_web_run_2' in result.stderr
        assert 'Removing volume v2full_data' in result.stderr
        assert 'Removing image v2full_web' in result.stderr
        assert 'Removing image busybox' not in result.stderr
        assert 'Removing network v2full_default' in result.stderr
        assert 'Removing network v2full_front' in result.stderr

    def test_up_detached(self):
        self.dispatch(['up', '-d'])
        service = self.project.get_service('simple')
        another = self.project.get_service('another')
        self.assertEqual(len(service.containers()), 1)
        self.assertEqual(len(another.containers()), 1)

        # Ensure containers don't have stdin and stdout connected in -d mode
        container, = service.containers()
        self.assertFalse(container.get('Config.AttachStderr'))
        self.assertFalse(container.get('Config.AttachStdout'))
        self.assertFalse(container.get('Config.AttachStdin'))

    def test_up_attached(self):
        self.base_dir = 'tests/fixtures/echo-services'
        result = self.dispatch(['up', '--no-color'])

        assert 'simple_1   | simple' in result.stdout
        assert 'another_1  | another' in result.stdout
        assert 'simple_1 exited with code 0' in result.stdout
        assert 'another_1 exited with code 0' in result.stdout

    @v2_only()
    def test_up(self):
        self.base_dir = 'tests/fixtures/v2-simple'
        self.dispatch(['up', '-d'], None)

        services = self.project.get_services()

        network_name = self.project.networks.networks['default'].full_name
        networks = self.client.networks(names=[network_name])
        self.assertEqual(len(networks), 1)
        self.assertEqual(networks[0]['Driver'], 'bridge')
        assert 'com.docker.network.bridge.enable_icc' not in networks[0]['Options']

        network = self.client.inspect_network(networks[0]['Id'])

        for service in services:
            containers = service.containers()
            self.assertEqual(len(containers), 1)

            container = containers[0]
            self.assertIn(container.id, network['Containers'])

            networks = container.get('NetworkSettings.Networks')
            self.assertEqual(list(networks), [network['Name']])

            self.assertEqual(
                sorted(networks[network['Name']]['Aliases']),
                sorted([service.name, container.short_id]))

            for service in services:
                assert self.lookup(container, service.name)

    @v2_only()
    def test_up_with_default_network_config(self):
        filename = 'default-network-config.yml'

        self.base_dir = 'tests/fixtures/networks'
        self._project = get_project(self.base_dir, [filename])

        self.dispatch(['-f', filename, 'up', '-d'], None)

        network_name = self.project.networks.networks['default'].full_name
        networks = self.client.networks(names=[network_name])

        assert networks[0]['Options']['com.docker.network.bridge.enable_icc'] == 'false'

    @v2_only()
    def test_up_with_network_aliases(self):
        filename = 'network-aliases.yml'
        self.base_dir = 'tests/fixtures/networks'
        self.dispatch(['-f', filename, 'up', '-d'], None)
        back_name = '{}_back'.format(self.project.name)
        front_name = '{}_front'.format(self.project.name)

        networks = [
            n for n in self.client.networks()
            if n['Name'].startswith('{}_'.format(self.project.name))
        ]

        # Two networks were created: back and front
        assert sorted(n['Name'] for n in networks) == [back_name, front_name]
        web_container = self.project.get_service('web').containers()[0]

        back_aliases = web_container.get(
            'NetworkSettings.Networks.{}.Aliases'.format(back_name)
        )
        assert 'web' in back_aliases
        front_aliases = web_container.get(
            'NetworkSettings.Networks.{}.Aliases'.format(front_name)
        )
        assert 'web' in front_aliases
        assert 'forward_facing' in front_aliases
        assert 'ahead' in front_aliases

    @v2_only()
    def test_up_with_network_static_addresses(self):
        filename = 'network-static-addresses.yml'
        ipv4_address = '172.16.100.100'
        ipv6_address = 'fe80::1001:100'
        self.base_dir = 'tests/fixtures/networks'
        self.dispatch(['-f', filename, 'up', '-d'], None)
        static_net = '{}_static_test'.format(self.project.name)

        networks = [
            n for n in self.client.networks()
            if n['Name'].startswith('{}_'.format(self.project.name))
        ]

        # One networks was created: front
        assert sorted(n['Name'] for n in networks) == [static_net]
        web_container = self.project.get_service('web').containers()[0]

        ipam_config = web_container.get(
            'NetworkSettings.Networks.{}.IPAMConfig'.format(static_net)
        )
        assert ipv4_address in ipam_config.values()
        assert ipv6_address in ipam_config.values()

    @v2_only()
    def test_up_with_networks(self):
        self.base_dir = 'tests/fixtures/networks'
        self.dispatch(['up', '-d'], None)

        back_name = '{}_back'.format(self.project.name)
        front_name = '{}_front'.format(self.project.name)

        networks = [
            n for n in self.client.networks()
            if n['Name'].startswith('{}_'.format(self.project.name))
        ]

        # Two networks were created: back and front
        assert sorted(n['Name'] for n in networks) == [back_name, front_name]

        back_network = [n for n in networks if n['Name'] == back_name][0]
        front_network = [n for n in networks if n['Name'] == front_name][0]

        web_container = self.project.get_service('web').containers()[0]
        app_container = self.project.get_service('app').containers()[0]
        db_container = self.project.get_service('db').containers()[0]

        for net_name in [front_name, back_name]:
            links = app_container.get('NetworkSettings.Networks.{}.Links'.format(net_name))
            assert '{}:database'.format(db_container.name) in links

        # db and app joined the back network
        assert sorted(back_network['Containers']) == sorted([db_container.id, app_container.id])

        # web and app joined the front network
        assert sorted(front_network['Containers']) == sorted([web_container.id, app_container.id])

        # web can see app but not db
        assert self.lookup(web_container, "app")
        assert not self.lookup(web_container, "db")

        # app can see db
        assert self.lookup(app_container, "db")

        # app has aliased db to "database"
        assert self.lookup(app_container, "database")

    @v2_only()
    def test_up_missing_network(self):
        self.base_dir = 'tests/fixtures/networks'

        result = self.dispatch(
            ['-f', 'missing-network.yml', 'up', '-d'],
            returncode=1)

        assert 'Service "web" uses an undefined network "foo"' in result.stderr

    @v2_only()
    def test_up_with_network_mode(self):
        c = self.client.create_container('busybox', 'top', name='composetest_network_mode_container')
        self.addCleanup(self.client.remove_container, c, force=True)
        self.client.start(c)
        container_mode_source = 'container:{}'.format(c['Id'])

        filename = 'network-mode.yml'

        self.base_dir = 'tests/fixtures/networks'
        self._project = get_project(self.base_dir, [filename])

        self.dispatch(['-f', filename, 'up', '-d'], None)

        networks = [
            n for n in self.client.networks()
            if n['Name'].startswith('{}_'.format(self.project.name))
        ]
        assert not networks

        for name in ['bridge', 'host', 'none']:
            container = self.project.get_service(name).containers()[0]
            assert list(container.get('NetworkSettings.Networks')) == [name]
            assert container.get('HostConfig.NetworkMode') == name

        service_mode_source = 'container:{}'.format(
            self.project.get_service('bridge').containers()[0].id)
        service_mode_container = self.project.get_service('service').containers()[0]
        assert not service_mode_container.get('NetworkSettings.Networks')
        assert service_mode_container.get('HostConfig.NetworkMode') == service_mode_source

        container_mode_container = self.project.get_service('container').containers()[0]
        assert not container_mode_container.get('NetworkSettings.Networks')
        assert container_mode_container.get('HostConfig.NetworkMode') == container_mode_source

    @v2_only()
    def test_up_external_networks(self):
        filename = 'external-networks.yml'

        self.base_dir = 'tests/fixtures/networks'
        self._project = get_project(self.base_dir, [filename])

        result = self.dispatch(['-f', filename, 'up', '-d'], returncode=1)
        assert 'declared as external, but could not be found' in result.stderr

        networks = [
            n['Name'] for n in self.client.networks()
            if n['Name'].startswith('{}_'.format(self.project.name))
        ]
        assert not networks

        network_names = ['{}_{}'.format(self.project.name, n) for n in ['foo', 'bar']]
        for name in network_names:
            self.client.create_network(name)

        self.dispatch(['-f', filename, 'up', '-d'])
        container = self.project.containers()[0]
        assert sorted(list(container.get('NetworkSettings.Networks'))) == sorted(network_names)

    @v2_only()
    def test_up_with_external_default_network(self):
        filename = 'external-default.yml'

        self.base_dir = 'tests/fixtures/networks'
        self._project = get_project(self.base_dir, [filename])

        result = self.dispatch(['-f', filename, 'up', '-d'], returncode=1)
        assert 'declared as external, but could not be found' in result.stderr

        networks = [
            n['Name'] for n in self.client.networks()
            if n['Name'].startswith('{}_'.format(self.project.name))
        ]
        assert not networks

        network_name = 'composetest_external_network'
        self.client.create_network(network_name)

        self.dispatch(['-f', filename, 'up', '-d'])
        container = self.project.containers()[0]
        assert list(container.get('NetworkSettings.Networks')) == [network_name]

    @v2_only()
    def test_up_no_services(self):
        self.base_dir = 'tests/fixtures/no-services'
        self.dispatch(['up', '-d'], None)

        network_names = [
            n['Name'] for n in self.client.networks()
            if n['Name'].startswith('{}_'.format(self.project.name))
        ]
        assert network_names == []

    def test_up_with_links_v1(self):
        self.base_dir = 'tests/fixtures/links-composefile'
        self.dispatch(['up', '-d', 'web'], None)

        # No network was created
        network_name = self.project.networks.networks['default'].full_name
        networks = self.client.networks(names=[network_name])
        assert networks == []

        web = self.project.get_service('web')
        db = self.project.get_service('db')
        console = self.project.get_service('console')

        # console was not started
        self.assertEqual(len(web.containers()), 1)
        self.assertEqual(len(db.containers()), 1)
        self.assertEqual(len(console.containers()), 0)

        # web has links
        web_container = web.containers()[0]
        self.assertTrue(web_container.get('HostConfig.Links'))

    def test_up_with_net_is_invalid(self):
        self.base_dir = 'tests/fixtures/net-container'

        result = self.dispatch(
            ['-f', 'v2-invalid.yml', 'up', '-d'],
            returncode=1)

        assert "Unsupported config option for services.bar: 'net'" in result.stderr

    def test_up_with_net_v1(self):
        self.base_dir = 'tests/fixtures/net-container'
        self.dispatch(['up', '-d'], None)

        bar = self.project.get_service('bar')
        bar_container = bar.containers()[0]

        foo = self.project.get_service('foo')
        foo_container = foo.containers()[0]

        assert foo_container.get('HostConfig.NetworkMode') == \
            'container:{}'.format(bar_container.id)

    def test_up_with_no_deps(self):
        self.base_dir = 'tests/fixtures/links-composefile'
        self.dispatch(['up', '-d', '--no-deps', 'web'], None)
        web = self.project.get_service('web')
        db = self.project.get_service('db')
        console = self.project.get_service('console')
        self.assertEqual(len(web.containers()), 1)
        self.assertEqual(len(db.containers()), 0)
        self.assertEqual(len(console.containers()), 0)

    def test_up_with_force_recreate(self):
        self.dispatch(['up', '-d'], None)
        service = self.project.get_service('simple')
        self.assertEqual(len(service.containers()), 1)

        old_ids = [c.id for c in service.containers()]

        self.dispatch(['up', '-d', '--force-recreate'], None)
        self.assertEqual(len(service.containers()), 1)

        new_ids = [c.id for c in service.containers()]

        self.assertNotEqual(old_ids, new_ids)

    def test_up_with_no_recreate(self):
        self.dispatch(['up', '-d'], None)
        service = self.project.get_service('simple')
        self.assertEqual(len(service.containers()), 1)

        old_ids = [c.id for c in service.containers()]

        self.dispatch(['up', '-d', '--no-recreate'], None)
        self.assertEqual(len(service.containers()), 1)

        new_ids = [c.id for c in service.containers()]

        self.assertEqual(old_ids, new_ids)

    def test_up_with_force_recreate_and_no_recreate(self):
        self.dispatch(
            ['up', '-d', '--force-recreate', '--no-recreate'],
            returncode=1)

    def test_up_with_timeout(self):
        self.dispatch(['up', '-d', '-t', '1'])
        service = self.project.get_service('simple')
        another = self.project.get_service('another')
        self.assertEqual(len(service.containers()), 1)
        self.assertEqual(len(another.containers()), 1)

        # Ensure containers don't have stdin and stdout connected in -d mode
        config = service.containers()[0].inspect()['Config']
        self.assertFalse(config['AttachStderr'])
        self.assertFalse(config['AttachStdout'])
        self.assertFalse(config['AttachStdin'])

    def test_up_handles_sigint(self):
        proc = start_process(self.base_dir, ['up', '-t', '2'])
        wait_on_condition(ContainerCountCondition(self.project, 2))

        os.kill(proc.pid, signal.SIGINT)
        wait_on_condition(ContainerCountCondition(self.project, 0))

    def test_up_handles_sigterm(self):
        proc = start_process(self.base_dir, ['up', '-t', '2'])
        wait_on_condition(ContainerCountCondition(self.project, 2))

        os.kill(proc.pid, signal.SIGTERM)
        wait_on_condition(ContainerCountCondition(self.project, 0))

    @v2_only()
    def test_up_handles_force_shutdown(self):
        self.base_dir = 'tests/fixtures/sleeps-composefile'
        proc = start_process(self.base_dir, ['up', '-t', '200'])
        wait_on_condition(ContainerCountCondition(self.project, 2))

        os.kill(proc.pid, signal.SIGTERM)
        time.sleep(0.1)
        os.kill(proc.pid, signal.SIGTERM)
        wait_on_condition(ContainerCountCondition(self.project, 0))

    def test_up_handles_abort_on_container_exit(self):
        start_process(self.base_dir, ['up', '--abort-on-container-exit'])
        wait_on_condition(ContainerCountCondition(self.project, 2))
        self.project.stop(['simple'])
        wait_on_condition(ContainerCountCondition(self.project, 0))

    def test_exec_without_tty(self):
        self.base_dir = 'tests/fixtures/links-composefile'
        self.dispatch(['up', '-d', 'console'])
        self.assertEqual(len(self.project.containers()), 1)

        stdout, stderr = self.dispatch(['exec', '-T', 'console', 'ls', '-1d', '/'])
        self.assertEquals(stdout, "/\n")
        self.assertEquals(stderr, "")

    def test_exec_custom_user(self):
        self.base_dir = 'tests/fixtures/links-composefile'
        self.dispatch(['up', '-d', 'console'])
        self.assertEqual(len(self.project.containers()), 1)

        stdout, stderr = self.dispatch(['exec', '-T', '--user=operator', 'console', 'whoami'])
        self.assertEquals(stdout, "operator\n")
        self.assertEquals(stderr, "")

    def test_run_service_without_links(self):
        self.base_dir = 'tests/fixtures/links-composefile'
        self.dispatch(['run', 'console', '/bin/true'])
        self.assertEqual(len(self.project.containers()), 0)

        # Ensure stdin/out was open
        container = self.project.containers(stopped=True, one_off=OneOffFilter.only)[0]
        config = container.inspect()['Config']
        self.assertTrue(config['AttachStderr'])
        self.assertTrue(config['AttachStdout'])
        self.assertTrue(config['AttachStdin'])

    def test_run_service_with_links(self):
        self.base_dir = 'tests/fixtures/links-composefile'
        self.dispatch(['run', 'web', '/bin/true'], None)
        db = self.project.get_service('db')
        console = self.project.get_service('console')
        self.assertEqual(len(db.containers()), 1)
        self.assertEqual(len(console.containers()), 0)

    @v2_only()
    def test_run_service_with_dependencies(self):
        self.base_dir = 'tests/fixtures/v2-dependencies'
        self.dispatch(['run', 'web', '/bin/true'], None)
        db = self.project.get_service('db')
        console = self.project.get_service('console')
        self.assertEqual(len(db.containers()), 1)
        self.assertEqual(len(console.containers()), 0)

    def test_run_with_no_deps(self):
        self.base_dir = 'tests/fixtures/links-composefile'
        self.dispatch(['run', '--no-deps', 'web', '/bin/true'])
        db = self.project.get_service('db')
        self.assertEqual(len(db.containers()), 0)

    def test_run_does_not_recreate_linked_containers(self):
        self.base_dir = 'tests/fixtures/links-composefile'
        self.dispatch(['up', '-d', 'db'])
        db = self.project.get_service('db')
        self.assertEqual(len(db.containers()), 1)

        old_ids = [c.id for c in db.containers()]

        self.dispatch(['run', 'web', '/bin/true'], None)
        self.assertEqual(len(db.containers()), 1)

        new_ids = [c.id for c in db.containers()]

        self.assertEqual(old_ids, new_ids)

    def test_run_without_command(self):
        self.base_dir = 'tests/fixtures/commands-composefile'
        self.check_build('tests/fixtures/simple-dockerfile', tag='composetest_test')

        self.dispatch(['run', 'implicit'])
        service = self.project.get_service('implicit')
        containers = service.containers(stopped=True, one_off=OneOffFilter.only)
        self.assertEqual(
            [c.human_readable_command for c in containers],
            [u'/bin/sh -c echo "success"'],
        )

        self.dispatch(['run', 'explicit'])
        service = self.project.get_service('explicit')
        containers = service.containers(stopped=True, one_off=OneOffFilter.only)
        self.assertEqual(
            [c.human_readable_command for c in containers],
            [u'/bin/true'],
        )

    def test_run_service_with_entrypoint_overridden(self):
        self.base_dir = 'tests/fixtures/dockerfile_with_entrypoint'
        name = 'service'
        self.dispatch(['run', '--entrypoint', '/bin/echo', name, 'helloworld'])
        service = self.project.get_service(name)
        container = service.containers(stopped=True, one_off=OneOffFilter.only)[0]
        self.assertEqual(
            shlex.split(container.human_readable_command),
            [u'/bin/echo', u'helloworld'],
        )

    def test_run_service_with_user_overridden(self):
        self.base_dir = 'tests/fixtures/user-composefile'
        name = 'service'
        user = 'sshd'
        self.dispatch(['run', '--user={user}'.format(user=user), name], returncode=1)
        service = self.project.get_service(name)
        container = service.containers(stopped=True, one_off=OneOffFilter.only)[0]
        self.assertEqual(user, container.get('Config.User'))

    def test_run_service_with_user_overridden_short_form(self):
        self.base_dir = 'tests/fixtures/user-composefile'
        name = 'service'
        user = 'sshd'
        self.dispatch(['run', '-u', user, name], returncode=1)
        service = self.project.get_service(name)
        container = service.containers(stopped=True, one_off=OneOffFilter.only)[0]
        self.assertEqual(user, container.get('Config.User'))

    def test_run_service_with_environement_overridden(self):
        name = 'service'
        self.base_dir = 'tests/fixtures/environment-composefile'
        self.dispatch([
            'run', '-e', 'foo=notbar',
            '-e', 'allo=moto=bobo',
            '-e', 'alpha=beta',
            name,
            '/bin/true',
        ])
        service = self.project.get_service(name)
        container = service.containers(stopped=True, one_off=OneOffFilter.only)[0]
        # env overriden
        self.assertEqual('notbar', container.environment['foo'])
        # keep environement from yaml
        self.assertEqual('world', container.environment['hello'])
        # added option from command line
        self.assertEqual('beta', container.environment['alpha'])
        # make sure a value with a = don't crash out
        self.assertEqual('moto=bobo', container.environment['allo'])

    def test_run_service_without_map_ports(self):
        # create one off container
        self.base_dir = 'tests/fixtures/ports-composefile'
        self.dispatch(['run', '-d', 'simple'])
        container = self.project.get_service('simple').containers(one_off=OneOffFilter.only)[0]

        # get port information
        port_random = container.get_local_port(3000)
        port_assigned = container.get_local_port(3001)

        # close all one off containers we just created
        container.stop()

        # check the ports
        self.assertEqual(port_random, None)
        self.assertEqual(port_assigned, None)

    def test_run_service_with_map_ports(self):
        # create one off container
        self.base_dir = 'tests/fixtures/ports-composefile'
        self.dispatch(['run', '-d', '--service-ports', 'simple'])
        container = self.project.get_service('simple').containers(one_off=OneOffFilter.only)[0]

        # get port information
        port_random = container.get_local_port(3000)
        port_assigned = container.get_local_port(3001)
        port_range = container.get_local_port(3002), container.get_local_port(3003)

        # close all one off containers we just created
        container.stop()

        # check the ports
        self.assertNotEqual(port_random, None)
        self.assertIn("0.0.0.0", port_random)
        self.assertEqual(port_assigned, "0.0.0.0:49152")
        self.assertEqual(port_range[0], "0.0.0.0:49153")
        self.assertEqual(port_range[1], "0.0.0.0:49154")

    def test_run_service_with_explicitly_maped_ports(self):
        # create one off container
        self.base_dir = 'tests/fixtures/ports-composefile'
        self.dispatch(['run', '-d', '-p', '30000:3000', '--publish', '30001:3001', 'simple'])
        container = self.project.get_service('simple').containers(one_off=OneOffFilter.only)[0]

        # get port information
        port_short = container.get_local_port(3000)
        port_full = container.get_local_port(3001)

        # close all one off containers we just created
        container.stop()

        # check the ports
        self.assertEqual(port_short, "0.0.0.0:30000")
        self.assertEqual(port_full, "0.0.0.0:30001")

    def test_run_service_with_explicitly_maped_ip_ports(self):
        # create one off container
        self.base_dir = 'tests/fixtures/ports-composefile'
        self.dispatch([
            'run', '-d',
            '-p', '127.0.0.1:30000:3000',
            '--publish', '127.0.0.1:30001:3001',
            'simple'
        ])
        container = self.project.get_service('simple').containers(one_off=OneOffFilter.only)[0]

        # get port information
        port_short = container.get_local_port(3000)
        port_full = container.get_local_port(3001)

        # close all one off containers we just created
        container.stop()

        # check the ports
        self.assertEqual(port_short, "127.0.0.1:30000")
        self.assertEqual(port_full, "127.0.0.1:30001")

    def test_run_with_expose_ports(self):
        # create one off container
        self.base_dir = 'tests/fixtures/expose-composefile'
        self.dispatch(['run', '-d', '--service-ports', 'simple'])
        container = self.project.get_service('simple').containers(one_off=OneOffFilter.only)[0]

        ports = container.ports
        self.assertEqual(len(ports), 9)
        # exposed ports are not mapped to host ports
        assert ports['3000/tcp'] is None
        assert ports['3001/tcp'] is None
        assert ports['3001/udp'] is None
        assert ports['3002/tcp'] is None
        assert ports['3003/tcp'] is None
        assert ports['3004/tcp'] is None
        assert ports['3005/tcp'] is None
        assert ports['3006/udp'] is None
        assert ports['3007/udp'] is None

        # close all one off containers we just created
        container.stop()

    def test_run_with_custom_name(self):
        self.base_dir = 'tests/fixtures/environment-composefile'
        name = 'the-container-name'
        self.dispatch(['run', '--name', name, 'service', '/bin/true'])

        service = self.project.get_service('service')
        container, = service.containers(stopped=True, one_off=OneOffFilter.only)
        self.assertEqual(container.name, name)

    def test_run_service_with_workdir_overridden(self):
        self.base_dir = 'tests/fixtures/run-workdir'
        name = 'service'
        workdir = '/var'
        self.dispatch(['run', '--workdir={workdir}'.format(workdir=workdir), name])
        service = self.project.get_service(name)
        container = service.containers(stopped=True, one_off=True)[0]
        self.assertEqual(workdir, container.get('Config.WorkingDir'))

    def test_run_service_with_workdir_overridden_short_form(self):
        self.base_dir = 'tests/fixtures/run-workdir'
        name = 'service'
        workdir = '/var'
        self.dispatch(['run', '-w', workdir, name])
        service = self.project.get_service(name)
        container = service.containers(stopped=True, one_off=True)[0]
        self.assertEqual(workdir, container.get('Config.WorkingDir'))

    @v2_only()
    def test_run_interactive_connects_to_network(self):
        self.base_dir = 'tests/fixtures/networks'

        self.dispatch(['up', '-d'])
        self.dispatch(['run', 'app', 'nslookup', 'app'])
        self.dispatch(['run', 'app', 'nslookup', 'db'])

        containers = self.project.get_service('app').containers(
            stopped=True, one_off=OneOffFilter.only)
        assert len(containers) == 2

        for container in containers:
            networks = container.get('NetworkSettings.Networks')

            assert sorted(list(networks)) == [
                '{}_{}'.format(self.project.name, name)
                for name in ['back', 'front']
            ]

            for _, config in networks.items():
                assert not config['Aliases']

    @v2_only()
    def test_run_detached_connects_to_network(self):
        self.base_dir = 'tests/fixtures/networks'
        self.dispatch(['up', '-d'])
        self.dispatch(['run', '-d', 'app', 'top'])

        container = self.project.get_service('app').containers(one_off=OneOffFilter.only)[0]
        networks = container.get('NetworkSettings.Networks')

        assert sorted(list(networks)) == [
            '{}_{}'.format(self.project.name, name)
            for name in ['back', 'front']
        ]

        for _, config in networks.items():
            assert not config['Aliases']

        assert self.lookup(container, 'app')
        assert self.lookup(container, 'db')

    def test_run_handles_sigint(self):
        proc = start_process(self.base_dir, ['run', '-T', 'simple', 'top'])
        wait_on_condition(ContainerStateCondition(
            self.project.client,
            'simplecomposefile_simple_run_1',
            'running'))

        os.kill(proc.pid, signal.SIGINT)
        wait_on_condition(ContainerStateCondition(
            self.project.client,
            'simplecomposefile_simple_run_1',
            'exited'))

    def test_run_handles_sigterm(self):
        proc = start_process(self.base_dir, ['run', '-T', 'simple', 'top'])
        wait_on_condition(ContainerStateCondition(
            self.project.client,
            'simplecomposefile_simple_run_1',
            'running'))

        os.kill(proc.pid, signal.SIGTERM)
        wait_on_condition(ContainerStateCondition(
            self.project.client,
            'simplecomposefile_simple_run_1',
            'exited'))

    def test_rm(self):
        service = self.project.get_service('simple')
        service.create_container()
        kill_service(service)
        self.assertEqual(len(service.containers(stopped=True)), 1)
        self.dispatch(['rm', '--force'], None)
        self.assertEqual(len(service.containers(stopped=True)), 0)
        service = self.project.get_service('simple')
        service.create_container()
        kill_service(service)
        self.assertEqual(len(service.containers(stopped=True)), 1)
        self.dispatch(['rm', '-f'], None)
        self.assertEqual(len(service.containers(stopped=True)), 0)

    def test_rm_all(self):
        service = self.project.get_service('simple')
        service.create_container(one_off=False)
        service.create_container(one_off=True)
        kill_service(service)
        self.assertEqual(len(service.containers(stopped=True)), 1)
        self.assertEqual(len(service.containers(stopped=True, one_off=OneOffFilter.only)), 1)
        self.dispatch(['rm', '-f'], None)
        self.assertEqual(len(service.containers(stopped=True)), 0)
        self.assertEqual(len(service.containers(stopped=True, one_off=OneOffFilter.only)), 0)

        service.create_container(one_off=False)
        service.create_container(one_off=True)
        kill_service(service)
        self.assertEqual(len(service.containers(stopped=True)), 1)
        self.assertEqual(len(service.containers(stopped=True, one_off=OneOffFilter.only)), 1)
        self.dispatch(['rm', '-f', '--all'], None)
        self.assertEqual(len(service.containers(stopped=True)), 0)
        self.assertEqual(len(service.containers(stopped=True, one_off=OneOffFilter.only)), 0)

    def test_stop(self):
        self.dispatch(['up', '-d'], None)
        service = self.project.get_service('simple')
        self.assertEqual(len(service.containers()), 1)
        self.assertTrue(service.containers()[0].is_running)

        self.dispatch(['stop', '-t', '1'], None)

        self.assertEqual(len(service.containers(stopped=True)), 1)
        self.assertFalse(service.containers(stopped=True)[0].is_running)

    def test_stop_signal(self):
        self.base_dir = 'tests/fixtures/stop-signal-composefile'
        self.dispatch(['up', '-d'], None)
        service = self.project.get_service('simple')
        self.assertEqual(len(service.containers()), 1)
        self.assertTrue(service.containers()[0].is_running)

        self.dispatch(['stop', '-t', '1'], None)
        self.assertEqual(len(service.containers(stopped=True)), 1)
        self.assertFalse(service.containers(stopped=True)[0].is_running)
        self.assertEqual(service.containers(stopped=True)[0].exit_code, 0)

    def test_start_no_containers(self):
        result = self.dispatch(['start'], returncode=1)
        assert 'No containers to start' in result.stderr

    @v2_only()
    def test_up_logging(self):
        self.base_dir = 'tests/fixtures/logging-composefile'
        self.dispatch(['up', '-d'])
        simple = self.project.get_service('simple').containers()[0]
        log_config = simple.get('HostConfig.LogConfig')
        self.assertTrue(log_config)
        self.assertEqual(log_config.get('Type'), 'none')

        another = self.project.get_service('another').containers()[0]
        log_config = another.get('HostConfig.LogConfig')
        self.assertTrue(log_config)
        self.assertEqual(log_config.get('Type'), 'json-file')
        self.assertEqual(log_config.get('Config')['max-size'], '10m')

    def test_up_logging_legacy(self):
        self.base_dir = 'tests/fixtures/logging-composefile-legacy'
        self.dispatch(['up', '-d'])
        simple = self.project.get_service('simple').containers()[0]
        log_config = simple.get('HostConfig.LogConfig')
        self.assertTrue(log_config)
        self.assertEqual(log_config.get('Type'), 'none')

        another = self.project.get_service('another').containers()[0]
        log_config = another.get('HostConfig.LogConfig')
        self.assertTrue(log_config)
        self.assertEqual(log_config.get('Type'), 'json-file')
        self.assertEqual(log_config.get('Config')['max-size'], '10m')

    def test_pause_unpause(self):
        self.dispatch(['up', '-d'], None)
        service = self.project.get_service('simple')
        self.assertFalse(service.containers()[0].is_paused)

        self.dispatch(['pause'], None)
        self.assertTrue(service.containers()[0].is_paused)

        self.dispatch(['unpause'], None)
        self.assertFalse(service.containers()[0].is_paused)

    def test_pause_no_containers(self):
        result = self.dispatch(['pause'], returncode=1)
        assert 'No containers to pause' in result.stderr

    def test_unpause_no_containers(self):
        result = self.dispatch(['unpause'], returncode=1)
        assert 'No containers to unpause' in result.stderr

    def test_logs_invalid_service_name(self):
        self.dispatch(['logs', 'madeupname'], returncode=1)

    def test_logs_follow(self):
        self.base_dir = 'tests/fixtures/echo-services'
        self.dispatch(['up', '-d'])

        result = self.dispatch(['logs', '-f'])

        assert result.stdout.count('\n') == 5
        assert 'simple' in result.stdout
        assert 'another' in result.stdout
        assert 'exited with code 0' in result.stdout

    def test_logs_follow_logs_from_new_containers(self):
        self.base_dir = 'tests/fixtures/logs-composefile'
        self.dispatch(['up', '-d', 'simple'])

        proc = start_process(self.base_dir, ['logs', '-f'])

        self.dispatch(['up', '-d', 'another'])
        wait_on_condition(ContainerStateCondition(
            self.project.client,
            'logscomposefile_another_1',
            'exited'))

        self.dispatch(['kill', 'simple'])

        result = wait_on_process(proc)

        assert 'hello' in result.stdout
        assert 'test' in result.stdout
        assert 'logscomposefile_another_1 exited with code 0' in result.stdout
        assert 'logscomposefile_simple_1 exited with code 137' in result.stdout

    def test_logs_default(self):
        self.base_dir = 'tests/fixtures/logs-composefile'
        self.dispatch(['up', '-d'])

        result = self.dispatch(['logs'])
        assert 'hello' in result.stdout
        assert 'test' in result.stdout
        assert 'exited with' not in result.stdout

    def test_logs_on_stopped_containers_exits(self):
        self.base_dir = 'tests/fixtures/echo-services'
        self.dispatch(['up'])

        result = self.dispatch(['logs'])
        assert 'simple' in result.stdout
        assert 'another' in result.stdout
        assert 'exited with' not in result.stdout

    def test_logs_timestamps(self):
        self.base_dir = 'tests/fixtures/echo-services'
        self.dispatch(['up', '-d'])

        result = self.dispatch(['logs', '-f', '-t'])
        self.assertRegexpMatches(result.stdout, '(\d{4})-(\d{2})-(\d{2})T(\d{2})\:(\d{2})\:(\d{2})')

    def test_logs_tail(self):
        self.base_dir = 'tests/fixtures/logs-tail-composefile'
        self.dispatch(['up'])

        result = self.dispatch(['logs', '--tail', '2'])
        assert result.stdout.count('\n') == 3

    def test_kill(self):
        self.dispatch(['up', '-d'], None)
        service = self.project.get_service('simple')
        self.assertEqual(len(service.containers()), 1)
        self.assertTrue(service.containers()[0].is_running)

        self.dispatch(['kill'], None)

        self.assertEqual(len(service.containers(stopped=True)), 1)
        self.assertFalse(service.containers(stopped=True)[0].is_running)

    def test_kill_signal_sigstop(self):
        self.dispatch(['up', '-d'], None)
        service = self.project.get_service('simple')
        self.assertEqual(len(service.containers()), 1)
        self.assertTrue(service.containers()[0].is_running)

        self.dispatch(['kill', '-s', 'SIGSTOP'], None)

        self.assertEqual(len(service.containers()), 1)
        # The container is still running. It has only been paused
        self.assertTrue(service.containers()[0].is_running)

    def test_kill_stopped_service(self):
        self.dispatch(['up', '-d'], None)
        service = self.project.get_service('simple')
        self.dispatch(['kill', '-s', 'SIGSTOP'], None)
        self.assertTrue(service.containers()[0].is_running)

        self.dispatch(['kill', '-s', 'SIGKILL'], None)

        self.assertEqual(len(service.containers(stopped=True)), 1)
        self.assertFalse(service.containers(stopped=True)[0].is_running)

    def test_restart(self):
        service = self.project.get_service('simple')
        container = service.create_container()
        service.start_container(container)
        started_at = container.dictionary['State']['StartedAt']
        self.dispatch(['restart', '-t', '1'], None)
        container.inspect()
        self.assertNotEqual(
            container.dictionary['State']['FinishedAt'],
            '0001-01-01T00:00:00Z',
        )
        self.assertNotEqual(
            container.dictionary['State']['StartedAt'],
            started_at,
        )

    def test_restart_stopped_container(self):
        service = self.project.get_service('simple')
        container = service.create_container()
        container.start()
        container.kill()
        self.assertEqual(len(service.containers(stopped=True)), 1)
        self.dispatch(['restart', '-t', '1'], None)
        self.assertEqual(len(service.containers(stopped=False)), 1)

    def test_restart_no_containers(self):
        result = self.dispatch(['restart'], returncode=1)
        assert 'No containers to restart' in result.stderr

    def test_scale(self):
        project = self.project

        self.dispatch(['scale', 'simple=1'])
        self.assertEqual(len(project.get_service('simple').containers()), 1)

        self.dispatch(['scale', 'simple=3', 'another=2'])
        self.assertEqual(len(project.get_service('simple').containers()), 3)
        self.assertEqual(len(project.get_service('another').containers()), 2)

        self.dispatch(['scale', 'simple=1', 'another=1'])
        self.assertEqual(len(project.get_service('simple').containers()), 1)
        self.assertEqual(len(project.get_service('another').containers()), 1)

        self.dispatch(['scale', 'simple=1', 'another=1'])
        self.assertEqual(len(project.get_service('simple').containers()), 1)
        self.assertEqual(len(project.get_service('another').containers()), 1)

        self.dispatch(['scale', 'simple=0', 'another=0'])
        self.assertEqual(len(project.get_service('simple').containers()), 0)
        self.assertEqual(len(project.get_service('another').containers()), 0)

    def test_port(self):
        self.base_dir = 'tests/fixtures/ports-composefile'
        self.dispatch(['up', '-d'], None)
        container = self.project.get_service('simple').get_container()

        def get_port(number):
            result = self.dispatch(['port', 'simple', str(number)])
            return result.stdout.rstrip()

        self.assertEqual(get_port(3000), container.get_local_port(3000))
        self.assertEqual(get_port(3001), "0.0.0.0:49152")
        self.assertEqual(get_port(3002), "0.0.0.0:49153")

    def test_port_with_scale(self):
        self.base_dir = 'tests/fixtures/ports-composefile-scale'
        self.dispatch(['scale', 'simple=2'], None)
        containers = sorted(
            self.project.containers(service_names=['simple']),
            key=attrgetter('name'))

        def get_port(number, index=None):
            if index is None:
                result = self.dispatch(['port', 'simple', str(number)])
            else:
                result = self.dispatch(['port', '--index=' + str(index), 'simple', str(number)])
            return result.stdout.rstrip()

        self.assertEqual(get_port(3000), containers[0].get_local_port(3000))
        self.assertEqual(get_port(3000, index=1), containers[0].get_local_port(3000))
        self.assertEqual(get_port(3000, index=2), containers[1].get_local_port(3000))
        self.assertEqual(get_port(3002), "")

    def test_events_json(self):
        events_proc = start_process(self.base_dir, ['events', '--json'])
        self.dispatch(['up', '-d'])
        wait_on_condition(ContainerCountCondition(self.project, 2))

        os.kill(events_proc.pid, signal.SIGINT)
        result = wait_on_process(events_proc, returncode=1)
        lines = [json.loads(line) for line in result.stdout.rstrip().split('\n')]
        assert Counter(e['action'] for e in lines) == {'create': 2, 'start': 2}

    def test_events_human_readable(self):
        events_proc = start_process(self.base_dir, ['events'])
        self.dispatch(['up', '-d', 'simple'])
        wait_on_condition(ContainerCountCondition(self.project, 1))

        os.kill(events_proc.pid, signal.SIGINT)
        result = wait_on_process(events_proc, returncode=1)
        lines = result.stdout.rstrip().split('\n')
        assert len(lines) == 2

        container, = self.project.containers()
        expected_template = (
            ' container {} {} (image=busybox:latest, '
            'name=simplecomposefile_simple_1)')

        assert expected_template.format('create', container.id) in lines[0]
        assert expected_template.format('start', container.id) in lines[1]
        assert lines[0].startswith(datetime.date.today().isoformat())

    def test_env_file_relative_to_compose_file(self):
        config_path = os.path.abspath('tests/fixtures/env-file/docker-compose.yml')
        self.dispatch(['-f', config_path, 'up', '-d'], None)
        self._project = get_project(self.base_dir, [config_path])

        containers = self.project.containers(stopped=True)
        self.assertEqual(len(containers), 1)
        self.assertIn("FOO=1", containers[0].get('Config.Env'))

    @mock.patch.dict(os.environ)
    def test_home_and_env_var_in_volume_path(self):
        os.environ['VOLUME_NAME'] = 'my-volume'
        os.environ['HOME'] = '/tmp/home-dir'

        self.base_dir = 'tests/fixtures/volume-path-interpolation'
        self.dispatch(['up', '-d'], None)

        container = self.project.containers(stopped=True)[0]
        actual_host_path = container.get_mount('/container-path')['Source']
        components = actual_host_path.split('/')
        assert components[-2:] == ['home-dir', 'my-volume']

    def test_up_with_default_override_file(self):
        self.base_dir = 'tests/fixtures/override-files'
        self.dispatch(['up', '-d'], None)

        containers = self.project.containers()
        self.assertEqual(len(containers), 2)

        web, db = containers
        self.assertEqual(web.human_readable_command, 'top')
        self.assertEqual(db.human_readable_command, 'top')

    def test_up_with_multiple_files(self):
        self.base_dir = 'tests/fixtures/override-files'
        config_paths = [
            'docker-compose.yml',
            'docker-compose.override.yml',
            'extra.yml',

        ]
        self._project = get_project(self.base_dir, config_paths)
        self.dispatch(
            [
                '-f', config_paths[0],
                '-f', config_paths[1],
                '-f', config_paths[2],
                'up', '-d',
            ],
            None)

        containers = self.project.containers()
        self.assertEqual(len(containers), 3)

        web, other, db = containers
        self.assertEqual(web.human_readable_command, 'top')
        self.assertTrue({'db', 'other'} <= set(get_links(web)))
        self.assertEqual(db.human_readable_command, 'top')
        self.assertEqual(other.human_readable_command, 'top')

    def test_up_with_extends(self):
        self.base_dir = 'tests/fixtures/extends'
        self.dispatch(['up', '-d'], None)

        self.assertEqual(
            set([s.name for s in self.project.services]),
            set(['mydb', 'myweb']),
        )

        # Sort by name so we get [db, web]
        containers = sorted(
            self.project.containers(stopped=True),
            key=lambda c: c.name,
        )

        self.assertEqual(len(containers), 2)
        web = containers[1]

        self.assertEqual(
            set(get_links(web)),
            set(['db', 'mydb_1', 'extends_mydb_1']))

        expected_env = set([
            "FOO=1",
            "BAR=2",
            "BAZ=2",
        ])
        self.assertTrue(expected_env <= set(web.get('Config.Env')))
