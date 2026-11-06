"""Utilidades comunes de las pruebas."""
import hashlib
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src", "client"))
from dfs_client import DFSClient, DFSError  # noqa: E402,F401

USER = os.environ.get("DFS_TEST_USER", "miguel")
PASS = os.environ.get("DFS_TEST_PASS", "miguel123")
TMP = os.environ.get("DFS_TEST_TMP", "/tmp")


def client(user=USER, password=PASS, parallel=8):
    c = DFSClient(parallel=parallel)
    c.login(user, password)
    return c


def make_file(path, mb):
    with open(path, "wb") as f:
        for _ in range(mb):
            f.write(os.urandom(1024 * 1024))
    return path


def sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def run(cmd, manual_msg=""):
    """Ejecuta un comando del sistema (por ejemplo docker stop dfs-dn3).

    Si el comando es "manual", pide que la acción se haga a mano (útil en AWS, donde
    el nodo está en otra máquina) y espera Enter.
    """
    if cmd == "manual":
        input(f"   >>> {manual_msg} y presione Enter para continuar... ")
        return
    print(f"   $ {cmd}")
    subprocess.run(cmd, shell=True, check=False)


def title(t):
    print("\n" + "=" * 70 + f"\n{t}\n" + "=" * 70)


RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, ok))
    print(f"  [{'OK ' if ok else 'FALLA'}] {name}" + (f"  -> {detail}" if detail else ""))


def summary():
    ok = sum(1 for _, o in RESULTS if o)
    print(f"\nResultado: {ok}/{len(RESULTS)} verificaciones correctas")
    sys.exit(0 if ok == len(RESULTS) else 1)


def wait(seconds, msg):
    print(f"   ... {msg} ({seconds}s)", flush=True)
    time.sleep(seconds)


def replicas_by_node(m):
    count = {}
    for b in m["blocks"]:
        for r in b["replicas"]:
            count[r["node"]] = count.get(r["node"], 0) + 1
    return dict(sorted(count.items()))
