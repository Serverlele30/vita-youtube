import os, sys, tempfile, time, threading
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "server"))
os.environ["VITA_YT_DB"] = tempfile.mktemp(suffix=".db"); os.environ["VITA_YT_REQLOG"] = os.devnull
os.environ["YT_API_KEY"] = "k"
import adapter as A
A.API_KEY = "k"
# 1) cached: frisch / veraltet (sofort + Hintergrund) / gleichzeitige Abrufe nur einmal
n = [0]
def slow():
    n[0] += 1; time.sleep(0.3); return n[0]
ths = [threading.Thread(target=lambda: A.cached(("t", 1), 1, slow)) for _ in range(5)]
[t.start() for t in ths]; [t.join() for t in ths]
assert n[0] == 1, n
A._cache[("t", 1)] = (time.time() - 5, 1)          # abgelaufen, aber < 12x TTL
t0 = time.time(); v = A.cached(("t", 1), 1, slow); dt = time.time() - t0
assert v == 1 and dt < 0.05, (v, dt)
time.sleep(0.5); assert A._cache[("t", 1)][1] == 2       # im Hintergrund erneuert
print("cached ok")
# forget waehrend Hintergrund-Ladung -> alte Ladung schreibt nicht zurueck
A._cache[("likes", 7)] = (time.time() - 100, "alt")
A.cached(("likes", 7), 60, lambda: (time.sleep(0.3), "neu-aber-vor-forget")[1])
time.sleep(0.05); A.forget(7, "likes"); time.sleep(0.5)
assert ("likes", 7) not in A._cache, A._cache.get(("likes", 7))
print("forget ok")
# 2) videos_by_ids: Cache + ein Abruf je 50 + Reihenfolge
calls = []
def fake_api(path, **p):
    calls.append(p["id"].count(",") + 1)
    return {"items": [{"id": i} for i in p["id"].split(",")]}
A.api = fake_api
ids = ["V%010d" % i for i in range(120)]
r = A.videos_by_ids(ids); assert [v["id"] for v in r] == ids and sorted(calls) == [20, 50, 50], calls
calls.clear(); r = A.videos_by_ids(ids[:30] + ["NEU00000001"]); assert calls == [1], calls
assert A.video_by_id("V0000000005")["id"] == "V0000000005" and calls == [1]
print("videos_by_ids ok")
# 3) videos/batch: ein Abruf fuer alle Eintraege
calls.clear()
body = "<feed>" + "".join(f"<entry><id>http://gdata.youtube.com/feeds/api/videos/B{i:010d}</id></entry>" for i in range(20)) + "</feed>"
c, t, out, h = A.r_batch(None, "/feeds/api/videos/batch", {}, body)
assert calls == [20], calls; assert out.count(b"<yt:videoid>") == 40
print("batch ok")
# 4) Bilder-Cache
fc = []
A.fetch = lambda url, **k: (fc.append(url), (200, b"JPEG" * 100, {"Content-Type": "image/jpeg"}))[1]
for _ in range(3):
    st, ctype, raw, hd = A.r_thumb(None, "/vi/AAAAAAAAAAA/mqdefault.jpg", {}, "")
assert st == 200 and len(fc) == 1, fc
print("thumb-cache ok")
print("ALLE PERF-TESTS OK")
