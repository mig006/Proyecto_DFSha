"""Servidor de metadatos (meta) del DFSha.

meta NO guarda estado propio: todo vive en etcd (replicado con Raft). Por eso se pueden
correr varias instancias detrás de la misma lista de direcciones, y si una cae el
cliente usa otra. Los bytes de los archivos nunca pasan por aquí: el cliente habla
directo con los DataNodes usando los tokens de bloque que emite meta.

Esquema de claves en etcd:
  /users/<u>          {"pw": hash}
  /fs/<u><ruta>       directorio {"type":"dir"} o archivo {"type":"file", "version", "size", "blocks":[...]}
  /dn/<id>            DataNode vivo {"url","free","blocks"}      (lease de 15 s, lo renueva el heartbeat)
  /locks/<u><ruta>    lock exclusivo {"lock_id","lease","user"}  (lease de 30 s, lo renueva el cliente)
  /leader             qué meta hace reparación y limpieza       (lease de 10 s)
"""
import os
import posixpath
import threading
import time
import uuid

import jwt as pyjwt
import requests
from fastapi import Depends, FastAPI, Header, HTTPException

from common import security
from common.etcd import Etcd, EtcdError

META_ID = os.environ.get("DFS_META_ID", "meta1")
REPLICATION = int(os.environ.get("DFS_REPLICATION", "3"))   # R: copias por bloque
WRITE_QUORUM = int(os.environ.get("DFS_WRITE_QUORUM", "2"))  # W: copias mínimas para confirmar
BLOCK_SIZE = int(os.environ.get("DFS_BLOCK_SIZE", str(4 * 1024 * 1024)))
DN_TTL = 15          # sin heartbeat por 15 s => el DataNode se considera caído
LOCK_TTL = 30        # duración del lease de un lock (renovable)
GC_GRACE = int(os.environ.get("DFS_GC_GRACE", "600"))  # edad mínima para borrar un bloque sin referencia
CA = os.environ.get("DFS_CA") or True

etcd = Etcd()
HTTP = requests.Session()
HTTP.trust_env = False
app = FastAPI(title=f"DFSha meta {META_ID}")
_dn_leases: dict[str, int] = {}


def err(status, code, message="", **extra):
    raise HTTPException(status, {"error": code, "message": message, **extra})


# ---------------- autenticación ----------------
def current_user(authorization: str = Header(None)) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        err(401, "TOKEN_INVALID", "falta el token")
    try:
        return security.read_jwt(authorization[7:])
    except pyjwt.InvalidTokenError:
        err(401, "TOKEN_INVALID", "token inválido o vencido")


def norm(path: str) -> str:
    """Normaliza una ruta del usuario. Toda ruta queda dentro del espacio propio."""
    if not path:
        path = "/"
    if not path.startswith("/"):
        path = "/" + path
    p = posixpath.normpath(path)
    if p.startswith("//"):
        p = p[1:]
    return p


def fs_key(user, path):
    return f"/fs/{user}{'' if path == '/' else path}"


def lock_key(user, path):
    return f"/locks/{user}{path}"


def get_node(user, path):
    if path == "/":
        return {"type": "dir"}, 0
    return etcd.get(fs_key(user, path))


def alive_dns() -> dict:
    return {k.split("/")[-1]: v for k, v, _ in etcd.get_prefix("/dn/")}


# ---------------- usuarios ----------------
@app.post("/api/v1/auth/login")
def login(body: dict):
    rec, _ = etcd.get(f"/users/{body.get('user', '')}")
    if not rec or not security.check_password(body.get("password", ""), rec["pw"]):
        err(401, "BAD_CREDENTIALS", "usuario o contraseña incorrectos")
    return {"access_token": security.make_jwt(body["user"]), "expires_in": security.JWT_TTL}


@app.post("/api/v1/admin/users")
def add_user(body: dict, x_internal_token: str = Header(None)):
    if not security.check_internal(x_internal_token):
        err(403, "FORBIDDEN")
    etcd.put(f"/users/{body['user']}", {"pw": security.hash_password(body["password"])})
    return {"created": body["user"]}


# ---------------- RF1: árbol de directorios ----------------
@app.get("/api/v1/fs/stat")
def stat(path: str, user: str = Depends(current_user)):
    path = norm(path)
    node, _ = get_node(user, path)
    if not node:
        err(404, "NOT_FOUND", path)
    out = {"path": path, "type": node["type"]}
    if node["type"] == "file":
        out.update(size=node["size"], version=node["version"], blocks=len(node["blocks"]),
                   block_size=node["block_size"], mtime=node["mtime"])
    return out


