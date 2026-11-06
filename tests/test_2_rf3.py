"""Prueba 2 - Acceso RF3: open, read, write, lock, close con la semántica declarada.

- read() por rangos, incluso cruzando el límite entre dos bloques.
- write() parcial en medio del archivo (cruzando bloques) y anexado al final.
- write() más allá del final se rechaza (no se admiten huecos).
- Visibilidad de sesión: quien abrió antes sigue viendo su versión; al reabrir ve la nueva.
"""
from util import DFSError, check, client, summary, title

c = client()
title("PRUEBA 2: acceso RF3 (open/read/write/lock/close)")
BS = 4 * 1024 * 1024
path = "/pruebas/rf3.bin"
c.mkdir("/pruebas", parents=True)

# Crear un archivo de 2 bloques y medio: bloque 0 = 'A', bloque 1 = 'B', resto 'C'
with c.open(path, "w") as f:
    f.write(0, b"A" * BS + b"B" * BS + b"C" * (BS // 2))
    v1 = f.close()["version"]
print(f"  creado {path}: {c.stat(path)['size']} bytes, versión {v1}")

with c.open(path, "r") as f:
    data = f.read(BS - 3, 6)
print(f"  read(offset={BS - 3}, 6) = {data}  (cruza del bloque 0 al 1)")
check("read cruzando bloques", data == b"AAABBB")

lector_viejo = c.open(path, "r")  # este lector queda fijado en la versión actual

with c.open(path, "w") as f:  # open('w') toma el lock automáticamente
    f.write(BS - 2, b"xyz!")          # modificación parcial que cruza bloques
    f.write(f.size, b"-FIN")          # anexado
    print(f"  dentro de la sesión (lee lo propio): {f.read(BS - 2, 4)}")
    v2 = f.close()["version"]
print(f"  commit al cerrar: versión {v2}")
check("una sesión de escritura = una versión nueva", v2 == v1 + 1)

check("lector abierto antes sigue viendo su versión", lector_viejo.read(BS - 2, 4) == b"AABB",
      lector_viejo.read(BS - 2, 4))
with c.open(path, "r") as f:
    nuevo = f.read(BS - 2, 4)
    fin = f.read(f.size - 4, 4)
check("quien reabre ve los cambios (close-to-open)", nuevo == b"xyz!", nuevo)
check("anexado al final", fin == b"-FIN", fin)

try:
    with c.open(path, "w") as f:
        f.write(f.size + 100, b"hueco")
    check("write con hueco se rechaza", False)
except DFSError as e:
    check("write con hueco se rechaza", e.code == "INVALID_OFFSET", str(e))

lid = c.lock(path)
try:
    c.lock(path, wait=False)
    check("lock exclusivo", False)
except DFSError as e:
    check("lock exclusivo: un segundo lock recibe 423 LOCKED", e.status == 423, str(e))
c.unlock(path, lid)
summary()
