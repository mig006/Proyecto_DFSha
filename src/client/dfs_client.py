"""Librería cliente del DFSha.

El cliente solo conoce la lista de servidores meta (DFS_META). Las direcciones de los
DataNodes las descubre en cada operación preguntándole a meta (RNF7).

Semántica declarada (RF3):
  open(path, "r")  fija la versión actual; read() siempre lee esa versión.
  open(path, "w")  toma el lock exclusivo del archivo (lease de 30 s renovado solo),
                   crea el archivo si no existe. write(offset, data) admite reemplazo
                   parcial y anexado (offset <= tamaño actual; no se admiten huecos).
                   Internamente se reescriben bloques completos (copy-on-write).
  close()          publica todos los cambios como UNA versión nueva (atómica) y suelta el lock.
  Visibilidad: de sesión (close-to-open). Quien abre después de un close ve los cambios.
"""
import hashlib
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import requests


class DFSError(Exception):
    def __init__(self, status, body):
        self.status = status
        self.body = body if isinstance(body, dict) else {"error": str(body)}
        detail = self.body.get("detail", self.body)
        self.code = detail.get("error", "ERROR") if isinstance(detail, dict) else str(detail)
        self.message = detail.get("message", "") if isinstance(detail, dict) else ""
        super().__init__(f"{status} {self.code} {self.message}".strip())


