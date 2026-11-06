# DFSha: arquitectura del equipo

Equipo: Miguel Moreno y Abel Santiago Garcia Ortiz. Opción 1: cliente/servidor con servicio distribuido entre servidores (S2S) en una red privada.

## 1. Matriz comparativa de las propuestas individuales

| Aspecto | Propuesta de Miguel | Propuesta de Abel | Decisión del equipo |
|---|---|---|---|
| Plano de control | Un servidor de control primario y uno de respaldo, con journal propio, épocas y testigos | Varias instancias `meta` sin estado; los metadatos viven en etcd (Raft) | **Abel**: etcd ya resuelve el consenso, no hay que programar el failover |
| Metadatos | En memoria, respaldados por el journal | En etcd, con transacciones con comparación | **Abel** |
| Locks | Lease propio por rango de bloques | Lease de etcd por rango de bloques, con fencing token | **Mezcla**: lease de etcd, pero un lock por archivo |
| Tamaño de bloque | 4 MB | 8 MiB | **Miguel**: 4 MB dan más bloques y se ve mejor el reparto en las pruebas |
| Bloques | Inmutables, copy-on-write | Inmutables, copy-on-write | Igual en ambas |
| Réplicas | R = 3, W = 2, copia en cadena | R = 3, W = 2, copia en cadena | R = 3 y W = 2; **el cliente escribe a las réplicas en paralelo** |
| Semántica de RF3 | open/read/write/lock/close; visibilidad de sesión | Modos r, w, a, r+, flush, mv; close-to-open | **Miguel**: menos primitivas, misma garantía |
| Protocolos | REST para el control y gRPC para los datos | REST para el control y gRPC para los datos | **Todo REST sobre HTTPS** (FastAPI) |
| Seguridad | JWT, bcrypt, tokens de bloque HMAC, TLS 1.3, mTLS, AES-256-GCM | JWT Ed25519, Argon2id, tokens de bloque, mTLS, AES-GCM con HKDF | JWT, scrypt, tokens de bloque HMAC, HTTPS, mTLS en etcd, AES-256-GCM |
| Descubrimiento | Nombre DNS interno del control | Nombre DNS con las direcciones de `meta` | El cliente solo conoce la lista de `meta` (`DFS_META`) |
| Reparación de copias | La hace el control primario | Coordinador elegido con un lease | Una `meta` líder, elegida con un lease en etcd |

## 2. Registro de decisiones (ADR)

| # | Decisión | Por qué | Costo |
|---|---|---|---|
| D1 | Metadatos en un clúster de 3 etcd | Raft da consistencia fuerte y tolera la caída de 1 de 3 sin que escribamos código de consenso | Sin quórum (2 de 3 caídos) no hay operaciones de metadatos |
| D2 | `meta` sin estado, 2 instancias | Si una cae, el cliente usa la otra | Ninguno relevante: toda la verdad está en etcd |
| D3 | Bloques fijos de 4 MB, inmutables | `bloque = offset / 4 MB`; las réplicas no pueden divergir y nunca se ve un bloque a medias | Cambiar 1 byte reescribe 4 MB |
| D4 | R = 3, W = 2 | Las escrituras siguen con un nodo caído y la tercera copia se repone sola | Ventana corta con solo 2 copias |
| D5 | El cliente sube cada bloque a las réplicas en paralelo | Es más simple que la cadena y si un nodo falla se prueba con el siguiente | Usa más ancho de banda del cliente |
| D6 | Lock exclusivo por archivo con lease de 30 s en etcd | Si el cliente muere el lock vence solo, y el commit compara el lock dentro de la misma transacción (fencing) | Dos escritores de bloques distintos del mismo archivo se serializan |
| D7 | Commit atómico: una sola transacción de etcd | Exige el lock vigente, la versión base y W copias vivas por bloque nuevo | Ninguno |
| D8 | Semántica de sesión (close-to-open) | Garantía simple y comprobable | Los cambios ajenos se ven al reabrir |
| D9 | Todo REST/HTTPS con FastAPI | Un solo framework, fácil de probar y de explicar | Algo menos eficiente que gRPC para binarios |
| D10 | Heartbeat cada 3 s; nodo caído a los 15 s | Las órdenes van por la respuesta, sin cola de mensajes | Unos 15 s para detectar una caída |
| D11 | GC de bloques sin referencia tras 10 min | Limpia cargas abortadas, versiones viejas y archivos borrados, sin romper a los lectores que tienen abierta una versión anterior | Un lector puede leer una versión vieja hasta 10 min |

