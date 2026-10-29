#!/bin/sh
# Sicherung von PS-Vita-YouTube + PS-Vita-Twitch auf dem VPS (serverlele)
#   vita-backup.sh            -> /root/backups/vita-<Datum>.tgz (nur root lesbar, die letzten 10 bleiben)
# Enthaelt Secrets (env-Dateien, Konten-DB mit Google-Refresh-Token) -> NIE ins Git, nur sicher kopieren.
set -eu
DEST=/root/backups
STAMP=$(date +%F_%H%M)
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
mkdir -p "$DEST"
chmod 700 "$DEST"

# Konten-DB konsistent kopieren (sqlite-Backup statt cp, laeuft auch waehrend der Dienst laeuft)
if [ -f /var/lib/vita-youtube/accounts.db ]; then
    python3 -c "import sqlite3; s=sqlite3.connect('/var/lib/vita-youtube/accounts.db'); d=sqlite3.connect('$TMP/accounts.db'); s.backup(d); d.close()"
fi

LIST="
/opt/vita-youtube
/etc/vita-youtube.env
/etc/systemd/system/vita-youtube.service
/root/npm/data/nginx/custom/vita-youtube.conf
/root/npm/data/nginx/custom/http.conf
/root/npm/letsencrypt/renewal/ytv-rsa.conf
/root/npm/letsencrypt/renewal/ttv-rsa.conf
/root/vita-yt-plugin
/opt/vita-twitch
/etc/vita-twitch.env
/etc/systemd/system/vita-twitch.service
/var/lib/vita-twitch
"
EXIST=""
for p in $LIST; do [ -e "$p" ] && EXIST="$EXIST $p"; done

OUT="$DEST/vita-$STAMP.tgz"
# shellcheck disable=SC2086
tar czf "$OUT" --exclude='*/dump' --exclude='*/__pycache__' $EXIST -C "$TMP" $( [ -f "$TMP/accounts.db" ] && echo accounts.db )
chmod 600 "$OUT"

# nur die letzten 10 Sicherungen behalten
ls -1t "$DEST"/vita-*.tgz 2>/dev/null | tail -n +11 | xargs -r rm -f

echo "Sicherung: $OUT ($(du -h "$OUT" | cut -f1))"
tar tzf "$OUT" | sed 's#/.*##' | sort | uniq -c
