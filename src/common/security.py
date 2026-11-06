"""Seguridad compartida: hash de contraseñas, JWT de usuario y tokens de bloque.

- Contraseñas: scrypt (librería estándar) con sal aleatoria.
- JWT de usuario: HS256 con DFS_JWT_SECRET, compartido entre las instancias meta.
- Token de bloque: HMAC-SHA256 con DFS_BLOCK_SECRET (meta lo emite, el DataNode lo
  valida sin consultar a nadie). Dice qué bloque, qué operación y hasta cuándo.
- Token interno: DFS_INTERNAL_TOKEN, lo usan DataNodes y meta entre ellos.
"""
import base64
import hashlib
import hmac
import json
import os
import secrets
import time

import jwt

JWT_SECRET = os.environ.get("DFS_JWT_SECRET", "dev-jwt-secret-cambiar")
BLOCK_SECRET = os.environ.get("DFS_BLOCK_SECRET", "dev-block-secret-cambiar").encode()
INTERNAL_TOKEN = os.environ.get("DFS_INTERNAL_TOKEN", "dev-internal-token-cambiar")
JWT_TTL = 3600
BLOCK_TOKEN_TTL = 600


# ---------- contraseñas ----------
def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    h = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
    return salt.hex() + ":" + h.hex()


def check_password(password: str, stored: str) -> bool:
    salt_hex, h_hex = stored.split(":")
    h = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex), n=2**14, r=8, p=1)
    return hmac.compare_digest(h.hex(), h_hex)


# ---------- JWT de usuario ----------
def make_jwt(user: str) -> str:
    now = int(time.time())
    return jwt.encode({"sub": user, "iat": now, "exp": now + JWT_TTL}, JWT_SECRET, algorithm="HS256")


def read_jwt(token: str) -> str:
    """Devuelve el usuario o lanza jwt.InvalidTokenError."""
    return jwt.decode(token, JWT_SECRET, algorithms=["HS256"])["sub"]


# ---------- token de bloque ----------
def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def make_block_token(block_id: str, op: str, ttl: int = BLOCK_TOKEN_TTL) -> str:
    payload = json.dumps({"b": block_id, "op": op, "exp": int(time.time()) + ttl}, separators=(",", ":")).encode()
    sig = hmac.new(BLOCK_SECRET, payload, hashlib.sha256).digest()
    return _b64(payload) + "." + _b64(sig)


def check_block_token(token: str, block_id: str, op: str) -> bool:
    try:
        p64, s64 = token.split(".")
        payload = _unb64(p64)
        if not hmac.compare_digest(hmac.new(BLOCK_SECRET, payload, hashlib.sha256).digest(), _unb64(s64)):
            return False
        data = json.loads(payload)
        return data["b"] == block_id and data["op"] == op and data["exp"] >= time.time()
    except Exception:
        return False


def check_internal(token: str | None) -> bool:
    return token is not None and hmac.compare_digest(token, INTERNAL_TOKEN)
