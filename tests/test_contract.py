"""Rendered security and topology contracts, using the real Ansible templates."""
import hashlib
import json
from pathlib import Path
import tempfile
import subprocess
import unittest
import xml.etree.ElementTree as ET

import yaml
from fixture import fixture, ROOT


class Contract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='stack-contract-')
        cls.path = Path(cls.temp.name)
        # Test-only content-derived identifier; never a deployable image reference.
        cls.image = 'localhost/contract-fixture@sha256:' + hashlib.sha256(b'contract fixture').hexdigest()
        cls.variables = fixture(cls.path, cls.image)
        cls.compose = yaml.safe_load((cls.path / 'compose.yml').read_text())

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_hardened_five_node_topology(self):
        services = self.compose['services']
        self.assertEqual(len(services), 5)
        for name, service in services.items():
            self.assertEqual(service['image'], self.image)
            self.assertEqual(service['user'], '101:0')
            self.assertTrue(service['read_only'])
            self.assertEqual(service['cap_drop'], ['ALL'])
            self.assertEqual(service['security_opt'], ['no-new-privileges:true'])
            self.assertEqual(service['command'][1], 'keeper' if name.startswith('keeper') else 'server')
            self.assertIn(':ro,Z', service['volumes'][0])

    def test_tls_only_mutually_authenticated(self):
        for node in self.variables['stack_nodes']:
            xml = ET.parse(self.path / node['name'] / 'config/config.xml').getroot()
            for direction in ['client', 'server']:
                self.assertEqual(xml.findtext(f'openSSL/{direction}/verificationMode'), 'strict')
                self.assertEqual(xml.findtext(f'openSSL/{direction}/invalidCertificateHandler/name'), 'RejectCertificateHandler')
            self.assertEqual(xml.findtext('openSSL/client/extendedVerification'), 'true')
            self.assertIsNone(xml.find('openSSL/server/extendedVerification'))
            for port in ['tcp_port', 'http_port', 'interserver_http_port', 'keeper_server/tcp_port']:
                self.assertIsNone(xml.find(port))
            if node['role'] == 'keeper':
                self.assertEqual(xml.findtext('keeper_server/raft_configuration/secure'), 'true')
                self.assertEqual(len(xml.findall('keeper_server/raft_configuration/server')), 3)
            else:
                self.assertEqual(len(xml.findall('remote_servers/stack/shard/replica')), 2)
                self.assertEqual(len(xml.findall('zookeeper/node')), 3)
                self.assertTrue(all(n.findtext('secure') == '1' for n in xml.findall('zookeeper/node')))
                self.assertEqual(xml.findtext('interserver_http_credentials/allow_empty'), 'false')

    def test_no_default_user_or_cleartext_user_password(self):
        xml = ET.parse(self.path / 'replica1/config/users.xml').getroot()
        self.assertIsNone(xml.find('users/default'))
        for user in ['stack_admin', 'stack_reader']:
            self.assertIsNone(xml.find(f'users/{user}/password'))
            self.assertEqual(len(xml.findtext(f'users/{user}/password_sha256_hex')), 64)
        self.assertEqual(xml.findtext('profiles/reader/readonly'), '1')

    def test_input_guards(self):
        base = self.variables | yaml.safe_load((ROOT / 'tests/provision-vars.yml').read_text())
        variants = [({}, True), ({'stack_image': 'ghcr.io/datopsis/clickhouse-ubi:latest'}, False),
                    ({'stack_image': None}, False), ({'stack_shards': 2}, False),
                    ({'stack_secrets': {}}, False)]
        for override, accepted in variants:
            args = self.path / 'validation.json'
            args.write_text(json.dumps(base | override))
            result = subprocess.run(['ansible-playbook', 'tests/validate.yml', '--check', '-e', '@' + str(args)],
                                    cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(result.returncode == 0, accepted, 'Input guard returned an unexpected outcome')

    def test_rolling_and_no_aws_credentials_in_workflow(self):
        deploy = yaml.safe_load((ROOT / 'ansible/playbooks/deploy.yml').read_text())
        for play in deploy:
            if play.get('hosts') in ['keeper', 'clickhouse']:
                self.assertEqual(play['serial'], 1)
                self.assertTrue(play['any_errors_fatal'])
        workflow = (ROOT / '.github/workflows/ci.yml').read_text()
        self.assertNotIn('configure-aws-credentials', workflow)
        self.assertNotIn('pull_request_target', workflow)
        self.assertIn('persist-credentials: false', workflow)
        requirements = (ROOT / '.github/requirements/tools.txt').read_text()
        self.assertIn('filelock==4.2.9', requirements)


if __name__ == '__main__':
    unittest.main()
