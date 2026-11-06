#!/usr/bin/env bash
# Cliente contra el clúster local.  Ej: bash deploy/local/dfs.sh login miguel
# La carpeta deploy/local/files queda montada en /files para subir y bajar archivos.
cd "$(dirname "$0")"; mkdir -p files
docker compose run --rm -v "$(pwd)/files:/files" -v dfs_session:/session -e DFS_SESSION=/session/s.json client python src/client/dfs.py "$@"
