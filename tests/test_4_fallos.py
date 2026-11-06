"""Prueba 4 - Replicación y fallos.

1. Sube un archivo (3 copias por bloque).
2. Apaga un DataNode y lee el archivo de inmediato: debe salir idéntico usando las otras copias.
3. Escribe un archivo nuevo con el nodo caído (W = 2 copias basta).
4. Espera la detección (15 s) y la reposición automática de copias.
5. Apaga una instancia meta y un miembro de etcd: el sistema sigue respondiendo.

Por defecto la prueba pide hacer cada caída a mano (docker stop en la máquina del nodo).
Para automatizarla, defina los comandos:
  DFS_STOP_DN="docker stop dfs-dn3"   DFS_START_DN="docker start dfs-dn3"
  DFS_STOP_META="docker stop dfs-meta1 dfs-etcd1"   DFS_START_META="docker start dfs-etcd1 dfs-meta1"
"""
import os
import time

from util import TMP, check, client, make_file, replicas_by_node, run, sha, summary, title, wait

STOP_DN = os.environ.get("DFS_STOP_DN", "manual")
START_DN = os.environ.get("DFS_START_DN", "manual")
STOP_META = os.environ.get("DFS_STOP_META", "manual")
START_META = os.environ.get("DFS_START_META", "manual")
DEAD = os.environ.get("DFS_DEAD_NODE", "dn3")

c = client()
title("PRUEBA 4: caída de un DataNode y de una instancia de control")
src = make_file(os.path.join(TMP, "fallos.bin"), 40)
c.mkdir("/pruebas", parents=True)
c.put(src, "/pruebas/fallos.bin")
print(f"  copias por nodo antes: {replicas_by_node(c.locate('/pruebas/fallos.bin'))}")

print(f"\n  Se apaga {DEAD}:")
run(STOP_DN, f"Apague {DEAD}  (docker stop dfs-{DEAD})")
t0 = time.time()
c.get("/pruebas/fallos.bin", src + ".out")
check(f"lectura con {DEAD} caído, archivo idéntico", sha(src) == sha(src + ".out"), f"{time.time() - t0:.2f}s")

wait(16, "esperando a que meta detecte la caída (15 s sin heartbeat)")
nuevo = make_file(os.path.join(TMP, "fallos2.bin"), 8)
r = c.put(nuevo, "/pruebas/escrito_con_nodo_caido.bin")
m2 = c.locate("/pruebas/escrito_con_nodo_caido.bin")
check("escritura aceptada con un nodo caído", r["version"] >= 1,
      f"copias: {[len(b['replicas']) for b in m2['blocks']]}")
check(f"ninguna copia nueva en {DEAD}", all(DEAD not in [x["node"] for x in b["replicas"]] for b in m2["blocks"]))

wait(12, "esperando la reposición automática de copias")
m = c.locate("/pruebas/fallos.bin")
dist = replicas_by_node(m)
print(f"  copias por nodo después: {dist}")
check("cada bloque volvió a tener 3 copias vivas", all(len(b["replicas"]) >= 3 for b in m["blocks"]))
run(START_DN, f"Encienda {DEAD}  (docker start dfs-{DEAD})")

print("\n  Se apaga una instancia meta y un miembro de etcd:")
run(STOP_META, "Apague meta1 y etcd1  (docker stop dfs-meta1 dfs-etcd1)")
time.sleep(2)
t0 = time.time()
ok = True
try:
    c.ls("/pruebas")
    c.get("/pruebas/fallos.bin", src + ".out2")
    ok = sha(src) == sha(src + ".out2")
    c.put(nuevo, "/pruebas/con_meta_caido.bin")
except Exception as e:
    ok = False
    print("   ", e)
check("ls/get/put siguen funcionando sin meta1 ni etcd1", ok, f"{time.time() - t0:.2f}s")
run(START_META, "Encienda etcd1 y meta1  (docker start dfs-etcd1 dfs-meta1)")
summary()
