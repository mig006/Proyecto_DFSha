"""Prueba 3 - Concurrencia: dos clientes escriben el MISMO bloque al mismo tiempo.

Política: lock exclusivo por archivo con lease. El primero obtiene el lock y el
segundo espera (recibe 423 y reintenta). Al final hay dos versiones nuevas, ningún
cambio se pierde y no se mezclan bytes.
También se prueba que un escritor cuyo lock venció NO puede publicar (STALE_LOCK).
"""
import time
from multiprocessing import Process, Queue

from util import DFSError, check, client, summary, title

PATH = "/pruebas/concurrente.txt"


def writer(name, offset, text, q, hold):
    c = client()
    t0 = time.time()
    f = c.open(PATH, "w")
    got = time.time()
    f.write(offset, text.encode())
    time.sleep(hold)  # simula trabajo mientras tiene el lock
    v = f.close()["version"]
    q.put((name, round(t0, 2), round(got, 2), round(time.time(), 2), v))


if __name__ == "__main__":
    c = client()
    title("PRUEBA 3: dos clientes sobre el mismo bloque")
    c.mkdir("/pruebas", parents=True)
    with c.open(PATH, "w") as f:
        f.write(0, b"0123456789" * 3)
    base = c.stat(PATH)["version"]
    print(f"  archivo inicial v{base}: {c.open(PATH).read(0, 30)}")

    q = Queue()
    p1 = Process(target=writer, args=("cliente-1", 0, "UNO", q, 3))
    p2 = Process(target=writer, args=("cliente-2", 10, "DOS", q, 0))
    p1.start(); time.sleep(0.3); p2.start()
    p1.join(); p2.join()
    res = sorted([q.get(), q.get()], key=lambda r: r[2])
    t_ref = min(r[1] for r in res)
    for name, t0, got, end, v in res:
        print(f"  {name}: pidió el lock en t={t0 - t_ref:.2f}s, lo obtuvo en t={got - t_ref:.2f}s "
              f"(esperó {got - t0:.2f}s), cerró en t={end - t_ref:.2f}s y publicó v{v}")
    check("el segundo esperó a que el primero cerrara", res[1][2] >= res[0][3] - 0.05)
    final = c.open(PATH).read(0, 30)
    print(f"  contenido final v{c.stat(PATH)['version']}: {final}")
    check("dos versiones nuevas, una por escritor", c.stat(PATH)["version"] == base + 2)
    check("no se pierde ningún cambio ni se mezclan bytes", final == b"UNO3456789DOS3456789" + b"0123456789")

    # Escritor con lock vencido (fencing): pide un lock de 5 s, se "congela" 8 s e intenta publicar.
    title("Escritor con lease vencido")
    lid = c.lock(PATH, ttl=5)
    m = c.locate(PATH)
    print("  lock tomado con lease de 5 s; el cliente se pausa 8 s sin renovar...")
    time.sleep(8)
    other = client()
    lid2 = other.lock(PATH, wait=False)
    print("  otro cliente obtuvo el lock al vencer el anterior")
    try:
        blocks = [{"id": b["id"], "sha256": b["sha256"], "size": b["size"]} for b in m["blocks"]]
        c._commit(PATH, lid, m["version"], blocks, m["size"])
        check("commit con lock vencido se rechaza", False)
    except DFSError as e:
        check("commit con lock vencido se rechaza (STALE_LOCK)", e.code == "STALE_LOCK", str(e))
    other.unlock(PATH, lid2)
    summary()
