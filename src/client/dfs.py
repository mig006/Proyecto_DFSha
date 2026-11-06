#!/usr/bin/env python3
"""CLI del DFSha.

Ejemplos:
  python src/client/dfs.py login miguel
  python src/client/dfs.py mkdir -p /videos
  python src/client/dfs.py put clase.mp4 /videos/clase.mp4
  python src/client/dfs.py ls /videos
  python src/client/dfs.py get /videos/clase.mp4 copia.mp4
  python src/client/dfs.py read /notas.txt --offset 0 --length 100
  python src/client/dfs.py write /notas.txt --offset 5 --text "HOLA"
  python src/client/dfs.py lockhold /notas.txt 20      # toma el lock 20 s (demo de concurrencia)

Configuración por variables de entorno:
  DFS_META  lista de servidores meta, ej. https://10.0.1.10:8001,https://10.0.1.11:8001
  DFS_CA    certificado de la CA para verificar HTTPS
"""
import argparse
import getpass
import json
import os
import posixpath
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dfs_client import DFSClient, DFSError  # noqa: E402

SESSION = os.path.expanduser(os.environ.get("DFS_SESSION", "~/.dfs_session.json"))


def load_session():
    try:
        with open(SESSION) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"token": None, "cwd": "/"}


def save_session(s):
    with open(SESSION, "w") as f:
        json.dump(s, f)


def resolve(s, p):
    return posixpath.normpath(posixpath.join(s["cwd"], p)) if p else s["cwd"]


def human(n):
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f}{u}" if u == "B" else f"{n:.1f}{u}"
        n /= 1024
    return f"{n:.1f}TB"


def main():
    ap = argparse.ArgumentParser(prog="dfs", description="Cliente del DFSha")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("login"); p.add_argument("user"); p.add_argument("--password")
    sub.add_parser("logout"); sub.add_parser("pwd"); sub.add_parser("cluster")
    p = sub.add_parser("ls"); p.add_argument("path", nargs="?")
    p = sub.add_parser("cd"); p.add_argument("path", nargs="?", default="/")
    p = sub.add_parser("stat"); p.add_argument("path")
    p = sub.add_parser("mkdir"); p.add_argument("path"); p.add_argument("-p", action="store_true")
    p = sub.add_parser("rmdir"); p.add_argument("path")
    p = sub.add_parser("rm"); p.add_argument("path")
    p = sub.add_parser("put"); p.add_argument("local"); p.add_argument("remote", nargs="?")
    p = sub.add_parser("get"); p.add_argument("remote"); p.add_argument("local", nargs="?")
    p = sub.add_parser("read"); p.add_argument("path"); p.add_argument("--offset", type=int, default=0)
    p.add_argument("--length", type=int, default=200)
    p = sub.add_parser("write"); p.add_argument("path"); p.add_argument("--offset", type=int)
    g = p.add_mutually_exclusive_group(required=True); g.add_argument("--text"); g.add_argument("--file")
    p.add_argument("--append", action="store_true", help="escribe al final del archivo")
    p = sub.add_parser("lockhold"); p.add_argument("path"); p.add_argument("seconds", type=float)
    p = sub.add_parser("locate"); p.add_argument("path")
    a = ap.parse_args()

    s = load_session()
    c = DFSClient(token=s.get("token"))
    try:
        if a.cmd == "login":
            pw = a.password or getpass.getpass("Contraseña: ")
            s["token"], s["cwd"] = c.login(a.user, pw), "/"
            save_session(s)
            print(f"Sesión iniciada como {a.user}")
        elif a.cmd == "logout":
            save_session({"token": None, "cwd": "/"})
        elif a.cmd == "pwd":
            print(s["cwd"])
        elif a.cmd == "cd":
            path = resolve(s, a.path)
            if c.stat(path)["type"] != "dir":
                raise DFSError(400, {"error": "NOT_A_DIR", "message": path})
            s["cwd"] = path
            save_session(s)
        elif a.cmd == "ls":
            for e in c.ls(resolve(s, a.path)):
                if e["type"] == "dir":
                    print(f"d  {'':>9}  {'':>4}  {e['name']}/")
                else:
                    print(f"-  {human(e['size']):>9}  v{e['version']:<3}  {e['name']}")
        elif a.cmd == "stat":
            print(json.dumps(c.stat(resolve(s, a.path)), indent=2))
        elif a.cmd == "mkdir":
            c.mkdir(resolve(s, a.path), a.p)
        elif a.cmd == "rmdir":
            c.rmdir(resolve(s, a.path))
        elif a.cmd == "rm":
            c.rm(resolve(s, a.path))
        elif a.cmd == "put":
            remote = resolve(s, a.remote or os.path.basename(a.local))
            t0 = time.time()
            r = c.put(a.local, remote)
            dt = time.time() - t0
            size = os.path.getsize(a.local)
            print(f"{remote} v{r['version']}  {human(size)} en {dt:.2f}s ({size / dt / 1e6:.1f} MB/s)")
        elif a.cmd == "get":
            remote = resolve(s, a.remote)
            local = a.local or posixpath.basename(remote)
            t0 = time.time()
            m = c.get(remote, local)
            dt = time.time() - t0
            print(f"{local} v{m['version']}  {human(m['size'])} en {dt:.2f}s ({m['size'] / max(dt, 1e-9) / 1e6:.1f} MB/s)  sha256 OK")
        elif a.cmd == "read":
            with c.open(resolve(s, a.path), "r") as f:
                sys.stdout.buffer.write(f.read(a.offset, a.length))
                print(f"\n[versión {f.version}, tamaño {f.size}]", file=sys.stderr)
        elif a.cmd == "write":
            data = a.text.encode() if a.text is not None else open(a.file, "rb").read()
            with c.open(resolve(s, a.path), "w") as f:
                off = f.size if a.append or a.offset is None else a.offset
                f.write(off, data)
                r = f.close()
            print(f"escritos {len(data)} bytes en offset {off}; versión {r['version'] if r else f.version}")
        elif a.cmd == "lockhold":
            path = resolve(s, a.path)
            lid = c.lock(path)
            print(f"lock tomado sobre {path} ({lid[:8]}), se mantiene {a.seconds}s", flush=True)
            end = time.time() + a.seconds
            while time.time() < end:
                time.sleep(min(10, max(0, end - time.time())))
                if time.time() < end:
                    c.renew(path, lid)
            c.unlock(path, lid)
            print("lock liberado")
        elif a.cmd == "locate":
            m = c.locate(resolve(s, a.path))
            print(f"{m['path']} v{m['version']} {human(m['size'])} en {len(m['blocks'])} bloques")
            for b in m["blocks"]:
                print(f"  bloque {b['idx']:>3} {b['id'][:12]}  {human(b['size']):>8}  en {[r['node'] for r in b['replicas']]}")
        elif a.cmd == "cluster":
            print(json.dumps(c.cluster(), indent=2))
    except DFSError as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
