import os, sys, json, threading, urllib.request, urllib.parse, urllib.error, xml.dom.minidom as md, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "server"))
os.environ["VITA_YT_DB"] = tempfile.mktemp(suffix=".db")
os.environ["YT_OAUTH_CLIENT_ID"] = "cid"; os.environ["YT_OAUTH_CLIENT_SECRET"] = "sec"
os.environ["YT_API_KEY"] = "k"; os.environ["VITA_YT_REQLOG"] = os.devnull
import adapter as A
A.print = lambda *a, **k: None
A.API_KEY = "k"; A.OAUTH_ID = "cid"; A.OAUTH_SECRET = "sec"

def vid(i): return {"id": i, "snippet": {"title": "Video "+i, "channelId": "UCabcdefghijklmnopqrstuv", "channelTitle": "Kanal",
                    "publishedAt": "2026-01-01T00:00:00Z", "categoryId": "10", "description": "d & <x>"},
                    "contentDetails": {"duration": "PT3M5S"}, "statistics": {"viewCount": "5"}}
calls = []
def fake_api(path, **p):
    calls.append(("api", path, p.get("part")))
    if path == "videos":
        if p.get("chart"): return {"items": [vid("PPPPPPPPPP"+str(n)) for n in range(5)]}
        return {"items": [vid(i) for i in p["id"].split(",")]}
    if path == "commentThreads":
        return {"items": [{"id": "c1", "snippet": {"totalReplyCount": 2, "topLevelComment": {"id": "c1", "snippet": {
            "videoId": p["videoId"], "authorDisplayName": "Max", "authorChannelId": {"value": "UCzzzzzzzzzzzzzzzzzzzzzz"},
            "textOriginal": "Toll <3 & so", "publishedAt": "2026-01-02T00:00:00Z"}}}}]}
    if path == "search":
        if p.get("type") == "channel": return {"items": [{"id": {"channelId": "UCabcdefghijklmnopqrstuv"}}]}
        if p.get("type") == "playlist": return {"items": [{"id": {"playlistId": "PLxxxxxxxxxxxx"}}]}
        return {"items": [{"id": {"videoId": "AAAAAAAAAAA"}}]}
    if path == "channels":
        return {"items": [{"id": "UCabcdefghijklmnopqrstuv", "snippet": {"title": "Kanal", "thumbnails": {"default": {"url": "https://yt3.ggpht.com/x"}}}, "statistics": {"videoCount": "3"}}]}
    if path == "playlists":
        return {"items": [{"id": "PLxxxxxxxxxxxx", "snippet": {"title": "Meine Liste", "channelId": "UCabcdefghijklmnopqrstuv", "thumbnails": {}}, "contentDetails": {"itemCount": 2}}]}
    if path == "playlistItems":
        return {"items": [{"id": "ITEM1", "contentDetails": {"videoId": "BBBBBBBBBBB", "videoPublishedAt": "2026-01-03"}}]}
    return {"items": []}
A.api = fake_api
def fake_api_user(tf, path, _method="GET", _body=None, **p):
    tok = tf(); assert tok == "GTOK", tok
    calls.append(("user", _method, path, p.get("id") or p.get("rating") or ""))
    if path == "subscriptions" and _method == "GET":
        return {"items": [{"id": "SUB1", "snippet": {"title": "Abo-Kanal", "resourceId": {"channelId": "UCabcdefghijklmnopqrstuv"}, "thumbnails": {}}, "contentDetails": {"newItemCount": 1}}]}
    if path == "subscriptions" and _method == "POST":
        return {"id": "SUB2", "snippet": {"title": "Neu", "resourceId": _body["snippet"]["resourceId"]}}
    if path == "channels": return {"items": [{"id": "UCmeeeeeeeeeeeeeeeeeeeee", "snippet": {"title": "Aaron"}, "statistics": {}}]}
    if path == "playlists" and _method == "POST": return {"id": "PLnewwwwwwwwww", "snippet": {"title": _body["snippet"]["title"], "channelId": "UCmeeeeeeeeeeeeeeeeeeeee"}}
    if path == "playlists": return fake_api("playlists")
    if path == "playlistItems" and _method == "POST": return {"id": "ITEMNEW"}
    if path == "playlistItems": return fake_api("playlistItems")
    if path == "videos": return {"items": [vid("LLLLLLLLLLL")]}
    if path == "commentThreads": return {"id": "c9", "snippet": {"topLevelComment": {"id": "c9", "snippet": {"videoId": "AAAAAAAAAAA", "textOriginal": _body["snippet"]["topLevelComment"]["snippet"]["textOriginal"], "authorDisplayName": "Aaron"}}}}
    return {}
