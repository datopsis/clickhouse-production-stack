#!/usr/bin/env python3
"""Native backup evidence: real SQL, real S3 protocol, fresh Keeper for every DR point."""
import concurrent.futures
import contextlib
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import threading
import time
import xml.etree.ElementTree as ET

import yaml
from fixture import fixture, openssl, ROOT
from functional import run, retry, must_fail
sys.path.insert(0, str(ROOT / 'scripts'))
from backup import Backups, refresh, session, check_status  # noqa: E402
from verify import Cluster  # noqa: E402

EVIDENCE = []


def passed(name, **metrics):
    record = {'assertion': name, 'result': 'pass', **metrics}
    EVIDENCE.append(record)
    print(json.dumps(record), flush=True)


@contextlib.contextmanager
def topology(path, image, s3ca=None):
    path.mkdir()
    variables = fixture(path, image)
    if s3ca:
        for node in variables['stack_nodes']:
            ca = path / node['name'] / 'config/ca.crt'
            ca.write_text(ca.read_text() + s3ca.read_text())
    compose_file = path / 'compose.yml'
    compose = yaml.safe_load(compose_file.read_text())
    compose['networks'] = {'default': {'external': True, 'name': 'stack-backup-test'}}
    compose_file.write_text(yaml.safe_dump(compose))
    command = ['podman-compose', '-p', 'stack-backup-test', '-f', str(compose_file)]
    for node in variables['stack_nodes']:
        run('chown', '-R', '101:0', str(path / node['name']))
    config = {'nodes': variables['stack_nodes'], 'addresses': variables['stack_node_ips'],
              'ca': str(path / 'ca.crt'), 'cert': str(path / 'replica1.crt'), 'key': str(path / 'replica1.key'),
              'admin_password': variables['stack_secrets']['admin_password'],
              'reader_password': variables['stack_secrets']['reader_password'], 'query_timeout': 600}
    (path / 'verification.json').write_text(json.dumps(config))
    cluster = Cluster(config)
    try:
        run(*command, 'up', '-d')
        retry(cluster.quorum)
        retry(cluster.read_only)
        assert cluster.query('replica1', 'SELECT version()') == '26.8.2.7'
        yield cluster
    except BaseException:
        # Server logs can contain rendered SQL. Emit only the secret-free assertion/error code.
        print('Backup topology failed; SQL exception codes are preserved, credentials/log bodies are not.', flush=True)
        raise
    finally:
        run(*command, 'stop', 'replica1', 'replica2')
        for node in ['replica1', 'replica2']:
            state = json.loads(run('podman', 'inspect', 'clickhouse-' + node, capture_output=True).stdout)[0]['State']
            assert state['ExitCode'] == 0, state['ExitCode']
        run(*command, 'down', '--volumes')


@contextlib.contextmanager
def seaweed(path, image, ca_path):
    """Private single-node CI S3 with TLS and freshly generated credentials."""
    path.mkdir(mode=0o700)
    credentials = {'aws_access_key_id': secrets.token_hex(16), 'aws_secret_access_key': secrets.token_hex(32)}
    credentials_path = path / 'credentials.json'
    credentials_path.write_text(json.dumps(credentials))
    (path / 's3.json').write_text(json.dumps({'identities': [{'name': 'ci', 'credentials': [
        {'accessKey': credentials['aws_access_key_id'], 'secretKey': credentials['aws_secret_access_key']}],
        'actions': ['Admin', 'Read', 'List', 'Write', 'Tagging']}]}))
    openssl('req', '-newkey', 'rsa:2048', '-nodes', '-subj', '/CN=seaweed', '-keyout', path / 'tls.key', '-out', path / 'tls.csr')
    (path / 'tls.ext').write_text('subjectAltName=IP:172.30.243.50\nextendedKeyUsage=serverAuth\n')
    openssl('x509', '-req', '-in', path / 'tls.csr', '-CA', ca_path / 'ca.crt', '-CAkey', ca_path / 'ca.key',
            '-CAcreateserial', '-days', '1', '-extfile', path / 'tls.ext', '-out', path / 'tls.crt')
    (path / 'data').mkdir()
    run('chown', '-R', '1000:0', str(path))
    names = []
    try:
        # Separated roles use the image's qualified s3 TLS flags; every container is hardened.
        roles = [('master', ['master', '-mdir=/data', '-ip=172.30.243.50', '-volumeSizeLimitMB=32'], '172.30.243.50'),
                 ('volume', ['volume', '-dir=/data', '-ip=172.30.243.51', '-mserver=172.30.243.50:9333', '-max=5'], '172.30.243.51'),
                 ('filer', ['filer', '-ip=172.30.243.52', '-master=172.30.243.50:9333', '-defaultStoreDir=/data'], '172.30.243.52'),
                 ('s3', ['s3', '-filer=172.30.243.52:8888', '-ip.bind=0.0.0.0', '-config=/config/s3.json',
                         '-cert.file=/config/tls.crt', '-key.file=/config/tls.key'], '172.30.243.53')]
        # Certificate names the actual gateway IP.
        (path / 'tls.ext').write_text('subjectAltName=IP:172.30.243.53\nextendedKeyUsage=serverAuth\n')
        openssl('x509', '-req', '-in', path / 'tls.csr', '-CA', ca_path / 'ca.crt', '-CAkey', ca_path / 'ca.key',
                '-CAcreateserial', '-days', '1', '-extfile', path / 'tls.ext', '-out', path / 'tls.crt')
        for name, args, address in roles:
            data = path / name
            data.mkdir()
            run('chown', '1000:0', str(data))
            container = 'backup-seaweed-' + name
            names.append(container)
            run('podman', 'run', '-d', '--name', container, '--network', 'stack-backup-test', '--ip', address,
                '--user', '1000:0', '--read-only', '--cap-drop=ALL', '--security-opt=no-new-privileges',
                '--tmpfs', '/tmp:size=64m', '--memory', '512m', '--pids-limit', '128',
                '-v', f'{data}:/data:Z', '-v', f'{path}:/config:ro,z', image, *args)
        yield credentials_path
    finally:
        for name in reversed(names):
            run('podman', 'rm', '-f', name)


