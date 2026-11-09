#!/usr/bin/env python3
"""TLS verification shared by operator playbooks and the native Podman test."""
import argparse
import base64
import http.client
import json
import socket
import ssl
import uuid


class Cluster:
    def __init__(self, config):
        self.config = config
        self.context = ssl.create_default_context(cafile=config['ca'])
        self.context.minimum_version = ssl.TLSVersion.TLSv1_2
        self.context.load_cert_chain(config['cert'], config['key'])
        self.keepers = [n['name'] for n in config['nodes'] if n['role'] == 'keeper']
        self.replicas = [n['name'] for n in config['nodes'] if n['role'] == 'clickhouse']

    def connect(self, name, port):
        sock = socket.create_connection((self.config['addresses'][name], port), timeout=10)
        return self.context.wrap_socket(sock, server_hostname=name)

    def query(self, name, sql, reader=False, password=None):
        user = 'stack_reader' if reader else 'stack_admin'
        password = password if password is not None else self.config['reader_password' if reader else 'admin_password']
        auth = base64.b64encode(f'{user}:{password}'.encode()).decode()
        connection = http.client.HTTPConnection(name, 8443, timeout=30)
        connection.sock = self.connect(name, 8443)
        try:
            connection.request('POST', '/', body=sql.encode(), headers={'Authorization': f'Basic {auth}'})
            response = connection.getresponse()
            body = response.read().decode()
            if response.status != 200:
                # Never print server exception bodies: they may contain query/secret data.
                raise RuntimeError(f'{name}: query rejected (HTTP {response.status})')
            return body.strip()
        finally:
            connection.close()

    def quorum(self):
        states = []
        for name in self.keepers:
            with self.connect(name, 9281) as sock:
                sock.sendall(b'mntr')
                result = b''
                while chunk := sock.recv(65536):
                    result += chunk
            fields = dict(line.split('\t', 1) for line in result.decode().splitlines() if '\t' in line)
            states.append(fields.get('zk_server_state'))
        assert states.count('leader') == 1, f'Expected one Keeper leader: {states}'
        assert states.count('follower') == len(self.keepers) - 1, f'Keeper not joined: {states}'

    def read_only(self):
        for name in self.replicas:
            assert self.query(name, 'SELECT 1', reader=True) == '1'
            assert self.query(name, "SELECT count() FROM cluster('stack', system.one)", reader=True) == str(len(self.replicas))
            unhealthy = self.query(name, 'SELECT count() FROM system.replicas WHERE is_readonly OR is_session_expired '
                                   'OR active_replicas < total_replicas OR queue_size > 0', reader=True)
            assert unhealthy == '0', f'{name}: replication is degraded or has pending work'

    def create_probe(self):
        table = 'default.stack_probe_' + uuid.uuid4().hex
        for name in self.replicas:
            self.query(name, f"CREATE TABLE {table} (value UInt64) ENGINE=ReplicatedMergeTree('/stack-verification/{table}', "
                             "'{replica}') ORDER BY value")
        return table

    def sync(self, name, table):
        self.query(name, f'SYSTEM SYNC REPLICA {table}')

    def drop_probe(self, table):
        for name in self.replicas:
            self.query(name, f'DROP TABLE IF EXISTS {table} SYNC')

    def replication(self):
        table = self.create_probe()
        try:
            self.query(self.replicas[0], f'INSERT INTO {table} VALUES (1)')
            for name in self.replicas:
                self.sync(name, table)
                assert self.query(name, f'SELECT sum(value) FROM {table}', reader=True) == '1'
        finally:
            self.drop_probe(table)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('config')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--keepers-only', action='store_true')
    mode.add_argument('--read-only', action='store_true')
    mode.add_argument('--exercise-replication', action='store_true')
    args = parser.parse_args()
    cluster = Cluster(json.load(open(args.config, encoding='utf-8')))
    cluster.quorum()
    if not args.keepers_only:
        cluster.read_only()
        if args.exercise_replication:
            cluster.replication()
    print('Verification passed')


if __name__ == '__main__':
    main()
