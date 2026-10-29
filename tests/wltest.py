import os, sys, tempfile, json
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "server"))
os.environ["VITA_YT_DB"] = tempfile.mktemp(suffix=".db"); os.environ["VITA_YT_REQLOG"] = os.devnull
import adapter as A
A.google_token = lambda acc: "GTOK"
def vid(i): return {"id": i, "snippet": {"title": "V"+i, "channelId": "UCx", "channelTitle": "K", "publishedAt": "2026-01-01T00:00:00Z"}, "contentDetails": {"duration": "PT1M"}, "statistics": {}}
A.videos_by_ids = lambda ids: [vid(i) for i in ids]
PL = [{"id": "PLandere00000", "snippet": {"title": "Musik"}, "contentDetails": {"itemCount": 0}}]
ITEMS = {}   # plid -> [(itemid, vid)]
n = [0]; calls = []; fail_pos = [False]
def fake(tf, path, _method="GET", _body=None, **p):
    tf(); calls.append((_method, path))
    if path == "playlists" and _method == "GET": return {"items": PL}
    if path == "playlists" and _method == "POST":
        assert _body["status"]["privacyStatus"] == "private"
        pl = {"id": "PLwl%d" % len(PL), "snippet": {"title": _body["snippet"]["title"]}, "contentDetails": {}}
        PL.append(pl); ITEMS[pl["id"]] = []; return pl
    if path == "playlistItems" and _method == "GET":
        return {"items": [{"id": it, "contentDetails": {"videoId": v}} for it, v in ITEMS.get(p["playlistId"], [])]}
    if path == "playlistItems" and _method == "POST":
        sn = _body["snippet"]
        if "position" in sn and fail_pos[0]: raise A.ApiError("API(user) POST playlistItems HTTP 400: manualSortRequired")
        n[0] += 1; it = "IT%d" % n[0]
        lst = ITEMS[sn["playlistId"]]
        lst.insert(sn.get("position", len(lst)), (it, sn["resourceId"]["videoId"])); return {"id": it}
    if path == "playlistItems" and _method == "DELETE":
        for k, lst in ITEMS.items():
            ITEMS[k] = [x for x in lst if x[0] != p["id"]]
        return {}
    raise AssertionError((path, _method))
A.api_user = fake
acc = 1
# alte lokale Liste (aelteste zuerst hinzugefuegt)
for t, v in [(100, "OLD00000001"), (200, "OLD00000002")]:
    A.dbq("INSERT INTO watch_later VALUES(?,?,?)", (acc, v, t), commit=True)
ids = [v["id"] for v in A.my_watch_later(acc)]
print("nach Migration:", ids); assert ids == ["OLD00000002", "OLD00000001"]
assert A.dbq("SELECT COUNT(*) FROM watch_later", one=True)[0] == 0
assert [p["snippet"]["title"] for p in PL].count("PSVita Später ansehen") == 1
A.act_watch_later(acc, "NEW00000001", True); A.act_watch_later(acc, "NEW00000001", True)   # doppelt -> einmal
ids = [v["id"] for v in A.my_watch_later(acc)]; print("nach Add:", ids)
assert ids == ["NEW00000001", "OLD00000002", "OLD00000001"]
e = A.my_watch_later(acc)[0]; assert e["_entry_id"] == "tag:youtube.com,2008:playlist:WL:NEW00000001"
A.act_watch_later(acc, "OLD00000002", False)
ids = [v["id"] for v in A.my_watch_later(acc)]; print("nach Delete:", ids); assert ids == ["NEW00000001", "OLD00000001"]
fail_pos[0] = True; A.act_watch_later(acc, "END00000001", True)
ids = [v["id"] for v in A.my_watch_later(acc)]; print("Fallback ans Ende:", ids); assert ids[-1] == "END00000001"
# Playlist geloescht -> wird neu angelegt / selbst angelegte per Titel gefunden
PL[:] = [p for p in PL if p["snippet"]["title"] != "PSVita Später ansehen"]; A.forget(acc, "mypl")
A.dbq("UPDATE wl_playlist SET created=0", commit=True)   # Schonfrist vorbei
assert A.my_watch_later(acc) == []        # nur lesen legt nichts an
A.act_watch_later(acc, "AGAIN000001", True)
assert [p["snippet"]["title"] for p in PL].count("PSVita Später ansehen") == 1
# in "Meine Playlists" der alten App ausgeblendet
A.my_uid = lambda a: "me"; A.need_account = lambda h: acc
c, t, body, h = A.r_my_playlists(None, "/feeds/api/users/default/playlists", {}, "")
assert b"Musik" in body and "PSVita Sp".encode() not in body; print("Playlists-Feed: WL ausgeblendet")
print("ALLE WL-TESTS OK; API-Aufrufe:", len(calls))
# --- 1.1: YouTube verzoegert (neue Playlist 404 + fehlt in Liste) -> keine Doppelten
import adapter as A2
DELAY = set()
orig = A.api_user
def slow(tf, path, _method="GET", _body=None, **p):
    r = orig(tf, path, _method, _body, **p)
    if path == "playlists" and _method == "POST": DELAY.add(r["id"])
    if path == "playlists" and _method == "GET": return {"items": [x for x in r["items"] if x["id"] not in DELAY]}
    if path == "playlistItems" and _method == "GET" and p["playlistId"] in DELAY: raise A.ApiError("API(user) GET playlistItems HTTP 404: playlistNotFound")
    return r
