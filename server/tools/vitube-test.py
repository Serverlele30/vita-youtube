#!/usr/bin/env python3
"""Testet vom VPS aus dieselben InnerTube-Anfragen, die ViTube auf der Vita macht.

  python3 vitube-test.py [VIDEO_ID]

Kein Konto, keine Cookies. Zeigt, ob YouTube der VPS-IP Stream-URLs gibt.
"""
import json
import sys
import urllib.error
import urllib.request

VID = sys.argv[1] if len(sys.argv) > 1 else "L_gLXzmasUE"

ANDROID_VER = "21.26.364"
ANDROID_UA = f"com.google.android.youtube/{ANDROID_VER} (Linux; U; Android 11) gzip"
VISION_VER = "1.02"
VISION_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 15_7_3) AppleWebKit/605.1.15 "
             "(KHTML, like Gecko) Version/26.0 Safari/605.1.15")

visitor = None


def post(base, ctx, headers, extra):
    global visitor
    body = {"videoId": VID, "contentCheckOk": True, "racyCheckOk": True, "context": ctx}
    body.update(extra)
    h = {"Content-Type": "application/json"}
    h.update(headers)
    if visitor:
        h["X-Goog-Visitor-Id"] = visitor
    req = urllib.request.Request(base + "player?prettyPrint=false",
                                 data=json.dumps(body).encode(), headers=h)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            d = json.load(r)
    except urllib.error.HTTPError as e:
        return {"_http": e.code}
    vd = (d.get("responseContext") or {}).get("visitorData")
    if vd:
        visitor = vd
    return d


def android():
    ctx = {"client": {"clientName": "ANDROID", "clientVersion": ANDROID_VER,
                      "androidSdkVersion": 30, "userAgent": ANDROID_UA,
                      "osName": "Android", "osVersion": "11", "hl": "de", "gl": "DE"}}
    hdr = {"X-Youtube-Client-Name": "3", "X-Youtube-Client-Version": ANDROID_VER,
           "User-Agent": ANDROID_UA}
    return post("https://youtubei.googleapis.com/youtubei/v1/", ctx, hdr, {})


def vision():
    ctx = {"client": {"clientName": "VISIONOS", "clientVersion": VISION_VER,
                      "deviceMake": "Apple", "deviceModel": "RealityDevice17,1",
                      "userAgent": VISION_UA, "osName": "visionOS",
                      "osVersion": "26.5.23O471", "hl": "de", "gl": "DE",
                      "timeZone": "UTC", "utcOffsetMinutes": 0}}
    hdr = {"X-Youtube-Client-Name": "101", "X-Youtube-Client-Version": VISION_VER,
           "User-Agent": VISION_UA, "Origin": "https://www.youtube.com"}
    return post("https://www.youtube.com/youtubei/v1/", ctx, hdr,
                {"playbackContext": {"contentPlaybackContext": {"html5Preference": "HTML5_PREF_WANTS"}}})


def status(d):
    if "_http" in d:
        return f"HTTP {d['_http']}"
    ps = d.get("playabilityStatus") or {}
    return f"{ps.get('status')} {ps.get('reason') or ''}".strip()


def try_get(url, ua, rng="bytes=0-1023"):
    req = urllib.request.Request(url, headers={"User-Agent": ua, "Range": rng})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            n = len(r.read())
            return f"HTTP {r.status}, {n} Bytes, {r.headers.get('Content-Range') or r.headers.get('Content-Type')}"
    except urllib.error.HTTPError as e:
        return f"HTTP {e.code}"
    except Exception as e:  # noqa: BLE001
        return f"Fehler {e}"


print(f"Video: {VID}")
for name, fn, ua in (("ANDROID", android, ANDROID_UA), ("VISIONOS", vision, VISION_UA)):
    for attempt in (1, 2):   # 2. Versuch mit visitorData aus dem 1.
        d = fn()
        print(f"[{name} #{attempt}] {status(d)}  visitor={'ja' if visitor else 'nein'}")
        if (d.get("playabilityStatus") or {}).get("status") == "OK":
            break
    sd = d.get("streamingData") or {}
    if name == "ANDROID":
        fmts = sd.get("formats") or []
        print("  progressive itags:", [f.get("itag") for f in fmts],
              "| mit url:", [f.get("itag") for f in fmts if f.get("url")])
        f18 = next((f for f in fmts if f.get("itag") == 18 and f.get("url")), None)
        if f18:
            print("  itag 18 Abruf:", try_get(f18["url"], ANDROID_UA))
    else:
        m = sd.get("hlsManifestUrl")
        print("  hlsManifestUrl:", "ja" if m else "nein")
        if m:
            req = urllib.request.Request(m, headers={"User-Agent": VISION_UA})
            try:
                with urllib.request.urlopen(req, timeout=15) as r:
                    txt = r.read().decode("utf-8", "replace")
                lines = [l for l in txt.splitlines() if l.startswith("#EXT-X-STREAM-INF")]
                print(f"  Manifest: {len(lines)} Varianten, z. B. {lines[0][:110] if lines else '-'}")
                var = next((l for l in txt.splitlines() if l and not l.startswith("#")), None)
                if var:
                    with urllib.request.urlopen(urllib.request.Request(var, headers={"User-Agent": VISION_UA}), timeout=15) as r:
                        seg = next((l for l in r.read().decode().splitlines() if l and not l.startswith("#")), None)
                    if seg:
                        print("  1. Segment:", try_get(seg, VISION_UA, "bytes=0-4095"))
            except Exception as e:  # noqa: BLE001
                print("  Manifest-Abruf:", e)
