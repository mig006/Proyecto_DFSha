"""Prueba 5 - Consistencia con una réplica desconectada y escrituras interrumpidas.

1. Con una réplica desconectada se publica una versión nueva: los lectores solo ven
   versiones confirmadas y el mapa solo apunta a copias vivas.
2. Un lector que abrió antes sigue leyendo su versión completa.
3. Un cliente que sube bloques pero muere antes del commit NO publica nada:
   el archivo sigue en la versión anterior.
"""
import os

from util import check, client, run, summary, title, wait

STOP_DN = os.environ.get("DFS_STOP_DN", "manual")
START_DN = os.environ.get("DFS_START_DN", "manual")
DEAD = os.environ.get("DFS_DEAD_NODE", "dn3")
PATH = "/pruebas/consistencia.txt"

c = client()
title("PRUEBA 5: consistencia con réplica desconectada")
c.mkdir("/pruebas", parents=True)
with c.open(PATH, "w") as f:
    f.write(0, b"version-1 " * 1000)
v1 = c.stat(PATH)["version"]
lector = c.open(PATH, "r")
print(f"  versión inicial v{v1}; un lector la deja abierta")

run(STOP_DN, f"Apague {DEAD}  (docker stop dfs-{DEAD})")
wait(16, f"esperando a que {DEAD} quede marcado como caído")
with c.open(PATH, "w") as f:
    f.write(0, b"version-2 " * 1000)
v2 = c.stat(PATH)["version"]
m = c.locate(PATH)
print(f"  publicada v{v2} con {DEAD} desconectado; copias: {[[r['node'] for r in b['replicas']] for b in m['blocks']]}")
check("la nueva versión quedó confirmada", v2 == v1 + 1)
check(f"el mapa no apunta a {DEAD}", all(DEAD not in [r["node"] for r in b["replicas"]] for b in m["blocks"]))
with c.open(PATH) as f:
    check("un lector nuevo ve la versión 2 completa", f.read(0, 10) == b"version-2 ")
check("el lector antiguo sigue viendo la versión 1 completa", lector.read(0, 10) == b"version-1 ")

title("Escritura interrumpida (cliente muere antes del commit)")
lid = c.lock(PATH)
a = c._meta("POST", "/api/v1/blocks/alloc", json={"count": 1})
blk = c._upload_block(a["blocks"][0], b"datos-que-nunca-se-publican", 2, 3)
print(f"  bloque subido a {blk['replicas']} pero sin commit; el cliente 'muere' (no renueva el lock)")
c.renew(PATH, lid)  # simulamos que muere: no se vuelve a renovar ni se hace commit
wait(31, "esperando a que venza el lease del lock (30 s)")
st = c.stat(PATH)
check("el archivo sigue en la última versión confirmada", st["version"] == v2, f"v{st['version']}")
with c.open(PATH) as f:
    check("el contenido no cambió", f.read(0, 10) == b"version-2 ")
lid2 = c.lock(PATH, wait=False)
check("el lock se liberó solo al vencer el lease", bool(lid2))
c.unlock(PATH, lid2)

run(START_DN, f"Encienda {DEAD}  (docker start dfs-{DEAD})")
wait(6, f"{DEAD} vuelve; sus bloques viejos no los usa nadie y la limpieza (GC) los borra")
summary()
