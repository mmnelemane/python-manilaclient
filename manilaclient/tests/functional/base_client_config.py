# Copyright 2014 Mirantis Inc.
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import traceback

from oslo_log import log
# import tempest_lib.cli.base
import os_client_config

from tempest_lib import exceptions as lib_exc
import testtools
import fixtures
import os

import manilaclient
# from manilaclient import config
from manilaclient.tests.functional import client
# from manilaclient.tests.functional import utils

# CONF = config.CONF
LOG = log.getLogger(__name__)


class handle_cleanup_exceptions(object):
    """Handle exceptions raised with cleanup operations.

    Always suppress errors when lib_exc.NotFound or lib_exc.Forbidden
    are raised.
    Suppress all other exceptions only in case config opt
    'suppress_errors_in_cleanup' is True.
    """

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, exc_traceback):
        if not (isinstance(exc_value,
            (lib_exc.NotFound, lib_exc.Forbidden))):
            return False  # Do not suppress error if any
        if exc_traceback:
            LOG.error("Suppressed cleanup error: "
                      "\n%s" % traceback.format_exc())
        return True  # Suppress error if any

class NoCloudConfigException(Exception):
    """We couldn't find a cloud configuration."""
    pass

class ClientTestBase(testtools.TestCase):
    """Base test class for read only python-manilaclient commands.

    This is a first pass at a simple read only python-manilaclient test. This
    only exercises client commands that are read only.

    This should test commands:
    * as a regular user
    * as a admin user
    * with and without optional parameters
    * initially just check return codes, and later test command outputs

    """
    MANILA_API_VERSION = None

    log_format = ('%(asctime)s %(process)d %(levelname)-8s '
                  '[%(name)s] %(message)s')
    client = None

    def setUp(self):
        super(ClientTestBase, self).setUp()

        test_timeout = os.environ.get('OS_TEST_TIMEOUT', 0)
        try:
            test_timeout = int(test_timeout)
        except ValueError:
            test_timeout = 0
        if test_timeout > 0:
            self.useFixture(fixtures.Timeout(test_timeout, gentle=True))

        if (os.environ.get('OS_STDOUT_CAPTURE') == 'True' or
                os.environ.get('OS_STDOUT_CAPTURE') == '1'):
            stdout = self.useFixture(fixtures.StringStream('stdout')).stream
            self.useFixture(fixtures.MonkeyPatch('sys.stdout', stdout))
        if (os.environ.get('OS_STDERR_CAPTURE') == 'True' or
                os.environ.get('OS_STDERR_CAPTURE') == '1'):
            stderr = self.useFixture(fixtures.StringStream('stderr')).stream
            self.useFixture(fixtures.MonkeyPatch('sys.stderr', stderr))

        if (os.environ.get('OS_LOG_CAPTURE') != 'False' and
                os.environ.get('OS_LOG_CAPTURE') != '0'):
            self.useFixture(fixtures.LoggerFixture(nuke_handlers=False,
                                                   format=self.log_format,
                                                   level=None))

        # Collecting of credentials:
        #
        # Grab the cloud config from a user's clouds.yaml file.
        # First look for a functional_admin cloud, as this is a cloud
        # that the user may have defined for functional testing that has
        # admin credentials.
        # If that is not found, get the devstack config and override the
        # username and project_name to be admin so that admin credentials
        # will be used.
        #
        # Finally, fall back to looking for environment variables to support
        # existing users running these the old way. We should deprecate that
        # as tox 2.0 blanks out environment.
        #
        # TODO(sdague): while we collect this information in
        # tempest-lib, we do it in a way that's not available for top
        # level tests. Long term this probably needs to be in the base
        # class.
        openstack_config = os_client_config.config.OpenStackConfig()
        try:
            cloud_config = openstack_config.get_one_cloud('functional_admin')
        except os_client_config.exceptions.OpenStackConfigException:
            try:
                cloud_config = openstack_config.get_one_cloud(
                    'devstack', auth=dict(
                        username='admin', project_name='admin'))
            except os_client_config.exceptions.OpenStackConfigException:
                try:
                    cloud_config = openstack_config.get_one_cloud('envvars')
                except os_client_config.exceptions.OpenStackConfigException:
                    cloud_config = None

        if cloud_config is None:
            raise NoCloudConfigException(
                "Could not find a cloud named functional_admin or a cloud"
                " named devstack. Please check your clouds.yaml file and"
                " try again.")
        auth_info = cloud_config.config['auth']

        user = auth_info['username']
        passwd = auth_info['password']
        tenant = auth_info['project_name']
        auth_url = auth_info['auth_url']

        if self.MANILA_API_VERSION == "2.latest":
            version = manilaclient.API_MAX_VERSION.get_string()
        else:
            version = self.MANILA_API_VERSION or "2"
        self.client = manilaclient.client.Client(
            version, user, passwd, tenant,
            auth_url=auth_url)
        self.__class__.client = self.client

        # create a CLI client in case we'd like to do CLI
        # testing. tempest_lib does this really weird thing where it
        # builds a giant factory of all the CLIs that it knows
        # about. Eventually that should really be unwound into
        # something more sensible.
        cli_dir = os.environ.get(
            'OS_MANILACLIENT_EXEC_DIR',
            os.path.join(os.path.abspath('.'), '.tox/functional/bin'))

        self.cli_clients = client.ManilaCLIClient(
            username=user,
            password=passwd,
            tenant_name=tenant,
            uri=auth_url,
            cli_dir=cli_dir)

    def manila(self, action, flags='', params='', fail_ok=False,
             endpoint_type='publicURL', merge_stderr=False):
        if self.MANILA_API_VERSION:
            flags += " --os-manila-api-version %s " % self.MANILA_API_VERSION
        return self.cli_clients.manila(action, flags, params, fail_ok,
                                     endpoint_type, merge_stderr)

