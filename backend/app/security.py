"""Keys, PINs, derived tokens and location encryption."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
from typing import Optional

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def same(a: Optional[str], b: Optional[str]) -> bool:
    if a is None or b is None:
        return False
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def new_token() -> str:
    """URL-safe and valid as a Telegram /start parameter (A-Z a-z 0-9 _ -)."""
    return secrets.token_urlsafe(16)


# --- PINs ------------------------------------------------------------------
_SCRYPT = dict(n=2 ** 14, r=8, p=1, dklen=32)


def hash_pin(pin: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.scrypt(pin.encode("utf-8"), salt=salt, **_SCRYPT)
    return "scrypt$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(dk).decode()


def _check_pin(pin: str, stored: Optional[str]) -> bool:
    if not stored:
        # Burn the same work so a missing hash is not faster.
        hashlib.scrypt(pin.encode("utf-8"), salt=b"\0" * 16, **_SCRYPT)
        return False
    try:
        _, salt_b64, dk_b64 = stored.split("$")
        salt, dk = base64.b64decode(salt_b64), base64.b64decode(dk_b64)
    except ValueError:
        return False
    return hmac.compare_digest(hashlib.scrypt(pin.encode("utf-8"), salt=salt, **_SCRYPT), dk)


def classify_pin(pin: str, real_hash: Optional[str], duress_hash: Optional[str]) -> Optional[str]:
    """'real', 'duress' or None. Both hashes are always checked (same work either way)."""
    is_real = _check_pin(pin, real_hash)
    is_duress = _check_pin(pin, duress_hash)
    if is_duress:
        return "duress"
    if is_real:
        return "real"
    return None


# --- derived capability tokens ----------------------------------------------
def derive_token(secret: str, purpose: str, ident: str, nonce: str) -> str:
    mac = hmac.new(secret.encode("utf-8"), f"{purpose}:{ident}:{nonce}".encode("utf-8"), hashlib.sha256)
    return base64.urlsafe_b64encode(mac.digest()).decode().rstrip("=")[:32]


# --- location encryption ----------------------------------------------------
def _loc_key(secret: str) -> bytes:
    return hashlib.sha256(("stillhere-location:" + secret).encode("utf-8")).digest()


def encrypt_coords(secret: str, lat: float, lon: float) -> bytes:
    nonce = os.urandom(12)
    data = json.dumps({"lat": lat, "lon": lon}).encode("utf-8")
    return nonce + AESGCM(_loc_key(secret)).encrypt(nonce, data, b"loc")


def decrypt_coords(secret: str, blob: bytes) -> dict:
    blob = bytes(blob)
    return json.loads(AESGCM(_loc_key(secret)).decrypt(blob[:12], blob[12:], b"loc"))
