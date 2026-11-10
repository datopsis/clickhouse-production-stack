"""Generate disposable secrets only in a private temporary directory."""
import json
import os
from pathlib import Path
import secrets
import subprocess
import yaml

ROOT = Path(__file__).resolve().parents[1]


def openssl(*args):
    subprocess.run(['openssl', *map(str, args)], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def fixture(path, image):
    path = Path(path)
    os.chmod(path, 0o700)
    defaults = yaml.safe_load((ROOT / 'ansible/vars/defaults.yml').read_text())
    openssl('req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1', '-subj', '/CN=Disposable stack CI CA',
            '-keyout', path / 'ca.key', '-out', path / 'ca.crt',
            '-addext', 'basicConstraints=critical,CA:TRUE', '-addext', 'keyUsage=critical,keyCertSign,cRLSign')
    secret = {key: secrets.token_hex(32) for key in ['admin_password', 'reader_password', 'interserver_password', 'cluster_secret']}
    secret['keeper_identity'] = 'stack:' + secrets.token_hex(32)
    secret['ca_cert'] = (path / 'ca.crt').read_text()
    secret['nodes'] = {}
    addresses = {node['name']: f'172.30.243.{10 + i}' for i, node in enumerate(defaults['stack_nodes'])}
    for node in defaults['stack_nodes']:
        name = node['name']
        openssl('req', '-newkey', 'rsa:2048', '-nodes', '-subj', f'/CN={name}',
                '-keyout', path / f'{name}.key', '-out', path / f'{name}.csr')
        ext = path / f'{name}.ext'
        ext.write_text(f'subjectAltName=DNS:{name},IP:{addresses[name]}\nextendedKeyUsage=serverAuth,clientAuth\n')
        openssl('x509', '-req', '-in', path / f'{name}.csr', '-CA', path / 'ca.crt', '-CAkey', path / 'ca.key',
                '-CAcreateserial', '-days', '1', '-extfile', ext, '-out', path / f'{name}.crt')
        openssl('verify', '-x509_strict', '-purpose', 'sslserver', '-verify_hostname', name,
                '-CAfile', path / 'ca.crt', path / f'{name}.crt')
        openssl('verify', '-x509_strict', '-purpose', 'sslclient', '-CAfile', path / 'ca.crt', path / f'{name}.crt')
        openssl('verify', '-x509_strict', '-verify_ip', addresses[name], '-CAfile', path / 'ca.crt', path / f'{name}.crt')
        secret['nodes'][name] = {'cert': (path / f'{name}.crt').read_text(), 'key': (path / f'{name}.key').read_text()}
    variables = defaults | {'stack_root': str(path), 'stack_image': image, 'stack_secrets': secret,
                            'stack_node_ips': addresses, 'stack_compose_subnet': '172.30.243.0/24'}
    variable_file = path / 'vars.json'
    variable_file.write_text(json.dumps(variables))
    os.chmod(variable_file, 0o600)
    subprocess.run(['ansible-playbook', 'tests/render.yml', '-e', '@' + str(variable_file)], cwd=ROOT, check=True)
    return variables
