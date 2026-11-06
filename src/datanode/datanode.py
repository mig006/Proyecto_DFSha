"""DataNode del DFSha.

Guarda bloques inmutables cifrados con AES-256-GCM. Un bloque se escribe primero en
<id>.part y solo se renombra a <id> (operación atómica) cuando el hash es correcto,
así que un bloque a medias nunca queda visible. Cada 3 s manda un heartbeat a meta.
"""
import hashlib
import os
import secrets
import shutil
import threading
import time

import requests
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi import FastAPI, Header, HTTPException, Request, Response

from common import security

NODE_ID = os.environ.get("DFS_NODE_ID", "dn1")
NODE_URL = os.environ.get("DFS_NODE_URL", "https://127.0.0.1:9001")
META_URLS = [u.strip() for u in os.environ.get("DFS_META", "https://127.0.0.1:8001").split(",") if u.strip()]
DATA_DIR = os.environ.get("DFS_DATA_DIR", f"./data/{NODE_ID}")
CA = os.environ.get("DFS_CA") or True
HEARTBEAT_S = 3
HTTP = requests.Session()
HTTP.trust_env = False

# Llave de cifrado en reposo: viene de una variable de entorno (secreto), no del disco de datos.
_key_src = os.environ.get("DFS_NODE_KEY", "dev-node-key-cambiar-" + NODE_ID)
AES = AESGCM(hashlib.sha256(_key_src.encode()).digest())

os.makedirs(DATA_DIR, exist_ok=True)
for f in os.listdir(DATA_DIR):  # restos de escrituras interrumpidas
    if f.endswith(".part"):
        os.remove(os.path.join(DATA_DIR, f))

app = FastAPI(title=f"DFSha DataNode {NODE_ID}")


def _path(bid: str) -> str:
    if not all(c.isalnum() or c == "-" for c in bid):
        raise HTTPException(400, {"error": "BAD_BLOCK_ID"})
    return os.path.join(DATA_DIR, bid)


def _store(bid: str, data: bytes, sha: str):
    if hashlib.sha256(data).hexdigest() != sha:
        raise HTTPException(422, {"error": "DATA_LOSS", "message": "el SHA-256 no coincide"})
    final = _path(bid)
    if os.path.exists(final):
        return False
    nonce = secrets.token_bytes(12)
    blob = nonce + AES.encrypt(nonce, data, bid.encode())  # el id va como dato autenticado
    part = final + ".part"
    with open(part, "wb") as f:
        f.write(blob)
        f.flush()
        os.fsync(f.fileno())
    os.replace(part, final)  # rename atómico
    return True


def _load(bid: str) -> bytes:
    p = _path(bid)
    if not os.path.exists(p):
        raise HTTPException(404, {"error": "NOT_FOUND"})
    with open(p, "rb") as f:
        blob = f.read()
    try:
        return AES.decrypt(blob[:12], blob[12:], bid.encode())
    except Exception:
        raise HTTPException(500, {"error": "DATA_LOSS", "message": "bloque alterado en disco"})


# ---------------- API para clientes ----------------
@app.put("/blocks/{bid}")
async def put_block(bid: str, request: Request, x_block_token: str = Header(None), x_sha256: str = Header(None)):
    if not security.check_block_token(x_block_token or "", bid, "w"):
        raise HTTPException(401, {"error": "TOKEN_INVALID"})
    data = await request.body()
    created = _store(bid, data, x_sha256 or "")
    return {"node": NODE_ID, "block_id": bid, "stored": True, "new": created}


@app.get("/blocks/{bid}")
def get_block(bid: str, x_block_token: str = Header(None), x_internal_token: str = Header(None)):
    ok = security.check_block_token(x_block_token or "", bid, "r") or security.check_internal(x_internal_token)
    if not ok:
        raise HTTPException(401, {"error": "TOKEN_INVALID"})
    data = _load(bid)
    return Response(content=data, media_type="application/octet-stream",
                    headers={"X-SHA256": hashlib.sha256(data).hexdigest(), "X-Node": NODE_ID})


# ---------------- API interna (meta / otros DataNodes) ----------------
def _internal(tok):
    if not security.check_internal(tok):
        raise HTTPException(403, {"error": "FORBIDDEN"})


@app.post("/internal/replicate")
def replicate(body: dict, x_internal_token: str = Header(None)):
    """Copia un bloque desde otra réplica (reposición de copias tras una caída)."""
    _internal(x_internal_token)
    bid, src, sha = body["block_id"], body["source_url"], body["sha256"]
    r = HTTP.get(f"{src}/blocks/{bid}", headers={"X-Internal-Token": security.INTERNAL_TOKEN},
                     verify=CA, timeout=30)
    if r.status_code != 200:
        raise HTTPException(502, {"error": "SOURCE_FAILED", "message": r.text})
    _store(bid, r.content, sha)
    return {"node": NODE_ID, "block_id": bid, "stored": True}


@app.get("/internal/blocks")
def list_blocks(x_internal_token: str = Header(None)):
    _internal(x_internal_token)
    out = []
    for f in os.listdir(DATA_DIR):
        if not f.endswith(".part"):
            out.append({"id": f, "mtime": os.path.getmtime(os.path.join(DATA_DIR, f))})
    return {"node": NODE_ID, "blocks": out}


@app.delete("/internal/blocks/{bid}")
def delete_block(bid: str, x_internal_token: str = Header(None)):
    _internal(x_internal_token)
    p = _path(bid)
    if os.path.exists(p):
        os.remove(p)
    return {"deleted": bid}


@app.get("/health")
def health():
    return {"node": NODE_ID, "ok": True}


# ---------------- heartbeat ----------------
def _heartbeat_loop():
    while True:
        free = shutil.disk_usage(DATA_DIR).free
        n = sum(1 for f in os.listdir(DATA_DIR) if not f.endswith(".part"))
        body = {"id": NODE_ID, "url": NODE_URL, "free": free, "blocks": n}
        for i, m in enumerate(list(META_URLS)):  # cualquier meta sirve
            try:
                r = HTTP.post(f"{m}/internal/heartbeat", json=body,
                                  headers={"X-Internal-Token": security.INTERNAL_TOKEN}, verify=CA, timeout=3)
                if r.status_code == 200:
                    if i:
                        META_URLS.insert(0, META_URLS.pop(i))
                    break
            except requests.RequestException:
                continue
        time.sleep(HEARTBEAT_S)


@app.on_event("startup")
def _start():
    threading.Thread(target=_heartbeat_loop, daemon=True).start()
