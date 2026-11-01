"""Encrypt/decrypt ./data for the GitHub Actions cache.

The repo is public, so the cache is stored only as cache/data.enc (AES-GCM, key from
DASHBOARD_PASSWORD). Workflows from forks get no secrets and cannot read it.

    python cache_crypt.py pack     # data/ -> cache/data.enc
    python cache_crypt.py unpack   # cache/data.enc -> data/ (no-op if missing or undecryptable)
"""
import io
import os
import sys
import zipfile
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.hashes import SHA256
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

ROOT = Path(__file__).resolve().parent
DATA, ENC = ROOT / "data", ROOT / "cache" / "data.enc"


def key(salt):
    pw = os.environ.get("DASHBOARD_PASSWORD", "")
    if not pw:
        sys.exit("Missing DASHBOARD_PASSWORD")
    return PBKDF2HMAC(algorithm=SHA256(), length=32, salt=salt, iterations=200_000).derive(pw.encode())


def pack():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for p in DATA.rglob("*"):
            if p.is_file() and p.name != "payload_debug.json":
                z.write(p, p.relative_to(DATA).as_posix())
    salt, iv = os.urandom(16), os.urandom(12)
    ENC.parent.mkdir(exist_ok=True)
    ENC.write_bytes(salt + iv + AESGCM(key(salt)).encrypt(iv, buf.getvalue(), None))
    print(f"Packed data cache ({ENC.stat().st_size // 1024} KB)")


def unpack():
    if not ENC.exists():
        print("No data cache: full sync will run")
        return
    raw = ENC.read_bytes()
    try:
        plain = AESGCM(key(raw[:16])).decrypt(raw[16:28], raw[28:], None)
    except InvalidTag:
        print("Data cache could not be decrypted (password changed?): full sync will run")
        return
    with zipfile.ZipFile(io.BytesIO(plain)) as z:
        z.extractall(DATA)
    print("Restored data cache")


if __name__ == "__main__":
    {"pack": pack, "unpack": unpack}[sys.argv[1]]()