A.api_user = fake_api_user
gp = []
def fake_google_post(url, f):
    gp.append(url)
    if "device/code" in url: return {"device_code": "DC", "user_code": "ABCD-EFGH", "verification_url": "https://www.google.com/device", "interval": 5, "expires_in": 1800}
    if f.get("grant_type", "").endswith("device_code"):
        return {"error": "authorization_pending"} if len(gp) < 3 else {"access_token": "GTOK", "refresh_token": "GREF", "expires_in": 3600}
    if f.get("grant_type") == "refresh_token": return {"access_token": "GTOK", "expires_in": 3600}
A.google_post = fake_google_post
A.innertube_next_ids = lambda v: ["CCCCCCCCCCC", "DDDDDDDDDDD"]

srv = A.ThreadingHTTPServer(("127.0.0.1", 0), A.Handler)
threading.Thread(target=srv.serve_forever, daemon=True).start()
B = f"http://127.0.0.1:{srv.server_port}"
class NoRedir(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k): return None
op = urllib.request.build_opener(NoRedir)
TOKEN = [None]
def req(m, p, body=None, ctype="application/atom+xml", expect=None, xmlcheck=True):
    h = {"Content-Type": ctype}
    if TOKEN[0]: h["Authorization"] = "Bearer " + TOKEN[0]
    r = urllib.request.Request(B + p, data=body.encode() if body else None, headers=h, method=m)
    try:
        resp = op.open(r); code, data, hd = resp.status, resp.read(), resp.headers
    except urllib.error.HTTPError as e:
        code, data, hd = e.code, e.read(), e.headers
    ok = (expect is None or code == expect)
    if xmlcheck and code in (200, 201) and data[:5] == b"<?xml":
        md.parseString(data)
    print(("OK  " if ok else "FAIL"), m, p[:70], code, len(data))
    if not ok: print("   ", data[:300])
    return code, data, hd

# --- Login-Ablauf
c, d, h = req("GET", "/o/oauth2/auth?redirect_uri=https%3A%2F%2Fvitayt.dl.playstation.net%2Fvitayt%2Fauthorized.html&response_type=code&client_id=569177848438.apps.googleusercontent.com&scope=x&access_type=offline", expect=200)
assert b"ABCD-EFGH" in d
import re; pid = re.search(rb"p=([A-Za-z0-9_-]+)", d).group(1).decode()
A._pending[pid]["next_poll"] = 0
c, d, h = req("GET", f"/o/oauth2/auth/wait?p={pid}", expect=200)      # pending
A._pending[pid]["next_poll"] = 0
c, d, h = req("GET", f"/o/oauth2/auth/wait?p={pid}", expect=302)
loc = h["Location"]; print("   Redirect:", loc[:80]); assert loc.startswith("https://vitayt.dl.playstation.net/vitayt/authorized.html?code=")
code = urllib.parse.parse_qs(urllib.parse.urlsplit(loc).query)["code"][0]
c, d, h = req("POST", "/o/oauth2/token", f"code={code}&client_id=x&client_secret=y&redirect_uri=z&grant_type=authorization_code", "application/x-www-form-urlencoded", 200, False)
tok = json.loads(d); TOKEN[0] = tok["access_token"]; print("   tokens:", list(tok))
c, d, h = req("POST", "/o/oauth2/token", f"code={code}&grant_type=authorization_code", "application/x-www-form-urlencoded", 400, False)   # Code nur einmal
c, d, h = req("POST", "/o/oauth2/token", f"refresh_token={tok['refresh_token']}&grant_type=refresh_token", "application/x-www-form-urlencoded", 200, False)
A._gtok.clear()   # erzwingt Google-Refresh
# --- Konto lesen
for p in ["/feeds/api/users/default?v=2.1", "/feeds/api/users/default/subscriptions?v=2.1", "/feeds/api/users/default/newsubscriptionvideos?v=2.1",
          "/feeds/api/users/default/playlists?v=2.1", "/feeds/api/users/default/favorites?v=2.1", "/feeds/api/users/default/watch_later?v=2.1",
          "/feeds/api/users/default/recommendations?v=2.1", "/feeds/api/users/default/uploads?v=2.1", "/feeds/api/users/default/events?v=2.1"]:
    req("GET", p, expect=200)
