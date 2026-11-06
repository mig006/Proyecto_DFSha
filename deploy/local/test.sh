#!/usr/bin/env bash
# Corre una prueba contra el clúster local.  Ej: bash deploy/local/test.sh test_1_carga.py 100
cd "$(dirname "$0")"
docker compose run --rm -v "$(pwd)/../../tests:/app/tests" client python "tests/$1" "${@:2}"
