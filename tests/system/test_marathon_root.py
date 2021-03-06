""" Test using root marathon.
    This test suite imports all common tests found in marathon_common.py which are
    to be tested on root marathon and MoM.
    In addition it contains tests which are specific to root marathon, specifically
    tests round dcos services registration and control and security.
"""

import apps
import common
import json
import os
import pytest
import requests
import retrying
import uuid

from datetime import timedelta

import dcos_service_marathon_tests
import marathon_auth_common_tests
import marathon_common_tests
import marathon_pods_tests

from shakedown.clients import marathon
from shakedown.dcos import marathon_leader_ip
from shakedown.dcos.agent import get_private_agents, get_public_agents, public_agents, required_public_agents # NOQA F401
from shakedown.dcos.cluster import dcos_1_9, dcos_version_less_than, ee_version, is_strict # NOQA F401
from shakedown.dcos.command import run_command, run_command_on_agent, run_command_on_master
from shakedown.dcos.marathon import deployment_wait, marathon_version_less_than # NOQA F401
from shakedown.dcos.master import get_all_master_ips, masters, is_multi_master, required_masters # NOQA F401
from shakedown.dcos.service import wait_for_service_endpoint
from fixtures import sse_events, wait_for_marathon_and_cleanup, user_billy, docker_ipv6_network_fixture, archive_sandboxes, install_enterprise_cli # NOQA F401


# the following lines essentially do:
#     from dcos_service_marathon_tests import test_*
for attribute in dir(dcos_service_marathon_tests):
    if attribute.startswith('test_'):
        exec("from dcos_service_marathon_tests import {}".format(attribute))

# the following lines essentially do:
#     from marathon_auth_common_tests import test_*
for attribute in dir(marathon_auth_common_tests):
    if attribute.startswith('test_'):
        exec("from marathon_auth_common_tests import {}".format(attribute))

# the following lines essentially do:
#     from marathon_common_tests import test_*
for attribute in dir(marathon_common_tests):
    if attribute.startswith('test_'):
        exec("from marathon_common_tests import {}".format(attribute))

# the following lines essentially do:
#     from marathon_pods_tests import test_*
for attribute in dir(marathon_pods_tests):
    if attribute.startswith('test_'):
        exec("from marathon_pods_tests import {}".format(attribute))


pytestmark = [pytest.mark.usefixtures('wait_for_marathon_and_cleanup')]


@pytest.fixture(scope="function")
def marathon_service_name():
    return "marathon"


def setup_module(module):
    # When the cluster is starting, it might happen that there is some delay in:
    # - marathon leader registration with mesos
    # - admin router refreshing cache (every 30s)
    # We should not start our tests before marathon is accessible through service endpoint.
    wait_for_service_endpoint('marathon', timedelta(minutes=5).total_seconds(), path="ping")

    common.cluster_info()
    common.clean_up_marathon()


def teardown_module(module):
    common.clean_up_marathon()


#################################################
# Root Marathon specific tests
#################################################


@masters(3)
def test_marathon_delete_leader(marathon_service_name):
    original_leader = marathon_leader_ip()
    print('leader: {}'.format(original_leader))
    common.abdicate_marathon_leader()

    wait_for_service_endpoint(marathon_service_name, timedelta(minutes=5).total_seconds(), path="ping")

    common.assert_marathon_leadership_changed(original_leader)