@app.get("/api/v1/fs/ls")
def ls(path: str = "/", user: str = Depends(current_user)):
    path = norm(path)
    node, _ = get_node(user, path)
    if not node:
        err(404, "NOT_FOUND", path)
    if node["type"] == "file":
        return {"path": path, "entries": [{"name": posixpath.basename(path), "type": "file",
                                           "size": node["size"], "version": node["version"]}]}
    prefix = fs_key(user, path) + "/"
    entries = []
    for k, v, _ in etcd.get_prefix(prefix):
        rest = k[len(prefix):]
        if "/" in rest:
            continue
        e = {"name": rest, "type": v["type"]}
        if v["type"] == "file":
            e.update(size=v["size"], version=v["version"])
        entries.append(e)
    return {"path": path, "entries": sorted(entries, key=lambda e: (e["type"] != "dir", e["name"]))}


def _check_parent(user, path):
    parent, _ = get_node(user, posixpath.dirname(path))
    if not parent or parent["type"] != "dir":
        err(404, "NOT_FOUND", f"no existe el directorio {posixpath.dirname(path)}")


@app.post("/api/v1/fs/mkdir")
def mkdir(body: dict, user: str = Depends(current_user)):
    path = norm(body["path"])
    if path == "/":
        err(409, "EXISTS", "/")
    chain = [path]
    if body.get("parents"):
        p = posixpath.dirname(path)
        while p != "/":
            chain.insert(0, p)
            p = posixpath.dirname(p)
    else:
        _check_parent(user, path)
    for p in chain:
        k = fs_key(user, p)
        ok = etcd.txn([Etcd.cmp_absent(k)], [Etcd.op_put(k, {"type": "dir", "mtime": time.time()})])
        if not ok and p == path and not body.get("parents"):
            err(409, "EXISTS", path)
    return {"created": path}


@app.delete("/api/v1/fs/rmdir")
def rmdir(path: str, user: str = Depends(current_user)):
    path = norm(path)
    node, rev = get_node(user, path)
    if path == "/" or not node or node["type"] != "dir":
        err(404, "NOT_FOUND", path)
    if etcd.get_prefix(fs_key(user, path) + "/", keys_only=True):
        err(409, "DIR_NOT_EMPTY", path)
    etcd.txn([Etcd.cmp_mod(fs_key(user, path), rev)], [Etcd.op_delete(fs_key(user, path))])
    return {"deleted": path}


@app.delete("/api/v1/fs/rm")
def rm(path: str, user: str = Depends(current_user)):
    path = norm(path)
    node, rev = get_node(user, path)
    if not node or node["type"] != "file":
        err(404, "NOT_FOUND", path)
    holder, _ = etcd.get(lock_key(user, path))
    if holder:
        err(423, "LOCKED", "el archivo tiene un lock activo")
    # Los bloques se quedan en los DataNodes hasta que la limpieza (GC) los borre.
    if not etcd.txn([Etcd.cmp_mod(fs_key(user, path), rev), Etcd.cmp_absent(lock_key(user, path))],
                    [Etcd.op_delete(fs_key(user, path))]):
        err(409, "CONFLICT", "el archivo cambió, reintente")
    return {"deleted": path}


# ---------------- RF3: locks con lease ----------------
@app.post("/api/v1/locks")
def lock(body: dict, user: str = Depends(current_user)):
    """Lock exclusivo de escritura sobre un archivo. Vence solo si el cliente muere."""
    path = norm(body["path"])
    _check_parent(user, path)
    ttl = int(body.get("ttl", LOCK_TTL))
    lease = etcd.lease_grant(ttl)
    val = {"lock_id": uuid.uuid4().hex, "lease": lease, "user": user}
    k = lock_key(user, path)
    if etcd.txn([Etcd.cmp_absent(k)], [Etcd.op_put(k, val, lease)]):
        return {"lock_id": val["lock_id"], "ttl": ttl, "path": path}
    etcd.lease_revoke(lease)
    err(423, "LOCKED", "otro cliente tiene el lock; reintente", retry_after_ms=1000)


def _my_lock(user, path, lock_id):
    val, _ = etcd.get(lock_key(user, path))
    if not val or val["lock_id"] != lock_id:
        return None
    return val


@app.put("/api/v1/locks/{lock_id}")
def renew(lock_id: str, path: str, user: str = Depends(current_user)):
    val = _my_lock(user, norm(path), lock_id)
    if not val or not etcd.lease_keepalive(val["lease"]):
        err(409, "STALE_LOCK", "el lock ya venció")
    return {"lock_id": lock_id, "renewed": True}


@app.delete("/api/v1/locks/{lock_id}")
def unlock(lock_id: str, path: str, user: str = Depends(current_user)):
    path = norm(path)
    val = _my_lock(user, path, lock_id)
    if val:
        etcd.txn([Etcd.cmp_value(lock_key(user, path), val)], [Etcd.op_delete(lock_key(user, path))])
        etcd.lease_revoke(val["lease"])
    return {"released": bool(val)}


