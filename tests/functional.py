#!/usr/bin/env python3
"""Native rootful Podman integration; never contacts AWS."""
import json
import os
from pathlib import Path
import socket
import ssl
import subprocess
import sys
import tempfile
import time

from fixture import fixture, ROOT
sys.path.insert(0, str(ROOT / 'scripts'))
from verify import Cluster  # noqa: E402


def run(*args, **kwargs):
    return subprocess.run(args, check=True, text=True, **kwargs)


def retry(fn, seconds=180):
    deadline = time.monotonic() + seconds
    while True:
        try:
            return fn()
        except (OSError, RuntimeError, AssertionError):
            if time.monotonic() >= deadline:
                raise
            time.sleep(2)


def must_fail(fn):
    try:
        fn()
    except (OSError, RuntimeError):
        return
    raise AssertionError('Insecure request unexpectedly succeeded')


def main():
    if os.geteuid() != 0:
        raise SystemExit('Run with sudo and the locked Python environment; production also uses rootful Podman with non-root containers.')
    image = sys.argv[1]
    with tempfile.TemporaryDirectory(prefix='stack-functional-') as directory:
        path = Path(directory)
        variables = fixture(path, image)
        nodes = variables['stack_nodes']
        compose = ['podman-compose', '-p', 'stack-test', '-f', str(path / 'compose.yml')]
        for node in nodes:
            run('chown', '-R', '101:0', str(path / node['name']))
        try:
            run(*compose, 'up', '-d')
            def address(name):
                result = run('podman', 'inspect', f'clickhouse-{name}', capture_output=True)
                network = json.loads(result.stdout)[0]['NetworkSettings']['Networks']
                return next(iter(network.values()))['IPAddress']
            config = {'nodes': nodes, 'addresses': {n['name']: address(n['name']) for n in nodes},
                      'ca': str(path / 'ca.crt'), 'cert': str(path / 'replica1.crt'), 'key': str(path / 'replica1.key'),
                      'admin_password': variables['stack_secrets']['admin_password'],
                      'reader_password': variables['stack_secrets']['reader_password']}
            cluster = Cluster(config)
            retry(cluster.quorum)
            retry(cluster.read_only)
            print('PASS: three-node Keeper quorum and authenticated distributed read-only queries', flush=True)
            # Confirm runtime hardening rather than just trusting the YAML.
            for node in nodes:
                name = 'clickhouse-' + node['name']
                info = json.loads(run('podman', 'inspect', name, capture_output=True).stdout)[0]
                assert info['Config']['User'] == '101:0'
                assert info['HostConfig']['ReadonlyRootfs']
                status = run('podman', 'exec', name, 'cat', '/proc/1/status', capture_output=True).stdout
                assert 'Uid:\t101\t101\t101\t101' in status
                assert 'CapEff:\t0000000000000000' in status
                assert 'NoNewPrivs:\t1' in status
            # Plaintext, missing certificate, unknown CA and wrong passwords must fail.
            for port in [8123, 9000, 9009]:
                must_fail(lambda: socket.create_connection((config['addresses']['replica1'], port), timeout=3))
            must_fail(lambda: socket.create_connection((config['addresses']['keeper1'], 9181), timeout=3))
            must_fail(lambda: cluster.query('replica1', 'SELECT 1', password=''))
            must_fail(lambda: cluster.query('replica1', 'CREATE TABLE default.denied (x UInt8) ENGINE=Memory', reader=True))
            original = cluster.context
            cluster.context = ssl.create_default_context(cafile=config['ca'])
            must_fail(lambda: cluster.query('replica1', 'SELECT 1'))
            cluster.context = ssl.create_default_context()
            cluster.context.load_cert_chain(config['cert'], config['key'])
            must_fail(lambda: cluster.query('replica1', 'SELECT 1'))
            cluster.context = original
            print('PASS: hardening, TLS and authentication negative tests', flush=True)
            table = cluster.create_probe()
            try:
                cluster.query('replica1', f'INSERT INTO {table} VALUES (1)')
                cluster.sync('replica2', table)
                assert cluster.query('replica2', f'SELECT sum(value) FROM {table}', reader=True) == '1'
                run('podman', 'stop', '-t', '30', 'clickhouse-replica1')
                assert cluster.query('replica2', f'SELECT sum(value) FROM {table}', reader=True) == '1'
                cluster.query('replica2', f'INSERT INTO {table} VALUES (2)')
                assert cluster.query('replica2', f'SELECT sum(value) FROM {table}', reader=True) == '3'
                run('podman', 'start', 'clickhouse-replica1')
                retry(lambda: cluster.query('replica1', 'SELECT 1', reader=True))
                cluster.sync('replica1', table)
                assert cluster.query('replica1', f'SELECT sum(value) FROM {table}', reader=True) == '3'
                print('PASS: replication, replica failure, survivor reads/writes and recovery', flush=True)
                run('podman', 'stop', '-t', '30', 'clickhouse-keeper1')
                cluster.keepers.remove('keeper1')
                retry(cluster.quorum)
                cluster.query('replica2', f'INSERT INTO {table} VALUES (3)')
                cluster.sync('replica1', table)
                assert cluster.query('replica1', f'SELECT sum(value) FROM {table}', reader=True) == '6'
                run('podman', 'start', 'clickhouse-keeper1')
                cluster.keepers.append('keeper1')
                retry(cluster.quorum)
                print('PASS: Keeper quorum survives one Keeper failure', flush=True)
            finally:
                cluster.drop_probe(table)
            before = {n['name']: json.loads(run('podman', 'inspect', 'clickhouse-' + n['name'], capture_output=True).stdout)[0]['Id'] for n in nodes}
            run(*compose, 'up', '-d')
            after = {n['name']: json.loads(run('podman', 'inspect', 'clickhouse-' + n['name'], capture_output=True).stdout)[0]['Id'] for n in nodes}
            assert before == after, 'Unchanged Compose deployment restarted containers'
            retry(cluster.read_only)
            print('PASS: unchanged Compose rerun preserves all containers', flush=True)
        except BaseException:
            # Only server logs; configuration files and generated credentials are never uploaded.
            run(*compose, 'logs', '--tail', '80')
            raise
        finally:
            run(*compose, 'down', '--volumes')


if __name__ == '__main__':
    main()
