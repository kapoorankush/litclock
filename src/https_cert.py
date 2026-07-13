"""Self-signed cert generation, shared by setup_server and control_server.

The same cert pair (cert.pem + key.pem under ``<repo>/.certs/``) is used by
both the first-boot setup server and the post-boot control server. They run
sequentially (firstboot exits before control starts; see PLAN A2) so they can
share the same files and the same hostname (``litclock.local``).

Keeping the cert local and self-signed is locked v1 (PRD §7.3, eng-review):
zero new infra, no public DNS dependency, the one-time browser warning is
muscle-memory by the time the user lands on the post-boot PWA. Re-evaluate if
and when cloud relay ships.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

# Cert validity is intentionally large (20y). The cert is per-device and
# regenerated on first run if absent, so rotation isn't a meaningful threat
# model — we'd rather not surprise users with surprise expirations on a
# device sitting on a wall for a decade.
CERT_VALIDITY_DAYS = 7300


def generate_self_signed_cert(cert_dir: str | os.PathLike[str]) -> tuple[str | None, str | None]:
    """Generate (or reuse) a self-signed cert+key pair under ``cert_dir``.

    Idempotent: if both files already exist, return their paths unchanged.
    Returns ``(None, None)`` on failure (e.g., openssl not installed) so
    callers can fall back to plain HTTP without crashing.
    """
    cert_dir = Path(cert_dir)
    cert_file = cert_dir / "cert.pem"
    key_file = cert_dir / "key.pem"

    if cert_file.exists() and key_file.exists():
        return str(cert_file), str(key_file)

    cert_dir.mkdir(parents=True, exist_ok=True)

    cmd = [
        "openssl",
        "req",
        "-x509",
        "-newkey",
        "rsa:2048",
        "-keyout",
        str(key_file),
        "-out",
        str(cert_file),
        "-days",
        str(CERT_VALIDITY_DAYS),
        "-nodes",
        "-subj",
        "/CN=litclock.local/O=LitClock/C=US",
        "-addext",
        "subjectAltName=DNS:litclock.local,DNS:localhost,IP:127.0.0.1",
    ]

    try:
        subprocess.run(cmd, check=True, capture_output=True)
        return str(cert_file), str(key_file)
    except subprocess.CalledProcessError as e:
        print(f"Failed to generate certificate: {e}")
        return None, None
    except FileNotFoundError:
        print("openssl not found - falling back to HTTP only")
        return None, None