## 3. Simplificaciones declaradas frente a los diseños individuales

1. **Sin gRPC**: todo el tráfico va por REST/HTTPS.
2. **Sin replicación en cadena**: el cliente escribe en paralelo a las réplicas.
3. **Lock por archivo**, no por rango de bloques.
4. **mTLS solo en etcd**. Entre `meta` y los DataNodes se usa HTTPS más un token interno compartido (`DFS_INTERNAL_TOKEN`). Los tokens de bloque protegen el acceso de los clientes a los DataNodes.
5. **Primitivas de RF3**: `open(r|w)`, `read`, `write`, `lock`, `unlock` y `close`. Quedan fuera `mv`, `flush` y los modos `a` y `r+`. El anexado y la modificación parcial se hacen con `write(offset)`.
6. **Sin versiones históricas**: las versiones viejas se borran con el GC.
7. **El último bloque no se rellena**: se guarda con su tamaño real.
8. Una sola llave de cifrado en reposo por despliegue (`DFS_NODE_KEY`), sin derivar llaves por bloque.

## 4. Arquitectura final

```
                     Cliente (CLI dfs / librería)
             solo conoce DFS_META = [meta1, meta2]
              |  REST/HTTPS + JWT          |  HTTPS + token de bloque
              v                            v
   +----------------------+      +--------------------------------+
   | meta1   meta2        | <--- | dn1   dn2   dn3   dn4          |
   | (FastAPI, sin estado)| hb   | bloques cifrados AES-256-GCM   |
   +----------+-----------+      +--------------------------------+
              | HTTPS + mTLS             ^  replicación (orden de meta líder)
              v                          |
   +------------------------+            |
   | etcd1  etcd2  etcd3    | -----------+
   | (Raft, metadatos/locks)|
   +------------------------+
```

**Despliegue en AWS Academy (3 EC2 dentro de la misma VPC):**

| EC2 | Contenedores |
|---|---|
| EC2-1 | etcd1, meta1, dn1 (y el cliente) |
| EC2-2 | etcd2, meta2, dn2 |
| EC2-3 | etcd3, dn3, dn4 |

### Claves en etcd

| Clave | Contenido |
|---|---|
| `/users/<u>` | hash scrypt de la contraseña |
| `/fs/<u><ruta>` | directorio `{"type":"dir"}` o archivo `{"type":"file","version","size","sha256","blocks":[{"id","sha256","size","replicas"}]}` |
| `/dn/<id>` | DataNode vivo `{"url","free","blocks"}`, con lease de 15 s |
| `/locks/<u><ruta>` | `{"lock_id","lease","user"}`, con lease de 30 s |
| `/leader` | la `meta` que repara copias y hace el GC, con lease de 10 s |

## 5. APIs

### meta (prefijo `/api/v1`, JWT en `Authorization: Bearer`)

| Método y ruta | Uso | Primitiva |
|---|---|---|
| `POST /auth/login` | Devuelve el JWT (1 h) | — |
| `GET /fs/ls?path=` · `GET /fs/stat?path=` | Listar y ver atributos | ls, cd, stat |
| `POST /fs/mkdir` · `DELETE /fs/rmdir?path=` · `DELETE /fs/rm?path=` | Crear y borrar | mkdir, rmdir, rm |
| `POST /locks` · `PUT /locks/{id}?path=` · `DELETE /locks/{id}?path=` | Pedir, renovar y soltar el lock | lock, unlock |
| `POST /blocks/alloc` | Ids de bloque nuevos, nodos destino y tokens de escritura | put, write |
| `GET /files/locate?path=` | Mapa del archivo: bloques, copias vivas y tokens de lectura | get, open, read |
| `POST /files/commit` | Publica la versión nueva (atómico e idempotente) | put, close |
| `GET /admin/cluster` | Nodos vivos y líder | — |

### DataNode

