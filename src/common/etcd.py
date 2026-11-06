"""Cliente mínimo de etcd v3 usando su gateway HTTP/JSON (sin librerías extra).

etcd guarda todos los metadatos con Raft: si cae 1 de 3 miembros sigue funcionando,
y cada operación es linealizable. Aquí solo usamos lo necesario: get, get por prefijo,
put, delete, transacciones con comparación y leases.
"""
import base64
import json
import os

import requests


def _e(s) -> str:
    if isinstance(s, str):
        s = s.encode()
    return base64.b64encode(s).decode()


def _d(s: str) -> str:
    return base64.b64decode(s).decode()


def _prefix_end(prefix: str) -> str:
    b = bytearray(prefix.encode())
    b[-1] += 1
    return _e(bytes(b))


class EtcdError(Exception):
    pass


class Etcd:
    def __init__(self, endpoints=None, ca=None, cert=None, key=None):
        eps = endpoints or os.environ.get("DFS_ETCD", "http://127.0.0.1:2379")
        self.endpoints = [e.strip() for e in eps.split(",") if e.strip()]
        self.s = requests.Session()
        self.s.trust_env = False  # ignora proxies del entorno: el tráfico interno va directo
        ca = ca or os.environ.get("DFS_CA")
        cert = cert or os.environ.get("DFS_CERT")
        key = key or os.environ.get("DFS_KEY")
        self.s.verify = ca if ca else True
        if cert and key:
            self.s.cert = (cert, key)

    def _call(self, path, body):
        last = None
        # Si un miembro de etcd no responde, se prueba el siguiente.
        for i, ep in enumerate(list(self.endpoints)):
            try:
                r = self.s.post(ep + path, json=body, timeout=5)
                if r.status_code == 200:
                    if i:  # el que respondió pasa al frente
                        self.endpoints.insert(0, self.endpoints.pop(i))
                    return r.json()
                last = EtcdError(f"{ep}{path}: {r.status_code} {r.text}")
            except requests.RequestException as ex:
                last = EtcdError(f"{ep}: {ex}")
        raise last

    # ---------- lectura ----------
    def get(self, key):
        """Devuelve (valor_json, mod_revision) o (None, 0)."""
        r = self._call("/v3/kv/range", {"key": _e(key)})
        kvs = r.get("kvs", [])
        if not kvs:
            return None, 0
        kv = kvs[0]
        return json.loads(_d(kv["value"])), int(kv["mod_revision"])

    def get_prefix(self, prefix, keys_only=False):
        body = {"key": _e(prefix), "range_end": _prefix_end(prefix)}
        if keys_only:
            body["keys_only"] = True
        r = self._call("/v3/kv/range", body)
        out = []
        for kv in r.get("kvs", []):
            val = None if keys_only else json.loads(_d(kv["value"]))
            out.append((_d(kv["key"]), val, int(kv["mod_revision"])))
        return out

    # ---------- escritura ----------
    def put(self, key, value, lease=None):
        body = {"key": _e(key), "value": _e(json.dumps(value))}
        if lease:
            body["lease"] = str(lease)
        self._call("/v3/kv/put", body)

    def delete(self, key):
        self._call("/v3/kv/deleterange", {"key": _e(key)})

    def delete_prefix(self, prefix):
        self._call("/v3/kv/deleterange", {"key": _e(prefix), "range_end": _prefix_end(prefix)})

    # ---------- transacciones ----------
    @staticmethod
    def cmp_mod(key, rev):
        """La clave no cambió desde rev (rev=0 significa que no existe)."""
        return {"key": _e(key), "target": "MOD", "result": "EQUAL", "mod_revision": str(rev)}

    @staticmethod
    def cmp_absent(key):
        return {"key": _e(key), "target": "CREATE", "result": "EQUAL", "create_revision": "0"}

    @staticmethod
    def cmp_value(key, value):
        return {"key": _e(key), "target": "VALUE", "result": "EQUAL", "value": _e(json.dumps(value))}

    @staticmethod
    def op_put(key, value, lease=None):
        p = {"key": _e(key), "value": _e(json.dumps(value))}
        if lease:
            p["lease"] = str(lease)
        return {"request_put": p}

    @staticmethod
    def op_delete(key):
        return {"request_delete_range": {"key": _e(key)}}

    def txn(self, compare, success, failure=None):
        """Aplica success si todas las comparaciones se cumplen, de forma atómica."""
        r = self._call("/v3/kv/txn", {"compare": compare, "success": success, "failure": failure or []})
        return bool(r.get("succeeded", False))

    # ---------- leases ----------
    def lease_grant(self, ttl):
        return int(self._call("/v3/lease/grant", {"TTL": str(ttl)})["ID"])

    def lease_keepalive(self, lease_id):
        r = self._call("/v3/lease/keepalive", {"ID": str(lease_id)})
        return int(r.get("result", {}).get("TTL", 0)) > 0

    def lease_revoke(self, lease_id):
        try:
            self._call("/v3/lease/revoke", {"ID": str(lease_id)})
        except EtcdError:
            pass
