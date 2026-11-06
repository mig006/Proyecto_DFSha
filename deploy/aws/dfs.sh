#!/usr/bin/env bash
# Atajo para usar el cliente desde una EC2.  Ej: bash dfs.sh put /tmp/a.bin /a.bin
# Monta /tmp del host para poder subir y bajar archivos, y guarda la sesión ahí.
cd "$(dirname "$0")"
docker compose --env-file ../.env run --rm -v /tmp:/tmp -e DFS_SESSION=/tmp/.dfs_session.json client python src/client/dfs.py "$@"
