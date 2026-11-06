#!/usr/bin/env bash
# Cliente contra el clúster local.  Ej: bash deploy/local/dfs.sh login miguel
# La carpeta deploy/local/files queda montada en /files para subir y bajar archivos.
# En Git Bash (Windows) evita que rutas como /session se conviertan en rutas de Windows.
export MSYS_NO_PATHCONV=1
cd "$(dirname "$0")"; mkdir -p files
HERE="$(pwd -W 2>/dev/null || pwd)"
docker compose run --rm -v "$HERE/files:/files" -v dfs_session:/session -e DFS_SESSION=/session/s.json client python src/client/dfs.py "$@"
