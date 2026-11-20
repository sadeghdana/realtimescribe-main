"""Self-signed HTTPS certificate for LAN access.

Browsers only allow microphone access on https:// pages (or localhost), so serving the UI to
other PCs needs TLS. The certificate is regenerated whenever this PC's addresses change.
"""
import datetime
import ipaddress
import json
import socket
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID


def local_ipv4s():
    ips = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except socket.gaierror:
        pass
    try:  # the address of the default route, in case the hostname lookup missed it
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            ips.add(s.getsockname()[0])
    except OSError:
        pass
    return sorted(ip for ip in ips if not ip.startswith(("127.", "169.254.")))


def ensure_cert(cert_dir: Path):
    cert_dir.mkdir(parents=True, exist_ok=True)
    cert_path, key_path, meta_path = cert_dir / "realtran.crt", cert_dir / "realtran.key", cert_dir / "hosts.json"
    hostname = socket.gethostname()
    ips = ["127.0.0.1", *local_ipv4s()]
    wanted = {"hostname": hostname, "ips": ips}
    if cert_path.exists() and key_path.exists() and meta_path.exists():
        if json.loads(meta_path.read_text(encoding="utf-8")) == wanted:
            return str(cert_path), str(key_path)

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"RealTran ({hostname})")])
    san = [x509.DNSName("localhost"), x509.DNSName(hostname)]
    san += [x509.IPAddress(ipaddress.ip_address(ip)) for ip in ips]
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=825))
        .add_extension(x509.SubjectAlternativeName(san), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.ExtendedKeyUsage([x509.ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .sign(key, hashes.SHA256())
    )
    key_path.write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ))
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    meta_path.write_text(json.dumps(wanted), encoding="utf-8")
    return str(cert_path), str(key_path)
