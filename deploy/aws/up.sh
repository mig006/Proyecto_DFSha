#!/usr/bin/env bash
# Levanta los servicios de esta EC2.  Uso: bash up.sh host1|host2|host3
set -e
cd "$(dirname "$0")"
[ -f ../.env ] || { echo "Falta deploy/.env (copie .env.example y ponga las IPs)"; exit 1; }
[ -f ../certs/node.crt ] || { echo "Faltan los certificados en deploy/certs"; exit 1; }
docker compose --env-file ../.env --profile "$1" up -d --build
docker compose --env-file ../.env --profile "$1" ps
