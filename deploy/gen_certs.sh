#!/usr/bin/env bash
# Genera una CA propia y un certificado para todos los nodos (HTTPS / TLS).
# Uso:  bash deploy/gen_certs.sh [IP1 IP2 IP3 ...]
# Pasa las IPs privadas de las EC2 para que el certificado sea válido en AWS.
set -euo pipefail
# En Git Bash (Windows) evita que "/CN=..." se convierta en una ruta de Windows.
export MSYS_NO_PATHCONV=1
cd "$(dirname "$0")"
mkdir -p certs && cd certs

SAN="DNS:localhost,IP:127.0.0.1"
for n in etcd1 etcd2 etcd3 meta1 meta2 dn1 dn2 dn3 dn4 dn5 dn6; do SAN="$SAN,DNS:$n"; done
for ip in "$@"; do SAN="$SAN,IP:$ip"; done

openssl req -x509 -newkey rsa:2048 -nodes -days 365 -subj "/CN=DFSha CA" \
  -addext "basicConstraints=critical,CA:TRUE" -addext "keyUsage=critical,keyCertSign,cRLSign" \
  -addext "subjectKeyIdentifier=hash" -keyout ca.key -out ca.crt 2>/dev/null
openssl req -newkey rsa:2048 -nodes -subj "/CN=dfsha-node" \
  -keyout node.key -out node.csr 2>/dev/null
printf "subjectAltName=%s\nextendedKeyUsage=serverAuth,clientAuth\nkeyUsage=critical,digitalSignature,keyEncipherment\nbasicConstraints=CA:FALSE\nsubjectKeyIdentifier=hash\nauthorityKeyIdentifier=keyid,issuer\n" "$SAN" > ext.cnf
openssl x509 -req -in node.csr -CA ca.crt -CAkey ca.key -CAcreateserial -days 365 \
  -extfile ext.cnf -out node.crt 2>/dev/null
rm -f node.csr ext.cnf ca.srl
chmod 644 node.key
echo "Certificados en deploy/certs (SAN: $SAN)"
