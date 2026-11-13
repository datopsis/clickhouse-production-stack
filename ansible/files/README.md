# Amazon Trust Services roots

`amazon-trust-services.pem` concatenates the five unmodified self-signed PEM
files listed in `amazon-trust-services.json`, in manifest order. Downloaded
2026-11-13 from the [Amazon Trust Services repository](https://www.amazontrust.com/repository/).
Amazon Trust Services licenses these certificates under
[CC BY-ND 4.0](https://creativecommons.org/licenses/by-nd/4.0/).
The manifest records each source URL, PEM SHA-256 and DER SHA-256 fingerprint;
the DER fingerprints were checked against the repository's published values.

Bundle SHA-256:
`dfaa39c0350288ded35812f7b80f76c4a422b97ecc04c4ac84e325bec528b1b8`.

This is the ATS-recommended custom trust-store set: Amazon Root CA 1–4
(different RSA/ECDSA key types) and Starfield Services Root CA G2 (compatibility
chains). It contains no other public roots, intermediate or leaf pins. No
certificate is fetched during deployment. Ansible verifies the bundle hash
before copying it; contract tests verify each constituent fingerprint too.
Updates require reviewing the upstream roots, updating this file/manifest and
passing the trust-scope and native tests. A file hash pins our reviewed artifact;
it is not a claim that AWS promises an immutable certificate chain.

Only the ClickHouse server's outbound context combines these roots with the
private cluster CA. Keeper and the standalone client use only the private CA;
all inbound contexts use only the private CA. The host backup SDK uses the ATS
bundle alone. No default/system CA bundle is loaded by these contexts.

ClickHouse 26.8.2.7 native S3 uses the common Poco HTTPS pool:
[PocoHTTPClient](https://github.com/ClickHouse/ClickHouse/blob/v26.8.2.7-lts/src/IO/S3/PocoHTTPClient.cpp)
→ [HTTPCommon](https://github.com/ClickHouse/ClickHouse/blob/v26.8.2.7-lts/src/IO/HTTPCommon.cpp)
→ [HTTPConnectionPool](https://github.com/ClickHouse/ClickHouse/blob/v26.8.2.7-lts/src/Common/HTTPConnectionPool.cpp).
That path constructs `HTTPSClientSession(host, port)` without an endpoint TLS
context; it does not expose an S3-specific CA override. Consequently ClickHouse
outbound native/inter-server/Keeper connections also trust the explicit ATS
roots, not just S3. Hostname/IP checking and private-CA-only inbound mTLS remain
required. This is a narrowed shared context, not per-endpoint trust isolation.