@masters(3)
def test_marathon_delete_leader_and_check_apps(marathon_service_name):
    original_leader = marathon_leader_ip()
    print('leader: {}'.format(original_leader))

    app_def = apps.sleep_app()
    app_id = app_def['id']

    client = marathon.create_client()
    client.add_app(app_def)
    deployment_wait(service_id=app_id)

    app = client.get_app(app_id)
    assert app['tasksRunning'] == 1, "The number of running tasks is {}, but 1 was expected".format(app["tasksRunning"])

    # abdicate leader after app was started successfully
    common.abdicate_marathon_leader()

    wait_for_service_endpoint(marathon_service_name, timedelta(minutes=5).total_seconds(), path="ping")

    # wait until leader changed
    common.assert_marathon_leadership_changed(original_leader)
    original_leader = marathon_leader_ip()

    @retrying.retry(wait_fixed=1000, stop_max_attempt_number=30, retry_on_exception=common.ignore_exception)
    def check_app_existence(expected_instances):
        app = client.get_app(app_id)
        assert app['tasksRunning'] == expected_instances
        assert app['tasksRunning'] == expected_instances, \
            "The number of running tasks is {}, but {} was expected".format(app["tasksRunning"], expected_instances)

    # check if app definition is still there and one instance is still running after new leader was elected
    check_app_existence(1)

    @retrying.retry(wait_fixed=1000, stop_max_attempt_number=30, retry_on_exception=common.ignore_exception)
    def remove_app(app_id):
        client.remove_app(app_id)

    remove_app(app_id)
    deployment_wait(service_id=app_id)

    try:
        client.get_app(app_id)
    except Exception:
        pass
    else:
        assert False, "The application resurrected"

    # abdicate leader after app was started successfully
    common.abdicate_marathon_leader()

    wait_for_service_endpoint(marathon_service_name, timedelta(minutes=5).total_seconds(), path="ping")

    # wait until leader changed
    common.assert_marathon_leadership_changed(original_leader)

    # check if app definition is still not there
    try:
        client.get_app(app_id)
    except Exception:
        pass
    else:
        assert False, "The application resurrected"


@masters(3)
def test_marathon_zk_partition_leader_change(marathon_service_name):

    original_leader = common.get_marathon_leader_not_on_master_leader_node()

    common.block_iptable_rules_for_seconds(original_leader, 2181, sleep_seconds=30)

    common.assert_marathon_leadership_changed(original_leader)


@masters(3)
def test_marathon_master_partition_leader_change(marathon_service_name):

    original_leader = common.get_marathon_leader_not_on_master_leader_node()

    # blocking outbound connection to mesos master
    # Marathon has a Mesos heartbeat interval of 15 seconds. If 5 are missed it
    # disconnects. Thus we should wait more than 75 seconds.
    common.block_iptable_rules_for_seconds(original_leader, 5050, sleep_seconds=100,
                                           block_input=False, block_output=True)

    common.assert_marathon_leadership_changed(original_leader)


@public_agents(1)
def test_launch_app_on_public_agent():
    """ Test the successful launch of a mesos container on public agent.
        MoMs by default do not have slave_public access.
    """
    client = marathon.create_client()
    app_def = common.add_role_constraint_to_app_def(apps.mesos_app(), ['slave_public'])
    app_id = app_def["id"]
    client.add_app(app_def)
    deployment_wait(service_id=app_id)

    tasks = client.get_tasks(app_id)
    task_ip = tasks[0]['host']

    assert task_ip in get_public_agents(), "The application task got started on a private agent"


@pytest.mark.skipif("is_strict()") # NOQA F811
@pytest.mark.skipif('marathon_version_less_than("1.3.9")')
@pytest.mark.usefixtures("wait_for_marathon_and_cleanup")
@pytest.mark.asyncio
async def test_event_channel(sse_events):
    """ Tests the event channel. The way events are verified is by converting
        the parsed events to an iterator and asserting the right oder of certain
        events. Unknown events are skipped.
    """

    await common.assert_event('event_stream_attached', sse_events)

    app_def = apps.mesos_app()
    app_id = app_def['id']

    client = marathon.create_client()
    client.add_app(app_def)
    deployment_wait(service_id=app_id)

    await common.assert_event('deployment_info', sse_events)
    await common.assert_event('deployment_step_success', sse_events)

    client.remove_app(app_id, True)
    deployment_wait(service_id=app_id)

    await common.assert_event('app_terminated_event', sse_events)


