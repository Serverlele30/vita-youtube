#!/bin/sh
# Holt die laufenden Dateien vom VPS ins Repo (auf dem VPS im Repo-Ordner ausfuehren).
# Kopiert KEINE Secrets (env, DB) – die bleiben in /etc bzw. /var/lib.
set -eu
cd "$(dirname "$0")"
cp /opt/vita-youtube/adapter.py                      server/adapter.py
cp /etc/systemd/system/vita-youtube.service          server/vita-youtube.service
cp /usr/local/bin/vita-backup.sh                     server/vita-backup.sh 2>/dev/null || true
mkdir -p nginx
cp /root/npm/data/nginx/custom/vita-youtube.conf     nginx/vita-youtube.conf
for f in loader.c net.c diag.c CMakeLists.txt build.sh exports.yml exports_net.yml exports_diag.yml; do
    cp "/root/vita-yt-plugin/$f" plugin/
done
# Sicherheitscheck: keine Schluessel im Repo
if grep -rnE 'AIza[0-9A-Za-z_-]{20}|GOCSPX-|ya29\.|1//0[0-9A-Za-z_-]{20}' --exclude-dir=.git --exclude=sync-from-vps.sh . ; then
    echo "!!! Moegliches Secret gefunden – NICHT committen !!!"; exit 1
fi
echo "OK – Dateien aktualisiert, keine Secrets gefunden. Jetzt: git add -A && git commit"
