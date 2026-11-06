"""Prueba 6 - Seguridad.

- Autenticación: contraseña mala y token falso/ausente => 401.
- Autorización: abel no puede ver ni tocar los archivos de miguel.
- Tokens de bloque: un DataNode rechaza peticiones sin token, con token alterado
  o con un token emitido para otro bloque.
- Canal: meta y DataNodes solo hablan HTTPS (una conexión HTTP plana falla).
- Reposo: los bloques en disco están cifrados (no aparece el texto original).
  Para revisar el disco: DFS_DATA_DIR=/ruta/a/los/datos  o  DFS_DISK_CMD="docker exec dfs-dn1 sh -c 'cat /data/*'"
"""
import os
import subprocess

import requests

from util import USER, DFSClient, DFSError, check, client, summary, title

title("PRUEBA 6: seguridad")
SECRET = b"TEXTO-SECRETO-DFSHA-" * 50
c = client()
c.mkdir("/pruebas", parents=True)
with c.open("/pruebas/secreto.txt", "w") as f:
    f.write(0, SECRET)

anon = DFSClient()
try:
    anon.login(USER, "contraseña-mala")
    check("contraseña incorrecta => 401", False)
except DFSError as e:
    check("contraseña incorrecta => 401", e.status == 401, str(e))
try:
    anon.ls("/")
    check("sin token => 401", False)
except DFSError as e:
    check("sin token => 401", e.status == 401, str(e))
try:
    DFSClient(token="eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJtaWd1ZWwifQ.falso").ls("/")
    check("token falso => 401", False)
except DFSError as e:
    check("token falso => 401", e.status == 401, str(e))

abel = client(os.environ.get("DFS_TEST_USER2", "abel"), os.environ.get("DFS_TEST_PASS2", "abel123"))
names = [e["name"] for e in abel.ls("/")]
check("abel no ve el espacio de miguel", "pruebas" not in names or not abel.exists("/pruebas/secreto.txt"), names)
try:
    abel.locate("/pruebas/secreto.txt")
    check("abel no puede leer /pruebas/secreto.txt de miguel", False)
except DFSError as e:
    check("abel no puede leer /pruebas/secreto.txt de miguel", e.status == 404, str(e))
try:
    abel.locate("/../miguel/pruebas/secreto.txt")
    check("no se puede escapar del espacio propio con ..", False)
except DFSError as e:
    check("no se puede escapar del espacio propio con ..", e.status == 404, str(e))

m = c.locate("/pruebas/secreto.txt")
b = m["blocks"][0]
url = f"{b['replicas'][0]['url']}/blocks/{b['id']}"
s = requests.Session()
s.trust_env = False
s.verify = c.verify
check("DataNode con token válido => 200", s.get(url, headers={"X-Block-Token": b["token"]}).status_code == 200)
check("DataNode sin token => 401", s.get(url).status_code == 401)
bad = b["token"][:-4] + ("AAAA" if not b["token"].endswith("AAAA") else "BBBB")
check("DataNode con token alterado => 401", s.get(url, headers={"X-Block-Token": bad}).status_code == 401)
other = c.locate("/pruebas/secreto.txt")["blocks"][0]["token"]
fake_id = url.rsplit("/", 1)[0] + "/b-00000000000000000000000000000000"
check("token de un bloque no sirve para otro", s.get(fake_id, headers={"X-Block-Token": other}).status_code == 401)
check("escribir en un DataNode con token de lectura => 401",
      s.put(url, data=b"x", headers={"X-Block-Token": b["token"], "X-SHA256": "0"}).status_code == 401)

plain = c.metas[0].replace("https://", "http://")
try:
    requests.get(plain + "/health", timeout=3)
    check("meta no acepta HTTP sin cifrar", False)
except requests.RequestException as e:
    check("meta no acepta HTTP sin cifrar (solo TLS)", True, e.__class__.__name__)

data_dir = os.environ.get("DFS_DATA_DIR")
disk_cmd = os.environ.get("DFS_DISK_CMD")
raw = b""
if data_dir:
    for root, _, files in os.walk(data_dir):
        for fn in files:
            with open(os.path.join(root, fn), "rb") as f:
                raw += f.read()
elif disk_cmd:
    raw = subprocess.run(disk_cmd, shell=True, capture_output=True).stdout
if raw:
    check("los bloques en disco están cifrados (no aparece el texto)", b"TEXTO-SECRETO" not in raw, f"{len(raw)} bytes revisados")
else:
    print("  Para revisar el cifrado en disco, en la máquina de un DataNode ejecute:")
    print("    docker exec dfs-dn1 sh -c 'cat /data/b-* | grep -c TEXTO-SECRETO'")
    print("  Debe responder 0: el texto no aparece porque los bloques están cifrados con AES-256-GCM.")
summary()
