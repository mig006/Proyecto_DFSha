"""Prueba 7 - Rendimiento: latencia y throughput bajo varias condiciones.

Condición A: tamaño del archivo (10, 50, 100 MB).
Condición B: paralelismo del cliente (1 bloque a la vez vs 8 en paralelo).
Condición C: varios clientes leyendo al mismo tiempo (1 vs 4).
Cada medición se repite N veces y se reporta mediana y p95. Los resultados quedan en
tests/resultados_rendimiento.csv para el informe.
Uso: python tests/test_7_rendimiento.py [repeticiones]
"""
import csv
import os
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor

from util import TMP, client, make_file, title

N = int(sys.argv[1]) if len(sys.argv) > 1 else 5
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "resultados_rendimiento.csv")
rows = []


def p95(xs):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(0.95 * (len(xs) - 1))))]


def measure(cond, op, mb, fn):
    times = []
    for _ in range(N):
        t0 = time.time()
        fn()
        times.append(time.time() - t0)
    med = statistics.median(times)
    rows.append({"condicion": cond, "operacion": op, "MB": mb, "mediana_s": round(med, 3),
                 "p95_s": round(p95(times), 3), "MBps_mediana": round(mb / med, 1)})
    print(f"  {cond:<28} {op:<4} {mb:>4} MB  mediana {med:6.2f}s  p95 {p95(times):6.2f}s  {mb / med:7.1f} MB/s")


title(f"PRUEBA 7: rendimiento ({N} repeticiones por medición)")
c = client()
c.mkdir("/bench", parents=True)

for mb in (10, 50, 100):
    f = make_file(os.path.join(TMP, f"bench_{mb}.bin"), mb)
    measure("tamaño", "put", mb, lambda: c.put(f, f"/bench/f{mb}.bin"))
    measure("tamaño", "get", mb, lambda: c.get(f"/bench/f{mb}.bin", f + ".out"))

f100 = os.path.join(TMP, "bench_100.bin")
for par in (1, 8):
    cp = client(parallel=par)
    measure(f"paralelismo={par}", "put", 100, lambda: cp.put(f100, "/bench/par.bin"))
    measure(f"paralelismo={par}", "get", 100, lambda: cp.get("/bench/par.bin", f100 + ".par"))

for nclients in (1, 4):
    cs = [client() for _ in range(nclients)]

    def many():
        with ThreadPoolExecutor(nclients) as ex:
            list(ex.map(lambda i: cs[i].get("/bench/f50.bin", os.path.join(TMP, f"multi_{i}.out")), range(nclients)))

    measure(f"lectores_simultaneos={nclients}", "get", 50 * nclients, many)

with open(OUT, "w", newline="") as fh:
    w = csv.DictWriter(fh, fieldnames=rows[0].keys())
    w.writeheader()
    w.writerows(rows)
print(f"\nResultados guardados en {OUT}")
