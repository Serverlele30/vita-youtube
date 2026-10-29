#!/bin/sh
# Baut vytp.suprx im offiziellen VitaSDK-Docker-Image (nichts wird auf dem Host installiert)
set -e
cd "$(dirname "$0")"
docker run --rm -v "$PWD":/src -w /src vitasdk/vitasdk:latest \
  sh -c 'rm -rf build && mkdir build && cd build && cmake .. >/dev/null && make'
ls -l build/vytp.suprx build/vytp_net.suprx build/vytp_diag.suprx