@dcos_1_9
@pytest.mark.skipif("is_strict()")
def test_external_volume():
    volume_name = "marathon-si-test-vol-{}".format(uuid.uuid4().hex)
    app_def = apps.external_volume_mesos_app()
    app_def["container"]["volumes"][0]["external"]["name"] = volume_name
    app_id = app_def['id']

    # Tested with root marathon since MoM doesn't have
    # --enable_features external_volumes option activated.
    # First deployment should create the volume since it has a unique name
    try:
        print('INFO: Deploying {} with external volume {}'.format(app_id, volume_name))
        client = marathon.create_client()
        client.add_app(app_def)
        deployment_wait(service_id=app_id)

        # Create the app: the volume should be successfully created
        common.assert_app_tasks_running(client, app_def)
        common.assert_app_tasks_healthy(client, app_def)

        # Scale down to 0
        print('INFO: Scaling {} to 0 instances'.format(app_id))
        client.stop_app(app_id)
        deployment_wait(service_id=app_id)

        # Scale up again: the volume should be successfully reused
        print('INFO: Scaling {} back to 1 instance'.format(app_id))
        client.scale_app(app_id, 1)
        deployment_wait(service_id=app_id)

        common.assert_app_tasks_running(client, app_def)
        common.assert_app_tasks_healthy(client, app_def)

        # Remove the app to be able to remove the volume
        print('INFO: Finally removing {}'.format(app_id))
        client.remove_app(app_id)
        deployment_wait(service_id=app_id)
    except Exception as e:
        print('Fail to test external volumes: {}'.format(e))
        raise e
    finally:
        # Clean up after the test: external volumes are not destroyed by marathon or dcos
        # and have to be cleaned manually.
        cmd = 'sudo /opt/mesosphere/bin/dvdcli remove --volumedriver=rexray --volumename={}'.format(volume_name)
        removed = False
        for agent in get_private_agents():
            status, output = run_command_on_agent(agent, cmd)  # NOQA
            print('DEBUG: Failed to remove external volume with name={} on agent={}: {}'.format(
                volume_name, agent, output))
            if status:
                removed = True
        # Note: Removing the volume might fail sometimes because EC2 takes some time (~10min) to recognize that
        # the volume is not in use anymore hence preventing it's removal. This is a known pitfall: we log the error
        # and the volume should be cleaned up manually later.
        if not removed:
            print('WARNING: Failed to remove external volume with name={}'.format(volume_name))
        else:
            print('DEBUG: External volume with name={} successfully removed'.format(volume_name))


@pytest.mark.skipif('is_multi_master() or marathon_version_less_than("1.5")')
def test_marathon_backup_and_restore_leader(marathon_service_name):
    """Backup and restore meeting is done with only one master since new master has to be able
       to read the backup file that was created by the previous master and the easiest way to
       test it is when there is 1 master
    """

    backup_file = 'backup.tar'
    backup_dir = '/tmp'
    backup_url = 'file://{}/{}'.format(backup_dir, backup_file)

    # Deploy a simple test app. It is expected to be there after leader reelection
    app_def = apps.sleep_app()
    app_id = app_def['id']

    client = marathon.create_client()
    client.add_app(app_def)
    deployment_wait(service_id=app_id)

    app = client.get_app(app_id)
    assert app['tasksRunning'] == 1, "The number of running tasks is {}, but 1 was expected".format(app["tasksRunning"])
    task_id = app['tasks'][0]['id']

    # Abdicate the leader with backup and restore
    original_leader = marathon_leader_ip()
    print('leader: {}'.format(original_leader))
    params = '?backup={}&restore={}'.format(backup_url, backup_url)
    print('DELETE /v2/leader{}'.format(params))
    common.abdicate_marathon_leader(params)

    # Wait for new leader (but same master server) to be up and ready
    wait_for_service_endpoint(marathon_service_name, timedelta(minutes=5).total_seconds(), path="ping")
    app = client.get_app(app_id)
    assert app['tasksRunning'] == 1, "The number of running tasks is {}, but 1 was expected".format(app["tasksRunning"])
    assert task_id == app['tasks'][0]['id'], "Task has a different ID after restore"

    # Check if the backup file exits and is valid
    cmd = 'tar -tf {}/{} | wc -l'.format(backup_dir, backup_file)
    status, data = run_command_on_master(cmd)
    assert status, 'Failed to validate backup file {}'.format(backup_url)
    assert int(data.rstrip()) > 0, "Backup file is empty"


