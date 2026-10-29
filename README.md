# vita-youtube

Bringt die **originale PS-Vita-YouTube-App** (PCSF00150, v2.30) wieder zum Laufen – unverändert aus PKGj, ohne zweites Google-Konto, ohne Cookies.

Funktioniert: Startseite, Kategorien, Suche + Vorschläge, Wiedergabe (360p), vorgeschlagene Videos, Kommentare, Kanäle, **Google-Login**, Abos, neue Abo-Videos, Favoriten, Playlists, Später ansehen.

Schwesterprojekt: [vita-twitch](../vita-twitch) (läuft getrennt auf `ttv.serverlele.de`).

## Wie es funktioniert

```
PS Vita (YouTube-App)
 ├─ tls_fixer/iTLS: gdata.youtube.com, accounts.google.com … ──► ytv.serverlele.de
 └─ taiHEN-Plugin vytp (nur in PCSF00150)
     ├─ patcht URLs im Speicher (Login, gdata-Host, Content-Length)
     └─ Server 127.0.0.1:8090 ──► holt Video-URL selbst bei YouTube (InnerTube ANDROID)
                                  und streamt die MP4 an die App
VPS
 nginx (RSA-Zertifikat, alte CBC-Ciphers) ──► server/adapter.py :8444
 adapter.py: GData API v2.1 (Atom) ⇄ YouTube Data API v3 + OAuth-Gerätecode
```

Die Wiedergabe läuft **auf der Vita**, weil YouTube die VPS-IP als Bot sperrt.

## Inhalt

| Pfad | Zweck |
|---|---|
| `server/adapter.py` | GData-Emulation (Python 3, nur Standardbibliothek) |
| `server/vita-youtube.service` | systemd-Unit |
| `server/vita-youtube.env.example` | Vorlage für `/etc/vita-youtube.env` |
| `server/vita-backup.sh` | Sicherung VPS (YouTube + Twitch) nach `/root/backups` |
| `server/tools/` | Testskripte (InnerTube vom VPS, Kontingent) |
| `nginx/vita-youtube.conf` | nginx-Block für ytv.serverlele.de |
| `plugin/` | taiHEN-Plugin (VitaSDK, Build per Docker) |
| `vita/` | Beispiele für `ur0:tai/config.txt` und `redirects.txt` |
| `tests/fulltest.py` | Offline-Test des Adapters (49 Prüfungen) |
| `sync-from-vps.sh` | holt laufende Dateien vom VPS ins Repo, prüft auf Secrets |

## Einrichtung (Kurzfassung)

**Google Cloud:** Projekt anlegen → YouTube Data API v3 aktivieren → API-Key (auf VPS-IP beschränken) → OAuth-Client Typ *Fernseher und Geräte mit begrenzter Eingabe* → OAuth-Zustimmung im Testmodus mit eigenem Konto als Testnutzer (Login läuft dann nach 7 Tagen ab).

**VPS:**
```sh
install -d /opt/vita-youtube && install -m 644 server/adapter.py /opt/vita-youtube/
install -m 600 server/vita-youtube.env.example /etc/vita-youtube.env   # Werte eintragen
install -m 644 server/vita-youtube.service /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now vita-youtube
curl -s http://127.0.0.1:8444/health
```
nginx: `nginx/vita-youtube.conf` nach `/root/npm/data/nginx/custom/`, aus `http.conf` einbinden, Zertifikat `ytv-rsa` (RSA!) anlegen.

**Plugin bauen:**
```sh
cd plugin && ./build.sh      # -> vytp.suprx, vytp_net.suprx
```

**Vita:** `vytp.suprx` + `vytp_net.suprx` nach `ux0:tai/`, in `ur0:tai/config.txt`:
```
*PCSF00150
ux0:tai/vytp.suprx
```
`redirects.txt` von tls_fixer um die Zeilen aus `vita/redirects.txt.beispiel` ergänzen, taiHEN neu laden.

## Fehlersuche

- Vita: `ux0:data/vytp_log.txt` (bei jedem App-Start neu)
- VPS: `journalctl -u vita-youtube -f | grep -v '/vi/'`
- nginx: `docker exec npm-npm-1 tail /data/logs/vita-youtube_error.log`
- App-Fehlercodes: `0xA0060001`=400, `0xA0060004`=404, `0xA0030001`=401, `0xA0030002`=403

## Sicherheit

Secrets (API-Key, OAuth-Client, Konten-DB mit Refresh-Tokens) liegen nur in `/etc/vita-youtube.env` und `/var/lib/vita-youtube/` – **nie im Repo**. `.gitignore` und `sync-from-vps.sh` schützen davor.
