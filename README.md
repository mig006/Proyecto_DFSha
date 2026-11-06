# DFSha: sistema de archivos distribuido con alta disponibilidad

Proyecto 1 de Sistemas Distribuidos (2026-2), fase grupal. Integrantes: Miguel Moreno y Abel Santiago Garcia Ortiz.

Los archivos se parten en bloques de 4 MB, cada bloque se guarda cifrado en 3 DataNodes y los metadatos viven en un clúster etcd de 3 miembros. La arquitectura, las decisiones y las APIs están en [docs/ARQUITECTURA.md](docs/ARQUITECTURA.md).

```
README.md
src/                    código del sistema
  control/meta.py         servidor de metadatos "meta" (FastAPI, sin estado)
  datanode/datanode.py    nodo de datos (FastAPI, bloques cifrados)
  client/dfs.py           CLI del usuario
  client/dfs_client.py    librería cliente (put/get, open/read/write/lock/close)
  common/                 cliente de etcd y funciones de seguridad
  requirements.txt
tests/                  las 7 pruebas exigidas (test_1 ... test_7)
deploy/                 despliegue
  Dockerfile              imagen única para meta, DataNode y cliente
  gen_certs.sh            crea la CA y los certificados TLS
  .env.example            plantilla de configuración y secretos
  local/                  clúster completo en una máquina (docker compose)
  aws/                    despliegue en 3 EC2 de AWS Academy
docs/                   documentación
  ARQUITECTURA.md         matriz comparativa, decisiones, APIs y semántica
  Proyecto1_DFSha-20262_Rubrica.md   enunciado del proyecto
  DiseñosIndividuales/    propuestas de la fase I
```

## 1. Correr en local (una sola máquina con Docker)

Desde la raíz del repo:

```bash
bash deploy/gen_certs.sh                    # crea la CA y el certificado en deploy/certs
cp deploy/.env.example deploy/.env          # cambie los secretos
docker compose -f deploy/local/docker-compose.yml up -d --build    # 3 etcd, 2 meta, 4 DataNodes
docker compose -f deploy/local/docker-compose.yml ps
```

El cliente corre en un contenedor de la misma red. `deploy/local/dfs.sh` es un atajo y la carpeta `deploy/local/files` queda montada en `/files`:

```bash
bash deploy/local/dfs.sh login miguel --password miguel123
bash deploy/local/dfs.sh mkdir -p /videos
bash deploy/local/dfs.sh put /files/video.mp4 /videos/video.mp4
bash deploy/local/dfs.sh ls /videos
bash deploy/local/dfs.sh locate /videos/video.mp4      # en qué nodos quedó cada bloque
bash deploy/local/dfs.sh get /videos/video.mp4 /files/copia.mp4
bash deploy/local/dfs.sh write /notas.txt --text "hola"
bash deploy/local/dfs.sh write /notas.txt --offset 0 --text "HOLA"
bash deploy/local/dfs.sh write /notas.txt --append --text " mundo"
bash deploy/local/dfs.sh read /notas.txt
bash deploy/local/dfs.sh cluster                        # DataNodes vivos y meta líder
```

Comandos: `login, logout, ls, cd, pwd, stat, mkdir [-p], rmdir, rm, put, get, read, write, lockhold, locate, cluster`.

En Windows use Git Bash o WSL para correr los `.sh`.

## 2. Desplegar en AWS Academy (3 EC2)

1. **Crear 3 EC2 Ubuntu** (t3.small o t2.medium) en la misma VPC y subred, con un Security Group que permita:
   - SSH (22) desde su IP;
   - TCP 2379-2380, 8001 y 9001-9002 **desde el mismo Security Group**, que es el tráfico interno.
2. **En cada EC2:**
   ```bash
   git clone https://github.com/<usuario>/<repo>.git dfsha && cd dfsha
   bash deploy/aws/install_docker.sh && exit    # vuelva a entrar por SSH
   ```
3. **En la EC2-1**, generar los certificados con las 3 IPs privadas y preparar el `.env`:
   ```bash
   cd dfsha
   bash deploy/gen_certs.sh 10.0.1.10 10.0.1.11 10.0.1.12   # sus IPs privadas
   cp deploy/.env.example deploy/.env && nano deploy/.env    # secretos e IP1, IP2, IP3
   ```
   Copie `deploy/certs/` y `deploy/.env` a las otras dos máquinas, por ejemplo con `scp` desde su PC. **No los suba a GitHub.**
4. **Levantar cada máquina:**
   ```bash
   bash deploy/aws/up.sh host1     # en EC2-1
   bash deploy/aws/up.sh host2     # en EC2-2
   bash deploy/aws/up.sh host3     # en EC2-3
   ```
5. **Usar el cliente desde cualquier EC2** (`/tmp` del host queda montado):
   ```bash
   bash deploy/aws/dfs.sh login miguel --password miguel123
   bash deploy/aws/dfs.sh cluster
   ```

Las IPs privadas no cambian al detener y volver a iniciar el laboratorio, así que los certificados siguen sirviendo.

## 3. Pruebas

| # | Script | Qué demuestra |
|---|---|---|
| 1 | `test_1_carga.py [MB]` | Archivo grande repartido en varios nodos y recuperado con hash idéntico |
| 2 | `test_2_rf3.py` | open/read/write/lock/close con la semántica declarada |
| 3 | `test_3_concurrencia.py` | Dos clientes sobre el mismo bloque; escritor con lease vencido rechazado |
| 4 | `test_4_fallos.py` | Caída de un DataNode (lectura, escritura y reposición) y caída de meta1 y etcd1 |
| 5 | `test_5_consistencia.py` | Réplica desconectada y cliente que muere antes del commit |
| 6 | `test_6_seguridad.py` | Accesos autorizados y denegados, TLS, tokens de bloque, cifrado en disco |
| 7 | `test_7_rendimiento.py [N]` | Latencia y throughput por tamaño, paralelismo y lectores simultáneos (CSV) |

```bash
bash deploy/local/test.sh test_1_carga.py 100    # local
bash deploy/aws/test.sh test_1_carga.py 100      # AWS
```

En las pruebas 4 y 5, el script se detiene y pide apagar o encender un nodo a mano: por ejemplo, `docker stop dfs-dn3` en la máquina de ese nodo. Luego se presiona Enter para seguir. Así se ve bien en el video.

Para verificar el cifrado en disco, en la máquina de un DataNode:

```bash
docker exec dfs-dn1 sh -c 'cat /data/b-* | grep -c TEXTO-SECRETO'    # debe dar 0
```

## 4. Configuración

Todo se configura en `deploy/.env` (ver `.env.example`):

| Variable | Uso |
|---|---|
| `DFS_JWT_SECRET`, `DFS_BLOCK_SECRET`, `DFS_INTERNAL_TOKEN`, `DFS_NODE_KEY` | Secretos compartidos |
| `DFS_USERS` | Usuarios iniciales, en formato `usuario:clave,usuario:clave` |
| `DFS_REPLICATION`, `DFS_WRITE_QUORUM` | R y W (por defecto 3 y 2) |
| `DFS_GC_GRACE` | Segundos antes de borrar un bloque sin referencia |
| `IP1`, `IP2`, `IP3` | IPs privadas de las EC2 (solo AWS) |