| Método y ruta | Uso |
|---|---|
| `PUT /blocks/{id}` (`X-Block-Token`, `X-SHA256`) | Guardar un bloque: revisa el hash, cifra, escribe `.part` y lo renombra |
| `GET /blocks/{id}` (`X-Block-Token`) | Leer un bloque |
| `POST /internal/replicate` · `GET /internal/blocks` · `DELETE /internal/blocks/{id}` | Reposición de copias y GC (token interno) |

**Errores:** `401 TOKEN_INVALID/BAD_CREDENTIALS`, `403 FORBIDDEN`, `404 NOT_FOUND`, `400 INVALID_OFFSET`, `409 VERSION_CONFLICT/STALE_LOCK/DIR_NOT_EMPTY/CONFLICT`, `422 INSUFFICIENT_REPLICAS/DATA_LOSS`, `423 LOCKED`, `503 NOT_ENOUGH_NODES`.

## 6. Semántica de read() y write()

| Aspecto | Decisión |
|---|---|
| Creación | Sí, con `put` u `open(path, "w")` |
| Reemplazo completo | Sí, `put` sobre un archivo existente crea la versión siguiente |
| Anexado | Sí, `write(offset = tamaño actual)` |
| Modificación parcial | Sí, en cualquier offset hasta el tamaño actual; por dentro se reescriben los bloques afectados |
| Huecos | No: `offset > tamaño` responde `400 INVALID_OFFSET` |
| `read(offset, len)` | Lee la versión fijada en `open()`, en paralelo si abarca varios bloques |
| Visibilidad | De sesión: los cambios se ven cuando `close()` los confirma |
| Concurrencia | Lectores sin límite y sin lock; un escritor por archivo a la vez |

**Garantía comprobable.** Una escritura está confirmada cuando `commit` responde 200 con la versión `v`. En ese momento:
- cada bloque nuevo está en al menos 2 DataNodes vivos con el hash revisado;
- el cambio quedó en etcd, replicado por Raft;
- todo `open()` posterior, contra cualquier `meta`, ve `v` o una versión más nueva;
- nadie puede leer una versión sin confirmar;
- un escritor cuyo lock venció no puede publicar (`409 STALE_LOCK`).

## 7. Escenarios obligatorios

- **Dos procesos escriben el mismo bloque:** el primero obtiene el lock y el segundo recibe `423 LOCKED` y reintenta con espera creciente. Cuando el primero cierra, el segundo obtiene el lock, lee la versión nueva y escribe encima. No se mezclan bytes y no se pierde ningún cambio (prueba 3).
- **Réplica desconectada durante una escritura:** el cliente sube el bloque a los nodos que responden. Con W = 2 el commit se acepta y el mapa solo apunta a nodos vivos. Cuando meta detecta la caída, el líder repone la tercera copia. Los lectores no notan nada (pruebas 4 y 5).
- **Nodo que falla con un bloque a medias:** el pedazo queda en `<id>.part`, que nunca se renombra y se borra al reiniciar el nodo. El cliente prueba con otro nodo. El commit exige W copias completas por bloque, así que no hay forma de publicar un archivo incompleto (prueba 5).
- **El cliente no conoce dónde están los bloques:** solo conoce la lista de `meta`. `GET /files/locate` le dice, bloque por bloque, qué nodos vivos lo tienen y en qué dirección, según lo que cada DataNode reporta en su heartbeat (RNF7).

## 8. Cómo se cumplen los requisitos no funcionales

| RNF | Cómo se cumple |
|---|---|
| RNF1 Escalabilidad | `meta` sin estado; los datos no pasan por `meta`; un DataNode nuevo se registra solo con su heartbeat |
| RNF2 Disponibilidad | R = 3; etcd tolera 1 caído de 3; 2 `meta`; reposición automática de copias |
| RNF3 Consistencia | Commit atómico en etcd con versión base y lock; semántica de sesión |
| RNF4 Particionamiento | Bloques de 4 MB repartidos rotando el nodo inicial |
| RNF5 Rendimiento | Descarga y carga de bloques en paralelo desde varios nodos |
| RNF6 Seguridad | JWT, espacio por usuario, tokens de bloque HMAC, HTTPS, mTLS en etcd, AES-256-GCM en reposo |
| RNF7 Transparencia | El cliente solo conoce `DFS_META`; descubre los DataNodes con `locate` |
