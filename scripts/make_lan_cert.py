"""Make a self-signed certificate so a phone on the same Wi-Fi can use the agent.

Why a certificate is needed at all: browsers only expose a microphone on a **secure
context**. ``http://localhost`` is specially exempted, which is why the agent works on
the machine running it — but a LAN address like ``http://192.168.0.3:8080`` is not, so
``navigator.mediaDevices`` is simply absent there. The page loads and the call button
does nothing. No amount of permission-granting fixes it; the API is not present.

So the local server has to speak HTTPS. A real certificate needs a public domain, and
this is a private address, so it is self-signed: the phone shows a warning once, you
accept it, and from then on the microphone works.

The LAN IP goes in the Subject Alternative Name. Mobile browsers ignore the legacy
Common Name entirely, and a certificate without a matching SAN is rejected outright
rather than merely warned about.

    python scripts/make_lan_cert.py
    python scripts/serve_aws.py --host 0.0.0.0 --port 8443 \
        --ssl-certfile .certs/lan.pem --ssl-keyfile .certs/lan.key
"""

from __future__ import annotations

import argparse
import datetime as dt
import ipaddress
import socket
from pathlib import Path


def lan_ip() -> str:
    """This machine's address on the local network.

    Opens a UDP socket toward a public address to discover which interface the OS
    would route through. Nothing is sent — it is the routing decision that is wanted,
    which is more reliable than picking from a list of adapters that includes
    virtual ones.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        address: str = sock.getsockname()[0]
        return address
    finally:
        sock.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--host",
        default=None,
        help="LAN IP to certify (default: detected automatically)",
    )
    parser.add_argument("--out", default=".certs", help="directory to write into")
    parser.add_argument("--days", type=int, default=90)
    args = parser.parse_args()

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    address = args.host or lan_ip()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, "Clinic Front Desk (local)")]
    )
    now = dt.datetime.now(dt.timezone.utc)

    # Every name the phone might use. Without the IP here, mobile browsers reject the
    # certificate outright instead of offering the "proceed anyway" escape hatch.
    alt_names: list[x509.GeneralName] = [
        x509.IPAddress(ipaddress.ip_address(address)),
        x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
        x509.DNSName("localhost"),
    ]

    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=args.days))
        .add_extension(x509.SubjectAlternativeName(alt_names), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )

    cert_path = out / "lan.pem"
    key_path = out / "lan.key"
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )

    print(f"\n  certificate : {cert_path}")
    print(f"  private key : {key_path}")
    print(f"  valid for   : {address}, 127.0.0.1, localhost ({args.days} days)")
    print("\n  Serve it with:")
    print(
        f"    python scripts/serve_aws.py --host 0.0.0.0 --port 8443 "
        f"--ssl-certfile {cert_path} --ssl-keyfile {key_path}"
    )
    print(f"\n  On the phone (same Wi-Fi):  https://{address}:8443/voice")
    print("  Accept the certificate warning once, then the microphone will work.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
