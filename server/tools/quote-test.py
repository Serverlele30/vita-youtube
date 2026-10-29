#!/usr/bin/env python3
"""Wie viele Videos gibt YouTube der VPS-IP (InnerTube ANDROID, wie Adapter 0.4)?

  set -a; . /etc/vita-youtube.env; set +a; python3 quote-test.py

Testet 15 Videos aus "Beliebt DE" und 10 aeltere bekannte Videos, mit Pause
dazwischen, damit YouTube uns nicht wegen Tempo sperrt.
"""
import sys
import time

sys.path.insert(0, "/opt/vita-youtube")
import adapter  # noqa: E402

OLD = ["dQw4w9WgXcQ", "9bZkp7q19f0", "kJQP7kiw5Fk", "JGwWNGJdvx8", "OPf0YbXqDm0",
       "fJ9rUzIMcZQ", "hTWKbfoikeg", "RgKAFK5djSk", "YQHsXMglC9A", "60ItHLz5WEA"]


def run(label, ids):
    ok = 0
    for vid in ids:
        try:
            s = adapter.resolve_stream(vid, force=True)
            ok += 1
            print(f"  OK   {vid}  {s['size'] // 1024 // 1024} MB")
        except Exception as e:  # noqa: BLE001
            print(f"  FEHLER {vid}  {e}")
        time.sleep(4)
    print(f"{label}: {ok}/{len(ids)} gehen\n")


pop = [v["id"] for v in adapter.popular("DE", None)[:15]]
run("Beliebt DE (neu)", pop)
run("Aeltere Videos", OLD)