# ---------------- RF2/RF3: bloques y commit ----------------
@app.post("/api/v1/blocks/alloc")
def alloc(body: dict, user: str = Depends(current_user)):
    """Reserva ids de bloque nuevos y propone DataNodes para cada uno.

    Se devuelven todos los nodos vivos ordenados: los primeros R son los destinos y el
    resto sirve de reemplazo si alguno falla durante la carga.
    """
    n = int(body.get("count", 1))
    dns = sorted(alive_dns().items(), key=lambda kv: -kv[1]["free"])
    if len(dns) < WRITE_QUORUM:
        err(503, "NOT_ENOUGH_NODES", f"hay {len(dns)} DataNodes vivos y se necesitan {WRITE_QUORUM}")
    out = []
    for i in range(n):
        k = i % len(dns)  # se rota el nodo inicial: los bloques quedan repartidos (RNF4)
        order = dns[k:] + dns[:k]
        bid = "b-" + uuid.uuid4().hex
        out.append({"id": bid, "token": security.make_block_token(bid, "w"),
                    "targets": [{"node": d, "url": v["url"]} for d, v in order]})
    return {"block_size": BLOCK_SIZE, "replication": REPLICATION, "write_quorum": WRITE_QUORUM, "blocks": out}


@app.get("/api/v1/files/locate")
def locate(path: str, user: str = Depends(current_user)):
    """Devuelve el mapa del archivo: qué bloques lo forman y en qué nodos vivos están."""
    path = norm(path)
    node, _ = get_node(user, path)
    if not node or node["type"] != "file":
        err(404, "NOT_FOUND", path)
    alive = alive_dns()
    blocks = []
    for i, b in enumerate(node["blocks"]):
        reps = [{"node": r, "url": alive[r]["url"]} for r in b["replicas"] if r in alive]
        blocks.append({"idx": i, "id": b["id"], "sha256": b["sha256"], "size": b["size"],
                       "replicas": reps, "token": security.make_block_token(b["id"], "r")})
    return {"path": path, "version": node["version"], "size": node["size"], "block_size": node["block_size"],
            "sha256": node.get("sha256"), "blocks": blocks}


@app.post("/api/v1/files/commit")
def commit(body: dict, user: str = Depends(current_user)):
    """Publica una versión nueva del archivo de forma atómica.

    Reglas (garantía comprobable): solo se acepta si el cliente tiene el lock vigente,
    si el archivo sigue en la versión base y si cada bloque nuevo tiene al menos W
    copias en nodos vivos. Todo se escribe con UNA transacción de etcd.
    """
    path = norm(body["path"])
    _check_parent(user, path)
    k, lk = fs_key(user, path), lock_key(user, path)
    lock_val = _my_lock(user, path, body.get("lock_id", ""))
    cur, rev = etcd.get(k)
    commit_id = f"{body.get('lock_id')}:{body.get('base_version', 0)}"
    if cur and cur.get("commit_id") == commit_id:  # reintento de un commit ya aplicado (idempotente)
        return {"path": path, "version": cur["version"], "size": cur["size"], "repeated": True}
    if not lock_val:
        err(409, "STALE_LOCK", "no tiene el lock (venció o nunca lo pidió)")
    if cur and cur["type"] != "file":
        err(409, "IS_DIR", path)
    cur_version = cur["version"] if cur else 0
    if int(body.get("base_version", 0)) != cur_version:
        err(409, "VERSION_CONFLICT", f"versión base {body.get('base_version')}, actual {cur_version}")

    alive = alive_dns()
    old = {b["id"]: b for b in (cur["blocks"] if cur else [])}
    blocks = []
    for b in body["blocks"]:
        if b["id"] in old:          # bloque sin cambios: se conserva igual
            blocks.append(old[b["id"]])
            continue
        reps = [r for r in dict.fromkeys(b.get("replicas", [])) if r in alive]
        if len(reps) < WRITE_QUORUM:
            err(422, "INSUFFICIENT_REPLICAS", f"bloque {b['id']} tiene {len(reps)} copias vivas")
        blocks.append({"id": b["id"], "sha256": b["sha256"], "size": b["size"], "replicas": reps})

    size = int(body["size"])
    if sum(b["size"] for b in blocks) != size:
        err(400, "BAD_SIZE", "la suma de los bloques no coincide con el tamaño")
    new = {"type": "file", "version": cur_version + 1, "size": size, "block_size": BLOCK_SIZE,
           "sha256": body.get("sha256"), "blocks": blocks, "mtime": time.time(), "commit_id": commit_id}
    if not etcd.txn([Etcd.cmp_mod(k, rev), Etcd.cmp_value(lk, lock_val)], [Etcd.op_put(k, new)]):
        err(409, "CONFLICT", "el archivo o el lock cambiaron durante el commit")
    return {"path": path, "version": new["version"], "size": size}