class BaseTestCase(ClientTestBase):

    # Will be cleaned up after test suite run
    class_resources = []

    # Will be cleaned up after single test run
    method_resources = []

    def setUp(self):
        super(BaseTestCase, self).setUp()
        self.addCleanup(self.clear_resources)

    @classmethod
    def tearDownClass(cls):
        super(BaseTestCase, cls).tearDownClass()
        cls.clear_resources(cls.class_resources)

    @classmethod
    def clear_resources(cls, resources=None):
        """Deletes resources, that were created in test suites.

        This method tries to remove resources from resource list,
        if it is not found, assume it was deleted in test itself.
        It is expected, that all resources were added as LIFO
        due to restriction of deletion resources, that are in the chain.
        :param resources: dict with keys 'type','id','client' and 'deleted'
        """

        if resources is None:
            resources = cls.method_resources
        for res in resources:
            if "deleted" not in res:
                res["deleted"] = False
            if "client" not in res:
                res["client"] = cls.get_cleanup_client()
            if not(res["deleted"]):
                res_id = res["id"]
                client = res["client"]
                with handle_cleanup_exceptions():
                    # TODO(vponomaryov): add support for other resources
                    if res["type"] is "share_type":
                        client.delete_share_type(
                            res_id, microversion=res["microversion"])
                        client.wait_for_share_type_deletion(
                            res_id, microversion=res["microversion"])
                    elif res["type"] is "share_network":
                        client.delete_share_network(
                            res_id, microversion=res["microversion"])
                        client.wait_for_share_network_deletion(
                            res_id, microversion=res["microversion"])
                    elif res["type"] is "share":
                        client.delete_share(
                            res_id, microversion=res["microversion"])
                        client.wait_for_share_deletion(
                            res_id, microversion=res["microversion"])
                    else:
                        LOG.warn("Provided unsupported resource type for "
                                 "cleanup '%s'. Skipping." % res["type"])
                res["deleted"] = True

    """
    @classmethod
    def get_admin_client(cls):
        manilaclient = client.ManilaCLIClient(
            username=CONF.admin_username,
            password=CONF.admin_password,
            tenant_name=CONF.admin_tenant_name,
            uri=CONF.admin_auth_url or CONF.auth_url,
            cli_dir=CONF.manila_exec_dir)
        # Set specific for admin project share network
        manilaclient.share_network = CONF.admin_share_network
        return manilaclient

    @classmethod
    def get_user_client(cls):
        manilaclient = client.ManilaCLIClient(
            username=CONF.username,
            password=CONF.password,
            tenant_name=CONF.tenant_name,
            uri=CONF.auth_url,
            cli_dir=CONF.manila_exec_dir)
        # Set specific for user project share network
        manilaclient.share_network = CONF.share_network
        return manilaclient

    @property
    def admin_client(self):
        if not hasattr(self, '_admin_client'):
            self._admin_client = self.get_admin_client()
        return self._admin_client

    @property
    def user_client(self):
        if not hasattr(self, '_user_client'):
            self._user_client = self.get_user_client()
        return self._user_client

    def _get_clients(self):
        return {'admin': self.admin_client, 'user': self.user_client}

    def skip_if_microversion_not_supported(self, microversion):
        if not utils.is_microversion_supported(microversion):
            raise self.skipException(
                "Microversion '%s' is not supported." % microversion)
    """

    @classmethod
    def create_share_type(cls, name=None, driver_handles_share_servers=True,
                          snapshot_support=True, is_public=True, client=None,
                          cleanup_in_class=True, microversion=None):
        if client is None:
            client = cls.client
        share_type = client.create_share_type(
            name=name,
            driver_handles_share_servers=driver_handles_share_servers,
            snapshot_support=snapshot_support,
            is_public=is_public,
            microversion=microversion,
        )
        resource = {
            "type": "share_type",
            "id": share_type["ID"],
            "client": client,
            "microversion": microversion,
        }
        if cleanup_in_class:
            cls.class_resources.insert(0, resource)
        else:
            cls.method_resources.insert(0, resource)
        return share_type

    @classmethod
    def create_share_network(cls, name=None, description=None,
                             nova_net_id=None, neutron_net_id=None,
                             neutron_subnet_id=None, client=None,
                             cleanup_in_class=True, microversion=None):
        if client is None:
            client = cls.client
        share_network = client.create_share_network(
            name=name,
            description=description,
            nova_net_id=nova_net_id,
            neutron_net_id=neutron_net_id,
            neutron_subnet_id=neutron_subnet_id,
            microversion=microversion,
        )
        resource = {
            "type": "share_network",
            "id": share_network["id"],
            "client": client,
            "microversion": microversion,
        }
        if cleanup_in_class:
            cls.class_resources.insert(0, resource)
        else:
            cls.method_resources.insert(0, resource)
        return share_network

    @classmethod
    def create_share(cls, share_protocol=None, size=None, share_network=None,
                     share_type=None, name=None, description=None,
                     public=False, snapshot=None, metadata=None,
                     client=None, cleanup_in_class=False,
                     wait_for_creation=True, microversion=None):
        if client is None:
            client = cls.client
        data = {
            'share_protocol': share_protocol or client.share_protocol,
            'size': size or 1,
            'name': name,
            'description': description,
            'public': public,
            'snapshot': snapshot,
            'metadata': metadata,
            'microversion': microversion,
        }
        share_network = share_network or client.share_network
        share_type = share_type # or CONF.share_type
        if share_network:
            data['share_network'] = share_network
        if share_type:
            data['share_type'] = share_type
        share = client.create_share(**data)
        resource = {
            "type": "share",
            "id": share["id"],
            "client": client,
            "microversion": microversion,
        }
        if cleanup_in_class:
            cls.class_resources.insert(0, resource)
        else:
            cls.method_resources.insert(0, resource)
        if wait_for_creation:
            client.wait_for_share_status(share['id'], 'available')
        return share

    @classmethod
    def create_security_service(cls, type='ldap', name=None, description=None,
                                dns_ip=None, server=None, domain=None,
                                user=None, password=None, client=None,
                                cleanup_in_class=False, microversion=None):
        if client is None:
            client = cls.client
        data = {
            'type': type,
            'name': name,
            'description': description,
            'user': user,
            'password': password,
            'server': server,
            'domain': domain,
            'dns_ip': dns_ip,
            'microversion': microversion,
        }
        ss = client.create_security_service(**data)
        resource = {
            "type": "share",
            "id": ss["id"],
            "client": client,
            "microversion": microversion,
        }
        if cleanup_in_class:
            cls.class_resources.insert(0, resource)
        else:
            cls.method_resources.insert(0, resource)
        return ss
