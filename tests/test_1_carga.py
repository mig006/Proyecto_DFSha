"""Prueba 1 - Carga y recuperación.

Sube un archivo grande, muestra cómo quedaron repartidos sus bloques entre los
DataNodes, lo baja y compara el SHA-256 del original con el recuperado.
Uso: python tests/test_1_carga.py [MB]   (por defecto 100 MB)
"""
import os
import sys
import time

from util import TMP, check, client, make_file, replicas_by_node, sha, summary, title

mb = int(sys.argv[1]) if len(sys.argv) > 1 else 100
c = client()
title(f"PRUEBA 1: carga y recuperación de un archivo de {mb} MB")
src, dst = os.path.join(TMP, f"carga_{mb}.bin"), os.path.join(TMP, f"carga_{mb}.out")
make_file(src, mb)
c.mkdir("/pruebas", parents=True)

t0 = time.time()
r = c.put(src, "/pruebas/grande.bin")
t_put = time.time() - t0
print(f"  put: versión {r['version']} en {t_put:.2f}s ({mb / t_put:.1f} MB/s)")

m = c.locate("/pruebas/grande.bin")
dist = replicas_by_node(m)
print(f"  {len(m['blocks'])} bloques de {m['block_size'] // 2**20} MB; copias por nodo: {dist}")
for b in m["blocks"][:5]:
    print(f"    bloque {b['idx']}: {[x['node'] for x in b['replicas']]}")
check("el archivo quedó en varios nodos", len(dist) >= 3, f"{len(dist)} nodos")
check("cada bloque tiene 3 copias", all(len(b["replicas"]) >= 3 for b in m["blocks"]))

t0 = time.time()
c.get("/pruebas/grande.bin", dst)
t_get = time.time() - t0
print(f"  get: {t_get:.2f}s ({mb / t_get:.1f} MB/s)")
h1, h2 = sha(src), sha(dst)
print(f"  sha256 original:    {h1}\n  sha256 recuperado:  {h2}")
check("archivo recuperado idéntico (hash)", h1 == h2)
summary()
