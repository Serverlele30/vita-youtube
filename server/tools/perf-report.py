#!/usr/bin/env python3
"""Antwortzeiten des Vita-YouTube-Adapters auswerten (aus /var/log/vita-yt-requests.log).

  python3 perf-report.py            # letzte 24 h
  python3 perf-report.py 2          # letzte 2 h
Zeigt je Anfrage-Art: Anzahl, Median, 90 %, Maximum, Fehler. Dazu die Fehler-Pings
des Vita-Plugins aus dem nginx-Log.
"""
import json
import re
import subprocess
import sys
import time

LOG = "/var/log/vita-yt-requests.log"
hours = float(sys.argv[1]) if len(sys.argv) > 1 else 24
since = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - hours * 3600))

RULES = [
    (r"^/vi/", "Vorschaubild /vi/"),
    (r"^/img", "Bild /img"),
    (r"^/feeds/api/videos/batch", "videos/batch"),
    (r"^/feeds/api/videos/[^/]+/related", "Vorgeschlagene"),
    (r"^/feeds/api/videos/[^/]+/comments", "Kommentare"),
    (r"^/feeds/api/videos\?", "Suche"),
    (r"^/feeds/api/standardfeeds", "Startseite/Kategorien"),
    (r"^/feeds/api/users/default/newsubscriptionvideos", "Neue Abo-Videos"),
    (r"^/feeds/api/users/default/subscriptions", "Abos"),
    (r"^/feeds/api/users/default/watch_later", "Später ansehen"),
    (r"^/feeds/api/users/default/favorites", "Favoriten"),
    (r"^/feeds/api/users/default/playlists", "Meine Playlists"),
    (r"^/feeds/api/users/default\b", "Profil"),
    (r"^/feeds/api/playlists/", "Playlist-Inhalt"),
    (r"^/feeds/api/users/[^/]+/uploads", "Kanal-Uploads"),
    (r"^/feeds/api/users/", "Kanal"),
    (r"^/complete/search", "Suchvorschläge"),
    (r"^/o/oauth2", "Login"),
    (r"batch", "Batch (Schreiben)"),
]

stats = {}
for line in open(LOG, encoding="utf-8", errors="replace"):
    try:
        r = json.loads(line)
    except ValueError:
        continue
    if r.get("t", "") < since:
        continue
    p = r.get("path", "")
    name = next((n for rx, n in RULES if re.search(rx, p)), "Sonstiges " + p.split("?")[0][:30])
    s = stats.setdefault(name, {"ms": [], "err": 0, "size": 0})
    s["ms"].append(r.get("ms", 0))
    s["size"] += r.get("size", 0)
    if r.get("code", 200) >= 400:
        s["err"] += 1


def pct(v, q):
    v = sorted(v)
    return v[min(len(v) - 1, int(len(v) * q))] if v else 0


print(f"Adapter seit {since} (letzte {hours:g} h)")
print(f"{'Art':28} {'Anz':>5} {'Median':>7} {'90%':>7} {'Max':>7} {'Fehler':>6}  (ms)")
for name, s in sorted(stats.items(), key=lambda kv: -sum(kv[1]["ms"])):
    ms = s["ms"]
    print(f"{name[:28]:28} {len(ms):5} {pct(ms, .5):7} {pct(ms, .9):7} {max(ms):7} {s['err']:6}")

try:
    out = subprocess.run(["docker", "exec", "npm-npm-1", "sh", "-c",
                          "grep -h 'vytp/ping' /data/logs/vita-youtube_access.log | tail -n 3000"],
                         capture_output=True, text=True, timeout=20).stdout
    pings = {}
    import datetime
    limit = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=hours)
    for line in out.splitlines():
        tm = re.match(r"\[([^\]]+)\]", line)
        m = re.search(r"ping\?e=([^ \"]+)", line)
        if not (tm and m):
            continue
        try:
            when = datetime.datetime.strptime(tm.group(1), "%d/%b/%Y:%H:%M:%S %z")
        except ValueError:
            continue
        if when < limit:
            continue
        k = re.sub(r"_[A-Za-z0-9_-]{11}_\d+$", "", m.group(1))     # Video-ID/Pos weg
        k = re.sub(r"^(gesendet-MB)_\d+$", r"\1", k)
        pings[k] = pings.get(k, 0) + 1
    print(f"\nPlugin-Meldungen (letzte {hours:g} h):")
    for k, n in sorted(pings.items(), key=lambda kv: -kv[1]):
        print(f"  {n:4}x {k}")
except Exception as e:  # noqa: BLE001
    print("nginx-Log nicht lesbar:", e)