# Regression for MARATHON-7525, introduced in MARATHON-7538
@masters(3)
@pytest.mark.skipif('marathon_version_less_than("1.5")')
def test_marathon_backup_and_check_apps(marathon_service_name):

    backup_file1 = 'backup1.tar'
    backup_file2 = 'backup2.tar'
    backup_dir = '/tmp'

    for master_ip in get_all_master_ips():
        run_command(master_ip, "rm {}/{}".format(backup_dir, backup_file1))
        run_command(master_ip, "rm {}/{}".format(backup_dir, backup_file2))

    backup_url1 = 'file://{}/{}'.format(backup_dir, backup_file1)
    backup_url2 = 'file://{}/{}'.format(backup_dir, backup_file2)

    original_leader = marathon_leader_ip()
    print('leader: {}'.format(original_leader))

    app_def = apps.sleep_app()
    app_id = app_def['id']

    client = marathon.create_client()
    client.add_app(app_def)
    deployment_wait(service_id=app_id)

    app = client.get_app(app_id)
    assert app['tasksRunning'] == 1, "The number of running tasks is {}, but 1 was expected".format(app["tasksRunning"])

    # Abdicate the leader with backup
    original_leader = marathon_leader_ip()
    params = '?backup={}'.format(backup_url1)
    common.abdicate_marathon_leader(params)

    wait_for_service_endpoint(marathon_service_name, timedelta(minutes=5).total_seconds(), path="ping")

    # wait until leader changed
    common.assert_marathon_leadership_changed(original_leader)

    @retrying.retry(wait_fixed=1000, stop_max_attempt_number=30, retry_on_exception=common.ignore_exception)
    def check_app_existence(expected_instances):
        try:
            app = client.get_app(app_id)
        except Exception as e:
            if expected_instances != 0:
                raise e
        else:
            if expected_instances == 0:
                assert False, "The application resurrected"
            else:
                app['tasksRunning'] == expected_instances, \
                    "The number of running tasks is {}, but {} was expected".format(
                        app["tasksRunning"], expected_instances)

    # check if app definition is still there and one instance is still running after new leader was elected
    check_app_existence(1)

    # then remove
    client.remove_app(app_id)
    deployment_wait(service_id=app_id)

    check_app_existence(0)

    # Do a second backup. Before MARATHON-7525 we had the problem, that doing a backup after an app was deleted
    # leads to the state that marathon was not able to re-start, because the second backup failed constantly.

    # Abdicate the leader with backup
    original_leader = marathon_leader_ip()
    print('leader: {}'.format(original_leader))
    params = '?backup={}'.format(backup_url2)
    print('DELETE /v2/leader{}'.format(params))
    common.abdicate_marathon_leader(params)

    wait_for_service_endpoint(marathon_service_name, timedelta(minutes=5).total_seconds(), path="ping")

    # wait until leader changed
    # if leader changed, this means that marathon was able to start again, which is great :-).
    common.assert_marathon_leadership_changed(original_leader)

    # check if app definition is still not there and no instance is running after new leader was elected
    check_app_existence(0)


