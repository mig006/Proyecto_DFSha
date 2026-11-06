#!/usr/bin/env bash
# Instala Docker y el plugin compose en una EC2 con Ubuntu.
set -e
sudo apt-get update -y
sudo apt-get install -y ca-certificates curl git openssl
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker "$USER"
echo "Listo. Cierre la sesión SSH y vuelva a entrar para usar docker sin sudo."