A.api_user = slow
A.dbq("DELETE FROM wl_playlist", commit=True)
PL[:] = [x for x in PL if x["snippet"]["title"] != "PSVita Später ansehen"]; A.forget(acc, "mypl")
A.dbq("INSERT INTO watch_later VALUES(?,?,?)", (acc, "MIG00000001", 1), commit=True)
for _ in range(3):
    A.forget(acc, "mypl"); A._cache.clear(); r = A.my_watch_later(acc)
n_wl = [x["snippet"]["title"] for x in PL].count("PSVita Später ansehen")
print("verzoegert: Playlists", n_wl, "Ergebnis", [v["id"] for v in r]); assert n_wl == 1
DELAY.clear(); A._cache.clear(); A.forget(acc, "mypl")
ids = [v["id"] for v in A.my_watch_later(acc)]; print("nach Freigabe:", ids); assert "MIG00000001" in ids
assert A.dbq("SELECT COUNT(*) FROM watch_later", one=True)[0] == 0
print("ALLE 1.1-TESTS OK")
# --- 1.2: Loeschen per Batch wie die App (users/<name>/watch_later/batch, favoriteId)
A.api_user = fake; A._cache.clear(); A.forget(acc, "mypl")
A.act_watch_later(acc, "DEL00000001", True)
xml = A.video_entry(next(v for v in A.my_watch_later(acc) if v["id"] == "DEL00000001"))
assert "<yt:favoriteId>DEL00000001</yt:favoriteId>" in xml, xml[:300]
class H:  # minimaler Handler mit Login
    headers = {"Authorization": "Bearer X"}
A.auth_account = lambda h: acc
body = ("<feed xmlns='http://www.w3.org/2005/Atom' xmlns:batch='http://schemas.google.com/gdata/batch'>"
        "<batch:operation type=\"delete\"/><entry><id>tag:youtube.com,2008:playlist::DEL00000001</id>"
        "<link type='application/atom+xml' rel='edit' href=''/></entry></feed>")
path = "/feeds/api/users/meeeeeeeeeeeeeeeeeeeeee/watch_later/batch"
assert A.BATCH_RE.match(path)
c, t, out, hd = A.r_batch(H(), path, {}, body)
assert b"code='200'" in out, out
A._cache.clear()
ids = [v["id"] for v in A.my_watch_later(acc)]; print("nach Batch-Delete:", ids); assert "DEL00000001" not in ids
# normale Playlist: favoriteId = Eintrags-ID, Batch an playlists/<id>/batch
plid = "PLandere00000"; ITEMS[plid] = [("ITEMX1", "PLV00000001")]
e = A.playlist_items(plid, lambda: "GTOK")[0]; assert e["_fav"] == "ITEMX1"
body2 = body.replace("::DEL00000001", ":" + plid + ":ITEMX1")
c, t, out, hd = A.r_batch(H(), f"/feeds/api/playlists/{plid}/batch", {}, body2)
assert b"code='200'" in out and ITEMS[plid] == [], (out, ITEMS[plid])
print("ALLE 1.2-TESTS OK")
# --- 1.3: rel='edit' in allen loeschbaren Listen + Batch-ID aus dem edit-Link
A._cache.clear(); A.forget(acc, "mypl")
A.act_watch_later(acc, "EDT00000001", True)
v = next(v for v in A.my_watch_later(acc) if v["id"] == "EDT00000001")
xml = A.video_entry(v)
assert "rel='edit' type='application/atom+xml' href=\"http://ytv.serverlele.de/feeds/api/users/default/watch_later/EDT00000001\"" in xml, xml
e = A.playlist_items("PLandere00000", lambda: "GTOK")
body3 = ("<feed xmlns='http://www.w3.org/2005/Atom' xmlns:batch='http://schemas.google.com/gdata/batch'>"
         "<batch:operation type=\"delete\"/><entry><id>tag:youtube.com,2008:playlist::</id>"
         "<link type='application/atom+xml' rel='edit' href='http://ytv.serverlele.de/feeds/api/users/default/watch_later/EDT00000001'/></entry></feed>")
c, t, out, hd = A.r_batch(H(), "/feeds/api/users/meeeeeeeeeeeeeeeeeeeeee/watch_later/batch", {}, body3)
assert b"code='200'" in out, out
A._cache.clear()
assert "EDT00000001" not in [v["id"] for v in A.my_watch_later(acc)]
fx = A.video_entry({**A.videos_by_ids(["FAV00000001"])[0], "_entry_id": "tag:youtube.com,2008:favorite:FAV00000001",
                    "_fav": "FAV00000001", "_edit": A.EDIT_BASE + "/feeds/api/users/default/favorites/FAV00000001"})
import xml.dom.minidom as md; md.parseString("<feed xmlns='http://www.w3.org/2005/Atom' xmlns:yt='y' xmlns:gd='g' xmlns:media='m'>" + fx + "</feed>")
print("ALLE 1.3-TESTS OK")