@common.marathon_1_5
@pytest.mark.skipif("ee_version() is None")
@pytest.mark.skipif("common.docker_env_not_set()")
def test_private_repository_mesos_app():
    """Deploys an app with a private Docker image, using Mesos containerizer.
        It relies on the global `install_enterprise_cli` fixture to install the
        enterprise-cli-package.
    """

    username = os.environ['DOCKER_HUB_USERNAME']
    password = os.environ['DOCKER_HUB_PASSWORD']

    secret_name = "pullconfig"
    secret_value_json = common.create_docker_pull_config_json(username, password)
    secret_value = json.dumps(secret_value_json)

    app_def = apps.private_ucr_docker_app()
    app_id = app_def["id"]

    # In strict mode all tasks are started as user `nobody` by default and `nobody`
    # doesn't have permissions to write to /var/log within the container.
    if is_strict():
        app_def['user'] = 'root'
        common.add_dcos_marathon_user_acls()

    common.create_secret(secret_name, secret_value)
    client = marathon.create_client()

    try:
        client.add_app(app_def)
        deployment_wait(service_id=app_id)

        common.assert_app_tasks_running(client, app_def)
    finally:
        common.delete_secret(secret_name)


@pytest.mark.skipif('marathon_version_less_than("1.5")')
@pytest.mark.skipif("ee_version() is None")
def test_app_file_based_secret(secret_fixture):

    secret_name, secret_value = secret_fixture
    secret_container_path = 'mysecretpath'

    app_id = '/app-fbs-{}'.format(uuid.uuid4().hex)
    # In case you're wondering about the `cmd`: secrets are mounted via tmpfs inside
    # the container and are not visible outside, hence the intermediate file
    app_def = {
        "id": app_id,
        "instances": 1,
        "cpus": 0.5,
        "mem": 64,
        "cmd": "cat {} >> {}_file && /opt/mesosphere/bin/python -m http.server $PORT_API".format(
            secret_container_path, secret_container_path),
        "container": {
            "type": "MESOS",
            "volumes": [{
                "containerPath": secret_container_path,
                "secret": "secret1"
            }]
        },
        "portDefinitions": [{
            "port": 0,
            "protocol": "tcp",
            "name": "api",
            "labels": {}
        }],
        "secrets": {
            "secret1": {
                "source": secret_name
            }
        }
    }

    client = marathon.create_client()
    client.add_app(app_def)
    deployment_wait(service_id=app_id)

    tasks = client.get_tasks(app_id)
    assert len(tasks) == 1, 'Failed to start the file based secret app'

    port = tasks[0]['ports'][0]
    host = tasks[0]['host']
    # The secret by default is saved in $MESOS_SANDBOX/.secrets/path/to/secret
    cmd = "curl {}:{}/{}_file".format(host, port, secret_container_path)

    @retrying.retry(wait_fixed=1000, stop_max_attempt_number=30, retry_on_exception=common.ignore_exception)
    def value_check():
        status, data = run_command_on_master(cmd)
        assert status, "{} did not succeed. status = {}, data = {}".format(cmd, status, data)
        assert data.rstrip() == secret_value, "Got an unexpected secret data"

    value_check()


@dcos_1_9
@pytest.mark.skipif("ee_version() is None")
def test_app_secret_env_var(secret_fixture):

    secret_name, secret_value = secret_fixture

    app_id = '/app-secret-env-var-{}'.format(uuid.uuid4().hex)
    app_def = {
        "id": app_id,
        "instances": 1,
        "cpus": 0.5,
        "mem": 64,
        "cmd": "echo $SECRET_ENV >> $MESOS_SANDBOX/secret-env && /opt/mesosphere/bin/python -m http.server $PORT_API",
        "env": {
            "SECRET_ENV": {
                "secret": "secret1"
            }
        },
        "portDefinitions": [{
            "port": 0,
            "protocol": "tcp",
            "name": "api",
            "labels": {}
        }],
        "secrets": {
            "secret1": {
                "source": secret_name
            }
        }
    }

    client = marathon.create_client()
    client.add_app(app_def)
    deployment_wait(service_id=app_id)

    tasks = client.get_tasks(app_id)
    assert len(tasks) == 1, 'Failed to start the secret environment variable app'

    port = tasks[0]['ports'][0]
    host = tasks[0]['host']
    cmd = "curl {}:{}/secret-env".format(host, port)

    @retrying.retry(wait_fixed=1000, stop_max_attempt_number=30, retry_on_exception=common.ignore_exception)
    def value_check():
        status, data = run_command_on_master(cmd)
        assert status, "{} did not succeed".format(cmd)
        assert data.rstrip() == secret_value

    value_check()