# ---------------- plano interno ----------------
@app.post("/internal/heartbeat")
def heartbeat(body: dict, x_internal_token: str = Header(None)):
    if not security.check_internal(x_internal_token):
        err(403, "FORBIDDEN")
    dn = body["id"]
    lease = _dn_leases.get(dn)
    if not lease or not etcd.lease_keepalive(lease):
        lease = etcd.lease_grant(DN_TTL)
        _dn_leases[dn] = lease
    etcd.put(f"/dn/{dn}", {"url": body["url"], "free": body["free"], "blocks": body["blocks"],
                           "ts": time.time(), "via": META_ID}, lease)
    return {"ok": True}


@app.get("/api/v1/admin/cluster")
def cluster(user: str = Depends(current_user)):
    leader, _ = etcd.get("/leader")
    return {"meta": META_ID, "leader": leader, "datanodes": alive_dns()}


@app.get("/health")
def health():
    try:
        etcd.get("/leader")
        return {"meta": META_ID, "etcd": "ok"}
    except EtcdError as ex:
        err(503, "ETCD_UNAVAILABLE", str(ex))


# ---------------- reparación de copias y limpieza (solo el líder) ----------------
def _internal_req(method, url, **kw):
    return HTTP.request(method, url, headers={"X-Internal-Token": security.INTERNAL_TOKEN},
                            verify=CA, timeout=60, **kw)


def repair_once():
    """Repone copias de bloques que quedaron con menos de R réplicas vivas."""
    alive = alive_dns()
    fixed = 0
    for k, node, rev in etcd.get_prefix("/fs/"):
        if node.get("type") != "file":
            continue
        changed = False
        for b in node["blocks"]:
            live = [r for r in b["replicas"] if r in alive]
            if len(live) >= REPLICATION or not live:
                if len(live) != len(b["replicas"]) and live:
                    b["replicas"] = live
                    changed = True
                continue
            candidates = sorted((d for d in alive if d not in live), key=lambda d: -alive[d]["free"])
            for tgt in candidates[: REPLICATION - len(live)]:
                try:
                    r = _internal_req("POST", f"{alive[tgt]['url']}/internal/replicate",
                                      json={"block_id": b["id"], "sha256": b["sha256"],
                                            "source_url": alive[live[0]]["url"]})
                    if r.status_code == 200:
                        live.append(tgt)
                        fixed += 1
                except requests.RequestException:
                    pass
            b["replicas"] = live
            changed = True
        if changed:
            etcd.txn([Etcd.cmp_mod(k, rev)], [Etcd.op_put(k, node)])
    if fixed:
        print(f"[{META_ID}] reparación: {fixed} copias repuestas", flush=True)


def gc_once():
    """Borra bloques que ningún archivo usa (cargas abortadas, versiones viejas, archivos borrados)."""
    used = set()
    for _, node, _ in etcd.get_prefix("/fs/"):
        if node.get("type") == "file":
            used.update(b["id"] for b in node["blocks"])
    now = time.time()
    for dn, info in alive_dns().items():
        try:
            for b in _internal_req("GET", f"{info['url']}/internal/blocks").json()["blocks"]:
                if b["id"] not in used and now - b["mtime"] > GC_GRACE:
                    _internal_req("DELETE", f"{info['url']}/internal/blocks/{b['id']}")
        except requests.RequestException:
            pass


def _leader_loop():
    lease, last_gc = None, 0
    while True:
        try:
            if lease and etcd.lease_keepalive(lease):
                leader, _ = etcd.get("/leader")
                if leader and leader.get("meta") == META_ID:
                    repair_once()
                    if time.time() - last_gc > 30:
                        gc_once()
                        last_gc = time.time()
            else:
                lease = etcd.lease_grant(10)
                if not etcd.txn([Etcd.cmp_absent("/leader")], [Etcd.op_put("/leader", {"meta": META_ID}, lease)]):
                    etcd.lease_revoke(lease)
                    lease = None
        except Exception as ex:  # etcd sin quórum, etc.: se reintenta
            print(f"[{META_ID}] líder: {ex}", flush=True)
            lease = None
        time.sleep(3)


def _bootstrap_users():
    for pair in filter(None, os.environ.get("DFS_USERS", "").split(",")):
        u, p = pair.split(":", 1)
        for _ in range(30):
            try:
                if not etcd.get(f"/users/{u}")[0]:
                    etcd.put(f"/users/{u}", {"pw": security.hash_password(p)})
                break
            except EtcdError:
                time.sleep(1)


@app.on_event("startup")
def _start():
    threading.Thread(target=_bootstrap_users, daemon=True).start()
    threading.Thread(target=_leader_loop, daemon=True).start()