# --- Schreiben
req("POST", "/feeds/api/users/default/watch_later?v=2.1", "<entry xmlns='http://www.w3.org/2005/Atom'><id>AAAAAAAAAAA</id></entry>", expect=201)
c, d, h = req("GET", "/feeds/api/users/default/watch_later?v=2.1", expect=200); assert b"AAAAAAAAAAA" in d
req("DELETE", "/feeds/api/users/default/watch_later/AAAAAAAAAAA", expect=200)
c, d, h = req("GET", "/feeds/api/users/default/watch_later?v=2.1", expect=200); assert b"AAAAAAAAAAA" not in d
req("POST", "/feeds/api/users/default/favorites?v=2.1", "<entry><id>tag:youtube.com,2008:video:AAAAAAAAAAA</id></entry>", expect=201)
req("DELETE", "/feeds/api/users/default/favorites/AAAAAAAAAAA", expect=200)
req("POST", "/feeds/api/videos/AAAAAAAAAAA/ratings", "<entry><yt:rating value='dislike'/></entry>", expect=201)
req("POST", "/feeds/api/users/default/subscriptions?v=2.1", "<entry><category term='channel'/><yt:username>abcdefghijklmnopqrstuv</yt:username></entry>", expect=201)
req("DELETE", "/feeds/api/users/default/subscriptions/SUB1", expect=200)
req("POST", "/feeds/api/users/default/playlists?v=2.1", "<entry><title>Neue Liste</title><summary>x</summary><yt:private/></entry>", expect=201)
req("DELETE", "/feeds/api/users/default/playlists/PLnewwwwwwwwww", expect=200)
req("POST", "/feeds/api/playlists/PLxxxxxxxxxxxx?v=2.1", "<entry><id>AAAAAAAAAAA</id></entry>", expect=201)
req("DELETE", "/feeds/api/playlists/PLxxxxxxxxxxxx/ITEM1", expect=200)
req("DELETE", "/feeds/api/playlists/PLxxxxxxxxxxxx/BBBBBBBBBBB", expect=200)
req("POST", "/feeds/api/videos/AAAAAAAAAAA/comments", "<entry><content>Hallo &amp; tschuess</content></entry>", expect=201)
c, d, h = req("POST", "/feeds/api/users/default/favorites/batch?v=2.1",
    "<feed xmlns:batch='http://schemas.google.com/gdata/batch'><batch:operation type='delete'/><entry><id>http://gdata.youtube.com/feeds/api/users/default/favorites/AAAAAAAAAAA</id><batch:id>1</batch:id></entry></feed>", expect=200)
assert b"code='200'" in d
c, d, h = req("POST", "/feeds/api/videos/batch?v=2.1",
    "<feed><batch:operation type='query'/><entry><id>http://gdata.youtube.com/feeds/api/videos/AAAAAAAAAAA</id></entry><entry><id>http://gdata.youtube.com/feeds/api/videos/BBBBBBBBBBB</id></entry></feed>", expect=200)
assert d.count(b"<yt:videoid>") == 4
# --- oeffentlich
for p in ["/feeds/api/videos/AAAAAAAAAAA/related?v=2.1&start-index=1&max-results=20", "/feeds/api/videos/AAAAAAAAAAA/comments?v=2.1&orderby=published",
          "/feeds/api/channels?q=test&v=2.1", "/feeds/api/playlists/snippets?q=test&v=2.1", "/feeds/api/playlists/PLxxxxxxxxxxxx?v=2.1",
          "/feeds/api/users/abcdefghijklmnopqrstuv/playlists?v=2.1", "/feeds/api/users/abcdefghijklmnopqrstuv?v=2.1",
          "/feeds/api/users/abcdefghijklmnopqrstuv/uploads?v=2.1", "/schemas/2007/subscriptiontypes.cat", "/schemas/2007/complaint-reasons.cat",
          "/feeds/api/channelstandardfeeds/most_subscribed?v=2.1", "/feeds/api/suggest/username?v=2.1&hint=ab", "/vitayt/authorized.html?code=x",
          "/feeds/api/videos?q=katzen&v=2.1", "/health"]:
    req("GET", p, expect=200)
# --- ohne Login
TOKEN[0] = None
req("GET", "/feeds/api/users/default/subscriptions?v=2.1", expect=401)
TOKEN[0] = "falsch"
req("GET", "/feeds/api/users/default?v=2.1", expect=401)
print("Aufrufe user-API:", [c for c in calls if c[0] == "user"])