@dcos_1_9
@pytest.mark.skipif("ee_version() is None")
def test_app_inaccessible_secret_env_var():

    secret_name = '/some/secret'    # Secret in an inaccessible namespace

    app_id = '/app-inaccessible-secret-env-var-{}'.format(uuid.uuid4().hex)
    app_def = {
        "id": app_id,
        "instances": 1,
        "cpus": 0.1,
        "mem": 64,
        "cmd": "echo \"shouldn't be called anyway\"",
        "env": {
            "SECRET_ENV": {
                "secret": "secret1"
            }
        },
        "portDefinitions": [{
            "port": 0,
            "protocol": "tcp",
            "name": "api",
            "labels": {}
        }],
        "secrets": {
            "secret1": {
                "source": secret_name
            }
        }
    }

    client = marathon.create_client()

    with pytest.raises(requests.HTTPError) as excinfo:
        client.add_app(app_def)

    print('An app with an inaccessible secret could not be deployed because: {}'.format(excinfo.value))
    assert excinfo.value.response.status_code == 422
    assert 'Secret {} is not accessible'.format(secret_name) in excinfo.value.response.text


@dcos_1_9
@pytest.mark.skipif("ee_version() is None")
def test_pod_inaccessible_secret_env_var():

    secret_name = '/some/secret'    # Secret in an inaccessible namespace

    pod_id = '/pod-inaccessible-secret-env-var-{}'.format(uuid.uuid4().hex)
    pod_def = {
        "id": pod_id,
        "containers": [{
            "name": "container-1",
            "resources": {
                "cpus": 0.1,
                "mem": 64
            },
            "exec": {
                "command": {
                    "shell": "echo \"shouldn't be called anyway\""
                }
            }
        }],
        "environment": {
            "SECRET_ENV": {
                "secret": "secret1"
            }
        },
        "networks": [{
            "mode": "host"
        }],
        "secrets": {
            "secret1": {
                "source": secret_name
            }
        }
    }

    client = marathon.create_client()

    with pytest.raises(requests.HTTPError) as excinfo:
        client.add_pod(pod_def)

    print('A pod with an inaccessible secret could not be deployed because: {}'.format(excinfo.value))
    assert excinfo.value.response.status_code == 422
    assert 'Secret {} is not accessible'.format(secret_name) in excinfo.value.response.text