def configuration(path, credentials, ca):
    return {'node': 'replica1', 'verification': str(path / 'verification.json'),
            'credentials_dir': str(path / 'replica1/config/config.d'), 'status': str(path / 'backup-status.json'),
            'database': 'backup_test', 'region': 'us-east-1', 'bucket': 'backups', 'prefix': 'test',
            'endpoint': 'https://172.30.243.53:8333', 'url': 'https://172.30.243.53:8333/backups/test',
            'ca': str(ca), 'test_credentials_file': str(credentials),
            'full_every_hours': 168, 'retention_days': 30, 'max_age_hours': 26}


def authorize(config, cluster, path):
    for name in cluster.replicas:
        refresh(config | {'credentials_dir': str(path / name / 'config/config.d')}, session(config))
        cluster.query(name, 'SYSTEM RELOAD CONFIG')


def signature(cluster, table):
    return cluster.query('replica1', f'SELECT count(), sum(cityHash64(*)) FROM {table}')


def objects(manager, backup_id):
    return [obj for page in manager.s3.get_paginator('list_objects_v2').paginate(
        Bucket=manager.bucket, Prefix=manager.prefix + 'data/' + backup_id + '/') for obj in page.get('Contents', [])]


def main():
    if os.geteuid() != 0:
        raise SystemExit('Run using the rootful native test runtime')
    image, weed_image, evidence_path = sys.argv[1:]
    start = time.monotonic()
    with tempfile.TemporaryDirectory(prefix='stack-backup-') as directory:
        work = Path(directory)
        # Independent S3 CA, kept across every fresh cluster incarnation.
        ca = work / 's3-ca'
        ca.mkdir()
        openssl('req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1', '-subj', '/CN=Backup CI S3 CA',
                '-keyout', ca / 'ca.key', '-out', ca / 'ca.crt')
        run('podman', 'network', 'create', '--subnet', '172.30.243.0/24', 'stack-backup-test')
        try:
            with seaweed(work / 's3', weed_image, ca) as credentials:
                points = []
                with topology(work / 'source', image, ca / 'ca.crt') as cluster:
                    path = work / 'source'
                    config = configuration(path, credentials, ca / 'ca.crt')
                    authorize(config, cluster, path)
                    manager = Backups(config, cluster)
                    def ready():
                        try:
                            manager.s3.list_buckets()
                        except Exception as error:
                            raise RuntimeError('S3 not ready') from error
                    retry(ready)
                    manager.s3.create_bucket(Bucket='backups')
                    manager.s3.put_bucket_versioning(Bucket='backups', VersioningConfiguration={'Status': 'Enabled'})
                    cluster.query('replica1', 'CREATE DATABASE backup_test ON CLUSTER stack ENGINE=Atomic')
                    cluster.query('replica1', "CREATE TABLE backup_test.events ON CLUSTER stack (id UInt64, payload String) "
                                  "ENGINE=ReplicatedMergeTree('/backup-test/{uuid}/{shard}', '{replica}') ORDER BY id")
                    cluster.query('replica1', 'SYSTEM STOP MERGES backup_test.events ON CLUSTER stack')
                    # Incompressible deterministic payload: unchanged large parts must be referenced, not copied.
                    cluster.query('replica1', "INSERT INTO backup_test.events SELECT number, hex(SHA256(toString(number))) FROM numbers(100000)")
                    full = manager.backup(True)
                    first = signature(cluster, 'backup_test.events')
                    assert first.split('\t')[0] == '100000'
                    full_objects = objects(manager, full['id'])
                    full_bytes = sum(o['Size'] for o in full_objects)
                    assert full_bytes > 1000000
                    assert not any('/replicas/' in o['Key'] for o in full_objects)
                    passed('full backup: exact source count/checksum, one coordinator', signature=first, bytes=full_bytes, objects=len(full_objects))
                    points.append((full, first))
                    cluster.query('replica1', "INSERT INTO backup_test.events SELECT number+100000, hex(SHA256(toString(number+100000))) FROM numbers(1000)")
                    incremental = manager.backup()
                    second = signature(cluster, 'backup_test.events')
                    assert second.split('\t')[0] == '101000'
                    inc_objects = objects(manager, incremental['id'])
                    inc_bytes = sum(o['Size'] for o in inc_objects)
                    manifest_key = manager.prefix + 'data/' + incremental['id'] + '/.backup'
                    manifest = manager.s3.get_object(Bucket='backups', Key=manifest_key)['Body'].read()
                    xml = ET.fromstring(manifest)
                    assert xml.find('base_backup') is not None
                    assert any(el.text == 'true' or el.text == '1' for el in xml.iter('use_base'))
                    assert 0 < inc_bytes < full_bytes / 4
                    # There must be fewer newly uploaded data files than total files described in the manifest.
                    assert len(inc_objects) < len(list(xml.iter('file')))
                    passed('incremental: base references and fewer uploaded objects/bytes', signature=second,
                           bytes=inc_bytes, objects=len(inc_objects), full_bytes=full_bytes)
                    points.append((incremental, second))
                    manager.restore(full['id'], 'backup_test.events', 'backup_test.undo', 'TABLE')
                    cluster.sync('replica2', 'backup_test.undo')
                    assert signature(cluster, 'backup_test.undo') == first
                    assert signature(cluster, 'backup_test.events') == second
                    paths = cluster.query('replica1', "SELECT uniqExact(zookeeper_path) FROM system.replicas WHERE database='backup_test'")
                    assert paths == '2'
                    passed('renamed table restore: exact signature, distinct Keeper path, original unchanged')
                    # Single synchronous writer: strict lower/upper acknowledgement bounds, no retries or async inserts.
                    acknowledged = []
                    stop = threading.Event()
                    def writer():
                        index = 101000
                        while not stop.is_set():
                            cluster.query('replica1', f"INSERT INTO backup_test.events VALUES ({index}, 'live')")
                            acknowledged.append((index, time.monotonic()))
                            index += 1
                            stop.wait(0.02)
                    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                        future = executor.submit(writer)
                        retry(lambda: acknowledged[0] if acknowledged else (_ for _ in ()).throw(AssertionError('writer not started')))
                        lower_time = time.monotonic()
                        live = manager.backup()
                        upper_time = time.monotonic()
                        stop.set()
                        future.result(timeout=60)
                    lower = {i for i, t in acknowledged if t < lower_time}
                    upper = {i for i, t in acknowledged if t <= upper_time}
                    assert lower and len(upper) > len(lower), 'Backup must overlap acknowledged writes'
                    manager.restore(live['id'], 'backup_test.events', 'backup_test.live', 'TABLE')
                    restored = {int(i) for i in cluster.query('replica1', 'SELECT id FROM backup_test.live WHERE id>=101000').splitlines()}
                    assert lower <= restored <= upper
                    assert len(restored) == len(set(restored))
                    assert cluster.query('replica1', 'SELECT count() FROM backup_test.live WHERE id<101000') == '101000'
                    assert cluster.query('replica1', 'SELECT count() FROM backup_test.live WHERE id>=101000 AND payload!=\'live\'') == '0'
                    passed('online snapshot: all pre-start ACKs included; every restored ID ACKed by completion',
                           before=len(lower), restored=len(restored), by_completion=len(upper))
                    must_fail(lambda: manager.restore('0' * 32, 'backup_test.events', 'backup_test.missing', 'TABLE'))
                    corrupt_id = 'f' * 32
                    manager.s3.put_object(Bucket='backups', Key=manager.prefix + 'data/' + corrupt_id + '/.backup', Body=b'corrupt')
                    must_fail(lambda: manager.restore(corrupt_id, 'backup_test.events', 'backup_test.corrupt', 'TABLE'))
                    passed('missing and corrupted backup restores fail with ClickHouse exception codes')
                    # Actual timer ExecStart program, including atomic status, nonzero exit, and verifier rejection.
                    bad_credentials = work / 'wrong.json'
                    bad_credentials.write_text(json.dumps({'aws_access_key_id': secrets.token_hex(16), 'aws_secret_access_key': secrets.token_hex(32)}))
                    bad_config = config | {'test_credentials_file': str(bad_credentials)}
                    config_path = path / 'backup.json'
                    config_path.write_text(json.dumps(bad_config))
                    result = subprocess.run([sys.executable, str(ROOT / 'scripts/backup.py'), str(config_path), 'run'], capture_output=True)
                    assert result.returncode != 0
                    assert json.loads(Path(config['status']).read_text())['state'] == 'failure'
                    must_fail(lambda: check_status(config['status'], 3600))
                    # Explicit wrong credential SQL tests ClickHouse's S3 writer, beyond the host SDK failure.
                    must_fail(lambda: cluster.query('replica1', "BACKUP TABLE backup_test.events TO S3('https://172.30.243.53:8333/backups/denied/', "
                                                   + "'" + secrets.token_hex(16) + "', '" + secrets.token_hex(32) + "')"))
                    passed('wrong S3 credentials fail; timer command exits nonzero, records failure, verifier refuses')
                    authorize(config, cluster, path)
                    # New family makes the old family eligible; expire timestamps, not dependency rules.
                    newest = manager.backup(True)
                    cluster.query('replica1', "INSERT INTO backup_test.events VALUES (9999999, 'retained')")
                    newest_inc = manager.backup()
                    newest_signature = signature(cluster, 'backup_test.events')
                    points.append((newest_inc, newest_signature))
                    config_path.write_text(json.dumps(config))
                    result = subprocess.run([sys.executable, str(ROOT / 'scripts/backup.py'), str(config_path), 'run'], capture_output=True)
                    assert result.returncode == 0, 'Timer success command failed'
                    check_status(config['status'], 3600)
                    passed('timer success command records a fresh successful backup')
                # Source is down. Each DR invocation starts all FIVE nodes on NEW bind directories.
                for index, (record, expected) in enumerate(points):
                    with topology(work / f'dr-{index}', image, ca / 'ca.crt') as restored_cluster:
                        path = work / f'dr-{index}'
                        cfg = configuration(path, credentials, ca / 'ca.crt')
                        authorize(cfg, restored_cluster, path)
                        dr = Backups(cfg, restored_cluster)
                        if index == 2:
                            for old in dr.records():
                                if old['full'] != newest['id']:
                                    old['completed'] -= 40 * 86400
                                    dr.s3.put_object(Bucket='backups', Key=dr.prefix + 'catalog/' + old['id'] + '.json',
                                                     Body=json.dumps(old).encode())
                            expired = dr.retain()
                            assert full['id'] in expired and incremental['id'] in expired
                            assert objects(dr, full['id']) == [] and objects(dr, incremental['id']) == []
                            assert {r['full'] for r in dr.records()} == {newest['id']}
                            passed('retention deletes expired full/incremental family and preserves newest family', deleted=len(expired))
                        began = time.monotonic()
                        dr.restore(record['id'], 'backup_test', 'recovered', 'DATABASE')
                        restored_cluster.sync('replica2', 'recovered.events')
                        assert signature(restored_cluster, 'recovered.events') == expected
                        assert restored_cluster.query('replica2', 'SELECT count(), sum(cityHash64(*)) FROM recovered.events') == expected
                        restored_cluster.query('replica1', "INSERT INTO recovered.events VALUES (88888888, 'post-restore')")
                        restored_cluster.sync('replica2', 'recovered.events')
                        assert restored_cluster.query('replica2', 'SELECT count() FROM recovered.events WHERE id=88888888') == '1'
                        passed('fresh cluster DR and replication: ' + str(index), signature=expected,
                               restore_seconds=round(time.monotonic() - began, 3))
        finally:
            run('podman', 'network', 'rm', 'stack-backup-test')
            Path(evidence_path).write_text(json.dumps({'assertions': EVIDENCE, 'seconds': time.monotonic() - start}, indent=2) + '\n')


if __name__ == '__main__':
    main()
