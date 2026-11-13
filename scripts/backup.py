#!/usr/bin/env python3
"""Native SQL backup coordinator. Host-only IAM; no static production credentials."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import sys
import time
import uuid
import xml.etree.ElementTree as ET

import boto3
import botocore.session
from botocore.credentials import CredentialResolver, InstanceMetadataProvider
from botocore.utils import InstanceMetadataFetcher
from botocore.config import Config
from verify import Cluster, QueryError


def atomic_json(path, value):
    path = Path(path)
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(value, indent=2) + '\n')
    os.chmod(temp, 0o600)
    temp.replace(path)


def identifier(value):
    if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)?', value):
        raise ValueError('Invalid database/table identifier')
    return '.'.join('`' + part + '`' for part in value.split('.'))


def literal(value):
    return "'" + value.replace('\\', '\\\\').replace("'", "\\'") + "'"


def session(config):
    if config.get('test_credentials_file'):
        # Test runner only; this key is never rendered by Ansible.
        credentials = json.loads(Path(config['test_credentials_file']).read_text())
        return boto3.Session(region_name=config['region'], **credentials)
    core = botocore.session.get_session()
    fetcher = InstanceMetadataFetcher(timeout=5, num_attempts=3, config={'ec2_metadata_v1_disabled': True})
    core.register_component('credential_provider', CredentialResolver([InstanceMetadataProvider(iam_role_fetcher=fetcher)]))
    return boto3.Session(botocore_session=core, region_name=config['region'])


def refresh(config, aws):
    credentials = aws.get_credentials().get_frozen_credentials()
    root = ET.Element('clickhouse')
    collection = ET.SubElement(ET.SubElement(root, 'named_collections'), 'stack_backup')
    fields = {'access_key_id': credentials.access_key, 'secret_access_key': credentials.secret_key,
              'session_token': credentials.token or '', 'region': config['region'], 'use_environment_credentials': 'false'}
    for key, value in fields.items():
        ET.SubElement(collection, key, overridable='false').text = value
    ET.SubElement(collection, 'url', overridable='false').text = config['url']
    endpoint = ET.SubElement(ET.SubElement(root, 's3'), 'stack_backup')
    ET.SubElement(endpoint, 'endpoint').text = config['url']
    ET.SubElement(endpoint, 'region').text = config['region']
    directory = Path(config['credentials_dir'])
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chown(directory, 101, 0)
    temp = directory / ('backup.' + uuid.uuid4().hex + '.tmp')
    temp.write_bytes(ET.tostring(root))
    os.chmod(temp, 0o400)
    os.chown(temp, 101, 0)
    temp.replace(directory / 'backup.xml')


class Backups:
    def __init__(self, config, cluster=None, s3=None):
        self.config = config
        self.cluster = cluster or Cluster(json.loads(Path(config['verification']).read_text()) | {'query_timeout': 21600})
        self.node = config['node']
        self.bucket = config['bucket']
        self.prefix = config['prefix'].strip('/') + '/'
        if not re.fullmatch(r'[a-zA-Z0-9/_-]+/', self.prefix) or '..' in self.prefix:
            raise ValueError('Unsafe backup prefix')
        self.s3 = s3 or session(config).client('s3', endpoint_url=config.get('endpoint'), verify=config.get('ca', True),
                                             config=Config(s3={'addressing_style': 'path'}, retries={'max_attempts': 3}))

    def destination(self, backup_id):
        if not re.fullmatch(r'[0-9a-f]{32}', backup_id):
            raise ValueError('Invalid backup ID')
        return f"S3(stack_backup, {literal('data/' + backup_id + '/')})"

    def records(self):
        records = []
        for page in self.s3.get_paginator('list_objects_v2').paginate(Bucket=self.bucket, Prefix=self.prefix + 'catalog/'):
            for obj in page.get('Contents', []):
                record = json.loads(self.s3.get_object(Bucket=self.bucket, Key=obj['Key'])['Body'].read())
                self.destination(record['id'])
                self.destination(record['full'])
                if obj['Key'] != self.prefix + 'catalog/' + record['id'] + '.json':
                    raise ValueError('Catalog key mismatch')
                records.append(record)
        by_id = {r['id']: r for r in records}
        for record in records:
            base = by_id[record['full']]
            if base['full'] != base['id']:
                raise ValueError('Invalid full backup dependency')
        return sorted(records, key=lambda r: r['completed'])

    def backup(self, force_full=False, max_bandwidth=0):
        database = identifier(self.config['database'])
        # Single-shard coordinator: sync all ReplicatedMergeTree tables before the snapshot.
        tables = self.cluster.query(self.node, f"SELECT name FROM system.tables WHERE database={literal(self.config['database'])} "
                                    "AND engine LIKE 'Replicated%MergeTree' FORMAT JSONEachRow")
        for line in tables.splitlines():
            self.cluster.sync(self.node, database + '.' + identifier(json.loads(line)['name']))
        records = self.records()
        fulls = [r for r in records if r['id'] == r['full'] and r['database'] == self.config['database']]
        base = fulls[-1] if fulls else None
        now = time.time()
        if force_full or not base or now - base['completed'] >= self.config['full_every_hours'] * 3600:
            base = None
        backup_id = uuid.uuid4().hex
        sql = f'BACKUP DATABASE {database} TO {self.destination(backup_id)}'
        settings = []
        if base:
            settings.append(f"base_backup={self.destination(base['id'])}")
        if max_bandwidth:
            settings.append(f"max_backup_bandwidth={int(max_bandwidth)}")
        if settings:
            sql += ' SETTINGS ' + ', '.join(settings)
        started = time.time()
        self.cluster.query(self.node, sql)
        completed = time.time()
        record = {'id': backup_id, 'full': base['id'] if base else backup_id, 'database': self.config['database'],
                  'started': started, 'completed': completed}
        self.s3.put_object(Bucket=self.bucket, Key=self.prefix + 'catalog/' + backup_id + '.json', Body=json.dumps(record).encode())
        return record

    def delete_prefix(self, prefix):
        # Delete versions, not just current objects/delete markers; never age data with S3 lifecycle.
        for page in self.s3.get_paginator('list_object_versions').paginate(Bucket=self.bucket, Prefix=prefix):
            for item in page.get('Versions', []) + page.get('DeleteMarkers', []):
                self.s3.delete_object(Bucket=self.bucket, Key=item['Key'], VersionId=item['VersionId'])

    def retain(self, now=None):
        records = self.records()
        if not records:
            return []
        cutoff = (time.time() if now is None else now) - self.config['retention_days'] * 86400
        # Always retain the latest family for EACH configured database, even when stale.
        latest = {r['database']: r['full'] for r in records}
        keep = set(latest.values()) | {r['full'] for r in records if r['completed'] >= cutoff}
        expired = [r for r in records if r['full'] not in keep]
        # Children before bases; interrupted deletion leaves no retained dangling dependency.
        for record in sorted(expired, key=lambda r: r['id'] == r['full']):
            self.delete_prefix(self.prefix + 'data/' + record['id'] + '/')
            self.delete_prefix(self.prefix + 'catalog/' + record['id'] + '.json')
        return [r['id'] for r in expired]

    def restore(self, backup_id, source, target, kind):
        if source == target:
            raise ValueError('Restore requires a NEW name; use a new database name even during DR')
        identifier(source)
        identifier(target)
        if kind == 'TABLE':
            if '.' not in source or '.' not in target:
                raise ValueError('Table names must be database-qualified')
            database, table = target.split('.')
            exists = f"SELECT count() FROM system.tables WHERE database={literal(database)} AND name={literal(table)}"
        elif kind == 'DATABASE' and '.' not in source and '.' not in target:
            exists = f"SELECT count() FROM system.databases WHERE name={literal(target)}"
        else:
            raise ValueError('Invalid restore kind or database name')
        for replica in self.cluster.replicas:
            if self.cluster.query(replica, exists) != '0':
                raise ValueError('Restore target already exists; choose a new name')
        # Atomic DB + {uuid} paths isolate restored tables from the original Keeper znodes.
        sql = f'RESTORE {kind} {identifier(source)} AS {identifier(target)} ON CLUSTER stack FROM {self.destination(backup_id)}'
        return self.cluster.query(self.node, sql)


def check_status(path, max_age):
    status = json.loads(Path(path).read_text())
    if status['state'] != 'success' or time.time() - status['completed'] > max_age:
        raise RuntimeError('Backup status failed, running, or stale')
    return status


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('config')
    parser.add_argument('action', choices=['run', 'full', 'retain', 'credentials', 'status', 'restore-table', 'restore-database'])
    parser.add_argument('--id')
    parser.add_argument('--source')
    parser.add_argument('--target')
    args = parser.parse_args()
    config = json.loads(Path(args.config).read_text())
    if args.action == 'status':
        check_status(config['status'], config['max_age_hours'] * 3600)
        return
    lock_path = config['status'] + ('.credentials.lock' if args.action == 'credentials' else '.lock')
    with open(lock_path, 'a', encoding='utf-8') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        status = {'state': 'running', 'started': time.time(), 'action': args.action}
        tracks_backup = args.action in ('run', 'full')
        if tracks_backup:
            atomic_json(config['status'], status)
        try:
            aws = session(config)
            refresh(config, aws)
            if args.action == 'credentials':
                return  # ClickHouse watches config.d; starting the server also loads it.
            manager = Backups(config)
            manager.cluster.query(config['node'], 'SYSTEM RELOAD CONFIG')
            if tracks_backup:
                status['backup'] = manager.backup(args.action == 'full')
                status['deleted'] = manager.retain()
            elif args.action == 'retain':
                manager.retain()
            else:
                manager.restore(args.id, args.source, args.target, 'TABLE' if args.action == 'restore-table' else 'DATABASE')
            status.update(state='success', completed=time.time())
        except BaseException as error:
            status.update(state='failure', completed=time.time(), error_type=type(error).__name__)
            # QueryError's printable message contains only node/HTTP/ClickHouse codes, never SQL.
            # Other exception messages can contain SDK credentials and must stay private.
            detail = str(error) if isinstance(error, QueryError) else type(error).__name__
            raise RuntimeError(f'{args.action} failed: {detail} (sensitive details withheld)') from None
        finally:
            if tracks_backup:
                atomic_json(config['status'], status)


if __name__ == '__main__':
    main()