@dcos_1_9
@pytest.mark.skipif("ee_version() is None")
def test_pod_secret_env_var(secret_fixture):

    secret_name, secret_value = secret_fixture

    pod_id = '/pod-secret-env-var-{}'.format(uuid.uuid4().hex)
    pod_def = {
        "id": pod_id,
        "containers": [{
            "name": "container-1",
            "resources": {
                "cpus": 0.5,
                "mem": 64
            },
            "endpoints": [{
                "name": "http",
                "hostPort": 0,
                "protocol": [
                    "tcp"
                ]}
            ],
            "exec": {
                "command": {
                    "shell": "echo $SECRET_ENV && "
                             "echo $SECRET_ENV >> $MESOS_SANDBOX/secret-env && "
                             "/opt/mesosphere/bin/python -m http.server $ENDPOINT_HTTP"
                }
            }
        }],
        "environment": {
            "SECRET_ENV": {
                "secret": "secret1"
            }
        },
        "networks": [{
            "mode": "host"
        }],
        "secrets": {
            "secret1": {
                "source": secret_name
            }
        }
    }

    client = marathon.create_client()
    client.add_pod(pod_def)
    deployment_wait(service_id=pod_id)

    instances = client.show_pod(pod_id)['instances']
    assert len(instances) == 1, 'Failed to start the secret environment variable pod'

    port = instances[0]['containers'][0]['endpoints'][0]['allocatedHostPort']
    host = instances[0]['networks'][0]['addresses'][0]
    cmd = "curl {}:{}/secret-env".format(host, port)

    @retrying.retry(wait_fixed=1000, stop_max_attempt_number=30, retry_on_exception=common.ignore_exception)
    def value_check():
        status, data = run_command_on_master(cmd)
        assert status, "{} did not succeed. status = {}, data = {}".format(cmd, status, data)
        assert data.rstrip() == secret_value, "Got an unexpected secret data"

    value_check()


@pytest.mark.skipif('marathon_version_less_than("1.5")')
@pytest.mark.skipif("ee_version() is None")
def test_pod_file_based_secret(secret_fixture):
    secret_name, secret_value = secret_fixture
    secret_normalized_name = secret_name.replace('/', '')

    pod_id = '/pod-fbs-{}'.format(uuid.uuid4().hex)

    pod_def = {
        "id": pod_id,
        "containers": [{
            "name": "container-1",
            "resources": {
                "cpus": 0.5,
                "mem": 64
            },
            "endpoints": [{
                "name": "http",
                "hostPort": 0,
                "protocol": [
                    "tcp"
                ]}
            ],
            "exec": {
                "command": {
                    "shell": "cat {} >> {}_file && /opt/mesosphere/bin/python -m http.server $ENDPOINT_HTTP".format(
                        secret_normalized_name, secret_normalized_name),
                }
            },
            "volumeMounts": [{
                "name": "vol",
                "mountPath": './{}'.format(secret_name)
            }],
        }],
        "networks": [{
            "mode": "host"
        }],
        "volumes": [{
            "name": "vol",
            "secret": "secret1"
        }],
        "secrets": {
            "secret1": {
                "source": secret_name
            }
        }
    }

    client = marathon.create_client()
    client.add_pod(pod_def)
    deployment_wait(service_id=pod_id)

    instances = client.show_pod(pod_id)['instances']
    assert len(instances) == 1, 'Failed to start the file based secret pod'

    port = instances[0]['containers'][0]['endpoints'][0]['allocatedHostPort']
    host = instances[0]['networks'][0]['addresses'][0]
    cmd = "curl {}:{}/{}_file".format(host, port, secret_normalized_name)

    @retrying.retry(wait_fixed=1000, stop_max_attempt_number=30, retry_on_exception=common.ignore_exception)
    def value_check():
        status, data = run_command_on_master(cmd)
        assert status, "{} did not succeed. status = {}, data = {}".format(cmd, status, data)
        assert data.rstrip() == secret_value, "Got an unexpected secret data"

    value_check()


# Uncomment to run a quick and sure-to-pass SI test on any cluster. Useful for running SI tests locally
# from fixtures import parent_group
# def test_foo(parent_group):
#     client = marathon.create_client()
#     app_def = apps.sleep_app(parent_group=parent_group)
#     app_id = app_def['id']
#     client.add_app(app_def)
#      deployment_wait(service_id=app_id)
#
#     tasks = client.get_tasks(app_id)
#     assert len(tasks) == 1, 'Failed to start a simple sleep app'


@pytest.fixture(scope="function")
def secret_fixture():
    secret_name = '/mysecret'
    secret_value = 'super_secret_password'
    common.create_secret(secret_name, secret_value)
    yield secret_name, secret_value
    common.delete_secret(secret_name)
