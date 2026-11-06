#!/usr/bin/env bash
# Corre una prueba contra el clúster local.  Ej: bash deploy/local/test.sh test_1_carga.py 100
export MSYS_NO_PATHCONV=1
cd "$(dirname "$0")"
TESTS="$(cd ../../tests && (pwd -W 2>/dev/null || pwd))"
docker compose run --rm -v "$TESTS:/app/tests" client python "tests/$1" "${@:2}"
