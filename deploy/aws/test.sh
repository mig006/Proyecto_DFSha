#!/usr/bin/env bash
# Corre una prueba desde una EC2.  Ej: bash test.sh test_1_carga.py 100
cd "$(dirname "$0")"
docker compose --env-file ../.env run --rm -v /tmp:/tmp -v "$(pwd)/../../tests:/app/tests" client python "tests/$1" "${@:2}"
