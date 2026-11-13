"""Exercise ClickHouse's actual native and Keeper outbound TLS implementations."""
import queue
import socketserver
import ssl
import threading
from pathlib import Path

from fixture import openssl


class TLSProbe:
    def __init__(self, path):
        self.servers = []
        self.threads = []
        self.results = {}
        # The fixture's private Podman bridge; leaf identities are valid for this IP.
        self.host = '172.30.243.1'
        path = Path(path)
        openssl('req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1', '-subj', '/CN=Untrusted CI CA',
                '-keyout', path / 'untrusted-ca.key', '-out', path / 'untrusted-ca.crt',
                '-addext', 'basicConstraints=critical,CA:TRUE', '-addext', 'keyUsage=critical,keyCertSign,cRLSign')
        entries = []
        for name, ca in [('trusted', 'ca'), ('untrusted', 'untrusted-ca')]:
            openssl('req', '-newkey', 'rsa:2048', '-nodes', '-subj', '/CN=TLS probe',
                    '-keyout', path / f'{name}.key', '-out', path / f'{name}.csr')
            (path / f'{name}.ext').write_text(f'subjectAltName=IP:{self.host}\nextendedKeyUsage=serverAuth\n')
            openssl('x509', '-req', '-in', path / f'{name}.csr', '-CA', path / f'{ca}.crt', '-CAkey', path / f'{ca}.key',
                    '-CAcreateserial', '-days', '1', '-extfile', path / f'{name}.ext', '-out', path / f'{name}.crt')
            # Rejecting a correctly named, valid leaf must be a trust failure, not a SAN/expiry failure.
            openssl('verify', '-x509_strict', '-purpose', 'sslserver', '-verify_ip', self.host,
                    '-CAfile', path / f'{ca}.crt', path / f'{name}.crt')
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(path / f'{name}.crt', path / f'{name}.key')
            results = queue.Queue()
            self.results[name] = results

            class Handler(socketserver.BaseRequestHandler):
                def handle(self):
                    self.request.settimeout(5)
                    try:
                        with self.server.context.wrap_socket(self.request, server_side=True):
                            self.server.results.put('accepted')
                    except ssl.SSLError as error:
                        self.server.results.put(error.reason)
                    except OSError as error:
                        self.server.results.put(type(error).__name__)

            server = socketserver.ThreadingTCPServer(('0.0.0.0', 0), Handler)
            server.daemon_threads = True
            server.context, server.results = context, results
            self.servers.append(server)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.threads.append(thread)
            port = server.server_address[1]
            entries.append(f'<{name}><node><host>{self.host}</host><port>{port}</port><secure>1</secure></node>'
                           f'<session_timeout_ms>3000</session_timeout_ms><connection_timeout_ms>1000</connection_timeout_ms>'
                           f'<operation_timeout_ms>3000</operation_timeout_ms></{name}>')
        config = path / 'replica1/config/config.d'
        config.mkdir(exist_ok=True)
        (config / 'tls-probes.xml').write_text('<clickhouse><auxiliary_zookeepers>' + ''.join(entries)
                                             + '</auxiliary_zookeepers></clickhouse>')

    def check(self, cluster):
        # Keeper may retry a TLS-only endpoint before returning its protocol error.
        cluster = type(cluster)(cluster.config | {'query_timeout': 60})
        for protocol in ['native', 'keeper']:
            for index, name in enumerate(['trusted', 'untrusted']):
                results = self.results[name]
                while not results.empty():
                    results.get_nowait()
                if protocol == 'native':
                    port = self.servers[index].server_address[1]
                    sql = f"SELECT * FROM remoteSecure('{self.host}:{port}', system.one)"
                else:
                    sql = (f'CREATE TABLE default.tls_{name} (x UInt8) ENGINE=ReplicatedMergeTree('
                           f"'{name}:/tls-probe', 'replica1') ORDER BY x")
                try:
                    cluster.query('replica1', sql)
                except RuntimeError:
                    # Probe speaks TLS only. The assertion is the observed handshake, not this SQL failure.
                    pass
                else:
                    raise AssertionError('TLS-only probe unexpectedly spoke native/Keeper protocol')
                observed = results.get(timeout=10)
                if name == 'trusted':
                    assert observed == 'accepted', (protocol, name, observed)
                else:
                    assert observed in {'TLSV1_ALERT_UNKNOWN_CA', 'SSLV3_ALERT_CERTIFICATE_UNKNOWN', 'SSLV3_ALERT_BAD_CERTIFICATE'}, (
                        protocol, name, observed)
                    assert 'accepted' not in list(results.queue), f'{protocol}: untrusted certificate accepted'
                print(f'PASS: outbound {protocol} TLS {name}: {observed}', flush=True)

    def close(self):
        for server in self.servers:
            server.shutdown()
            server.server_close()
        for thread in self.threads:
            thread.join(timeout=5)