class DFSClient:
    def __init__(self, meta_urls=None, ca=None, token=None, parallel=8):
        urls = meta_urls or os.environ.get("DFS_META", "https://127.0.0.1:8001")
        self.metas = [u.strip() for u in (urls.split(",") if isinstance(urls, str) else urls) if u.strip()]
        self.verify = ca or os.environ.get("DFS_CA") or True
        self.token = token
        self.parallel = parallel
        self.http = requests.Session()
        self.http.verify = self.verify
        self.http.trust_env = False  # sin proxies: conexión directa a los nodos
        adapter = requests.adapters.HTTPAdapter(pool_connections=32, pool_maxsize=32)
        self.http.mount("https://", adapter)
        self.http.mount("http://", adapter)

    # ---------- llamadas a meta con conmutación entre instancias ----------
    def _meta(self, method, path, **kw):
        headers = kw.pop("headers", {})
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        last = None
        for i, m in enumerate(list(self.metas)):
            try:
                r = self.http.request(method, m + path, headers=headers, timeout=30, **kw)
            except requests.RequestException as ex:
                last = DFSError(503, {"error": "META_UNREACHABLE", "message": f"{m}: {ex.__class__.__name__}"})
                continue
            if r.status_code == 503 and i + 1 < len(self.metas):
                last = DFSError(503, r.json())
                continue
            if i:
                self.metas.insert(0, self.metas.pop(i))  # se usa primero la que respondió
            if r.status_code >= 400:
                try:
                    raise DFSError(r.status_code, r.json())
                except ValueError:
                    raise DFSError(r.status_code, {"error": r.text})
            return r.json()
        raise last

    # ---------- usuarios y RF1 ----------
    def login(self, user, password):
        self.token = self._meta("POST", "/api/v1/auth/login", json={"user": user, "password": password})["access_token"]
        return self.token

    def ls(self, path="/"):
        return self._meta("GET", "/api/v1/fs/ls", params={"path": path})["entries"]

    def stat(self, path):
        return self._meta("GET", "/api/v1/fs/stat", params={"path": path})

    def exists(self, path):
        try:
            self.stat(path)
            return True
        except DFSError as e:
            if e.status == 404:
                return False
            raise

    def mkdir(self, path, parents=False):
        return self._meta("POST", "/api/v1/fs/mkdir", json={"path": path, "parents": parents})

    def rmdir(self, path):
        return self._meta("DELETE", "/api/v1/fs/rmdir", params={"path": path})

    def rm(self, path):
        return self._meta("DELETE", "/api/v1/fs/rm", params={"path": path})

    def cluster(self):
        return self._meta("GET", "/api/v1/admin/cluster")

    # ---------- locks ----------
    def lock(self, path, wait=True, timeout=120, ttl=None):
        """Pide el lock exclusivo. Con wait=True reintenta con espera creciente."""
        delay, t0 = 0.2, time.time()
        body = {"path": path}
        if ttl:
            body["ttl"] = ttl
        while True:
            try:
                return self._meta("POST", "/api/v1/locks", json=body)["lock_id"]
            except DFSError as e:
                if e.status != 423 or not wait or time.time() - t0 > timeout:
                    raise
                time.sleep(delay)
                delay = min(delay * 1.5, 2.0)

    def renew(self, path, lock_id):
        return self._meta("PUT", f"/api/v1/locks/{lock_id}", params={"path": path})

    def unlock(self, path, lock_id):
        return self._meta("DELETE", f"/api/v1/locks/{lock_id}", params={"path": path})

    # ---------- bloques ----------
    def _upload_block(self, blk, data, need, want):
        """Sube un bloque a varios DataNodes. Si uno falla prueba con el siguiente."""
        sha = hashlib.sha256(data).hexdigest()
        ok, tried = [], []
        for t in blk["targets"]:
            if len(ok) >= want:
                break
            tried.append(t["node"])
            try:
                r = self.http.put(f"{t['url']}/blocks/{blk['id']}", data=data, timeout=60,
                                  headers={"X-Block-Token": blk["token"], "X-SHA256": sha})
                if r.status_code == 200:
                    ok.append(t["node"])
            except requests.RequestException:
                continue
        if len(ok) < need:
            raise DFSError(503, {"error": "INSUFFICIENT_REPLICAS",
                                 "message": f"bloque {blk['id']}: {len(ok)} copias (probados {tried})"})
        return {"id": blk["id"], "sha256": sha, "size": len(data), "replicas": ok}

    def _upload_many(self, datas):
        """Sube varios bloques nuevos en paralelo. Devuelve sus descriptores."""
        if not datas:
            return []
        a = self._meta("POST", "/api/v1/blocks/alloc", json={"count": len(datas)})
        need, want = a["write_quorum"], a["replication"]
        with ThreadPoolExecutor(self.parallel) as ex:
            return list(ex.map(lambda p: self._upload_block(p[0], p[1], need, want), zip(a["blocks"], datas)))

    def _download_block(self, b):
        """Baja un bloque de cualquier réplica viva y revisa su SHA-256."""
        errors = []
        for rep in b["replicas"]:
            try:
                r = self.http.get(f"{rep['url']}/blocks/{b['id']}", headers={"X-Block-Token": b["token"]}, timeout=60)
                if r.status_code == 200 and hashlib.sha256(r.content).hexdigest() == b["sha256"]:
                    return r.content
                errors.append(f"{rep['node']}:{r.status_code}")
            except requests.RequestException as ex:
                errors.append(f"{rep['node']}:{ex.__class__.__name__}")
        raise DFSError(503, {"error": "BLOCK_UNAVAILABLE", "message": f"bloque {b['idx']}: {errors or 'sin réplicas vivas'}"})

    def locate(self, path):
        return self._meta("GET", "/api/v1/files/locate", params={"path": path})

    def _commit(self, path, lock_id, base_version, blocks, size, sha=None):
        body = {"path": path, "lock_id": lock_id, "base_version": base_version, "size": size,
                "blocks": blocks, "sha256": sha}
        for attempt in range(3):  # si la respuesta se pierde, repetir es seguro (idempotente)
            try:
                return self._meta("POST", "/api/v1/files/commit", json=body)
            except DFSError as e:
                if e.code != "META_UNREACHABLE" or attempt == 2:
                    raise
                time.sleep(1)

    # ---------- RF2: archivos completos ----------
    def put(self, local, remote):
        size = os.path.getsize(local)
        bs = self._meta("POST", "/api/v1/blocks/alloc", json={"count": 0})["block_size"]
        lock_id = self.lock(remote)
        renewer = _Renewer(self, remote, lock_id)
        try:
            base = self.stat(remote)["version"] if self.exists(remote) else 0
            full = hashlib.sha256()
            blocks = []
            with open(local, "rb") as f:
                while True:  # se sube por tandas para no cargar el archivo entero en memoria
                    batch = []
                    for _ in range(self.parallel * 2):
                        chunk = f.read(bs)
                        if not chunk:
                            break
                        full.update(chunk)
                        batch.append(chunk)
                    if not batch:
                        break
                    blocks += self._upload_many(batch)
            if size == 0:
                blocks = []
            return self._commit(remote, lock_id, base, blocks, size, full.hexdigest())
        finally:
            renewer.stop()
            self.unlock(remote, lock_id)

    def get(self, remote, local):
        m = self.locate(remote)
        with open(local, "wb") as f:
            f.truncate(m["size"])

        def job(b):
            data = self._download_block(b)
            with open(local, "r+b") as f:
                f.seek(b["idx"] * m["block_size"])
                f.write(data)
            return len(data)

        with ThreadPoolExecutor(self.parallel) as ex:
            list(ex.map(job, m["blocks"]))
        if m.get("sha256"):
            h = hashlib.sha256()
            with open(local, "rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    h.update(chunk)
            if h.hexdigest() != m["sha256"]:
                raise DFSError(500, {"error": "DATA_LOSS", "message": "el hash del archivo no coincide"})
        return m

    # ---------- RF3: acceso por partes ----------
    def open(self, path, mode="r"):
        return DFSFile(self, path, mode)


class _Renewer:
    """Renueva el lease del lock cada 10 s mientras el archivo esté abierto."""

    def __init__(self, client, path, lock_id, every=10):
        self._stop = threading.Event()
        self.lost = False

        def run():
            while not self._stop.wait(every):
                try:
                    client.renew(path, lock_id)
                except DFSError:
                    self.lost = True
                    return

        threading.Thread(target=run, daemon=True).start()

    def stop(self):
        self._stop.set()


class DFSFile:
    def __init__(self, client: DFSClient, path, mode):
        if mode not in ("r", "w"):
            raise ValueError("modo debe ser 'r' o 'w'")
        self.c, self.path, self.mode = client, path, mode
        self.lock_id, self.renewer = None, None
        self.dirty: dict[int, bytes] = {}
        self.cache: dict[int, bytes] = {}
        if mode == "w":
            self.lock_id = client.lock(path)
            self.renewer = _Renewer(client, path, self.lock_id)
        try:
            self.map = client.locate(path)
        except DFSError as e:
            if e.status != 404 or mode != "w":
                self._release()
                raise
            bs = client._meta("POST", "/api/v1/blocks/alloc", json={"count": 0})["block_size"]
            self.map = {"path": path, "version": 0, "size": 0, "block_size": bs, "blocks": []}
        self.version = self.map["version"]
        self.size = self.map["size"]
        self.bs = self.map["block_size"]
        self.closed = False

    def _block(self, i) -> bytes:
        if i in self.dirty:
            return self.dirty[i]
        if i >= len(self.map["blocks"]):
            return b""
        if i not in self.cache:
            self.cache[i] = self.c._download_block(self.map["blocks"][i])
        return self.cache[i]

    def read(self, offset, length):
        """Lee de la versión fijada en open() (y de los cambios propios si está en modo 'w')."""
        if offset < 0 or offset > self.size:
            raise DFSError(400, {"error": "INVALID_OFFSET", "message": f"offset {offset}, tamaño {self.size}"})
        end = min(self.size, offset + length)
        first, last = offset // self.bs, max(offset, end - 1) // self.bs
        idxs = list(range(first, last + 1)) if end > offset else []
        missing = [i for i in idxs if i not in self.dirty and i not in self.cache and i < len(self.map["blocks"])]
        with ThreadPoolExecutor(self.c.parallel) as ex:  # bloques en paralelo
            for i, d in zip(missing, ex.map(lambda i: self.c._download_block(self.map["blocks"][i]), missing)):
                self.cache[i] = d
        buf = b"".join(self._block(i) for i in idxs)
        start = offset - first * self.bs
        return buf[start:start + (end - offset)]

    def write(self, offset, data: bytes):
        if self.mode != "w":
            raise DFSError(400, {"error": "READ_ONLY", "message": "abra con modo 'w'"})
        if self.renewer and self.renewer.lost:
            raise DFSError(409, {"error": "STALE_LOCK", "message": "se perdió el lock"})
        if offset < 0 or offset > self.size:
            raise DFSError(400, {"error": "INVALID_OFFSET", "message": f"offset {offset} > tamaño {self.size} (no se admiten huecos)"})
        pos, view = offset, memoryview(data)
        while view:
            i, inner = pos // self.bs, pos % self.bs
            cur = bytearray(self._block(i))
            n = min(len(view), self.bs - inner)
            cur[inner:inner + n] = view[:n]
            self.dirty[i] = bytes(cur)
            pos += n
            view = view[n:]
        self.size = max(self.size, offset + len(data))
        return len(data)

    def close(self):
        """Publica los cambios como una versión nueva y suelta el lock."""
        if self.closed:
            return None
        self.closed = True
        result = None
        try:
            if self.mode == "w":
                nblocks = (self.size + self.bs - 1) // self.bs
                idxs = sorted(self.dirty)
                new = dict(zip(idxs, self.c._upload_many([self.dirty[i] for i in idxs])))
                blocks = [new[i] if i in new else self.map["blocks"][i] for i in range(nblocks)]
                blocks = [{"id": b["id"], "sha256": b["sha256"], "size": b["size"], "replicas": b.get("replicas", [])}
                          for b in blocks]
                if self.dirty or self.version == 0:
                    result = self.c._commit(self.path, self.lock_id, self.version, blocks, self.size)
        finally:
            self._release()
        return result

    def _release(self):
        if self.renewer:
            self.renewer.stop()
        if self.lock_id:
            try:
                self.c.unlock(self.path, self.lock_id)
            except DFSError:
                pass
            self.lock_id = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        if exc[0] is None:
            self.close()
        else:
            self.closed = True
            self._release()
