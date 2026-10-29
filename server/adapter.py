#!/usr/bin/env python3
"""PS-Vita-YouTube-Adapter fuer die offizielle App PCSF00150 (Version 2.30).

Emuliert die 2015 abgeschaltete GData API v2.1 (gdata.youtube.com) plus die
Geraete-Registrierung. Datenquelle: YouTube Data API v3 (API-Key).

Eindeutig getrennt vom Twitch-Adapter:
  Domain ytv.serverlele.de, Dienst vita-youtube.service, Port 127.0.0.1:8444,
  Secrets in /etc/vita-youtube.env.

Stand 0.8:
  - Feeds: Beliebt (+ Kategorien), Suche (+ Vorschlaege), vorgeschlagene Videos,
    Kommentare (lesen/schreiben), Kanalprofil, Uploads, Kanal-/Playlist-Suche,
    Playlists, videos/batch
  - Login per Google-Geraetecode (/o/oauth2/*), Tokens in sqlite
  - Konto: Abos (+ neue Videos), Playlists, Favoriten (= "Mag ich"),
    Spaeter ansehen (lokal), Bewerten, Abonnieren/Kuendigen, Batch-Aktionen
  - Wiedergabe: macht das Vita-Plugin (vytp) - VPS-IP ist bei YouTube gesperrt
  - Unbekannte /feeds/api/*: leerer Feed statt Fehler (wird geloggt)
"""
import base64
import hashlib
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from xml.sax.saxutils import escape, quoteattr

VERSION = "0.9"
API_KEY = os.environ.get("YT_API_KEY", "")
PUBLIC_BASE = os.environ.get("VITA_YT_PUBLIC_BASE", "https://ytv.serverlele.de").rstrip("/")
# Wohin media:content zeigt. Standard: dieser Server. Mit Vita-Plugin: http://127.0.0.1:8090
PLAY_BASE = os.environ.get("VITA_YT_PLAY_BASE", PUBLIC_BASE).rstrip("/")
LISTEN_HOST = os.environ.get("VITA_YT_HOST", "127.0.0.1")
LISTEN_PORT = int(os.environ.get("VITA_YT_PORT", "8444"))
DUMP_DIR = os.environ.get("VITA_YT_DUMP", "")
# Fehlersuche: VITA_YT_EXPERIMENT=popular -> Favoriten/Playlist/Spaeter ansehen liefern 1:1 die Startseite
EXPERIMENT = os.environ.get("VITA_YT_EXPERIMENT", "")   # gesetzt -> Feed-Antworten dort ablegen (Fehlersuche)
REQ_LOG = os.environ.get("VITA_YT_REQLOG", "/var/log/vita-yt-requests.log")
DEFAULT_REGION = os.environ.get("VITA_YT_REGION", "DE")
FEED_CACHE_S = 600      # Standardfeeds 10 min cachen (spart API-Kontingent)
FEED_MAX = 200          # max. Eintraege pro Standardfeed (4 API-Abrufe a 50)
GDATA = "http://gdata.youtube.com"   # so erwartet die App ihre eigenen Links
UP_CHUNK = 8 * 1024 * 1024   # Upstream in 8-MB-Stuecken holen (googlevideo drosselt sonst)

NS = ("xmlns='http://www.w3.org/2005/Atom' "
      "xmlns:media='http://search.yahoo.com/mrss/' "
      "xmlns:openSearch='http://a9.com/-/spec/opensearchrss/1.0/' "
      "xmlns:gd='http://schemas.google.com/g/2005' "
      "xmlns:yt='http://gdata.youtube.com/schemas/2007'")

# (GData-Term, deutsches Label, v3-categoryId)
CATEGORIES = [
    ("Film", "Film & Animation", "1"),
    ("Autos", "Autos & Fahrzeuge", "2"),
    ("Music", "Musik", "10"),
    ("Animals", "Haustiere & Tiere", "15"),
    ("Sports", "Sport", "17"),
    ("Travel", "Reisen & Events", "19"),
    ("Games", "Gaming", "20"),
    ("People", "Leute & Blogs", "22"),
    ("Comedy", "Comedy", "23"),
    ("Entertainment", "Unterhaltung", "24"),
    ("News", "Nachrichten & Politik", "25"),
    ("Howto", "Praktische Tipps & Styling", "26"),
    ("Education", "Bildung", "27"),
    ("Tech", "Wissenschaft & Technik", "28"),
]
CAT_BY_TERM = {t.lower(): (t, l, i) for t, l, i in CATEGORIES}
CAT_BY_ID = {i: (t, l, i) for t, l, i in CATEGORIES}

THUMBS = [  # (Datei, Hoehe, Breite, yt:name)
    ("default", 90, 120, "default"),
    ("mqdefault", 180, 320, "mqdefault"),
    ("hqdefault", 360, 480, "hqdefault"),
    ("sddefault", 480, 640, "sddefault"),
    ("1", 90, 120, "start"),
    ("2", 90, 120, "middle"),
    ("3", 90, 120, "end"),
]

_BAD_XML = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_log_lock = threading.Lock()
_cache = {}
_cache_lock = threading.Lock()


# ---------------------------------------------------------------- Hilfen
def x(s):
    """Text fuer XML-Inhalt."""
    return escape(_BAD_XML.sub("", str(s or "")))


def a(s):
    """Text fuer XML-Attribut (inkl. Anfuehrungszeichen)."""
    return quoteattr(_BAD_XML.sub("", str(s or "")))


def gtime(iso):
    """2026-10-27T15:00:05Z -> 2026-10-27T15:00:05.000Z (GData-Stil)."""
    if not iso:
        return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
    return re.sub(r"(\d{2}:\d{2}:\d{2})(\.\d+)?Z$", r"\1.000Z", iso)


def iso_dur(s):
    m = re.match(r"P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?$", s or "")
    if not m:
        return 0
    d, h, mi, se = (int(g or 0) for g in m.groups())
    return ((d * 24 + h) * 60 + mi) * 60 + se


def cached(key, ttl, fn):
    now = time.time()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
    val = fn()
    with _cache_lock:
        _cache[key] = (now, val)
    return val


class ApiError(Exception):
    pass


def api(path, **params):
    if not API_KEY:
        raise ApiError("YT_API_KEY fehlt in /etc/vita-youtube.env")
    params["key"] = API_KEY
    url = "https://www.googleapis.com/youtube/v3/" + path + "?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        body = e.read()[:300].decode("utf-8", "replace")
        raise ApiError(f"API {path} HTTP {e.code}: {body}") from e
    except (urllib.error.URLError, TimeoutError) as e:
        raise ApiError(f"API {path} nicht erreichbar: {e}") from e


def query_int(q, name, default, lo, hi):
    try:
        return max(lo, min(hi, int(q.get(name, [default])[0])))
    except ValueError:
        return default


# ---------------------------------------------------------------- XML
def video_entry(v, root=False):
    vid = v["id"]
    sn = v.get("snippet", {})
    st = v.get("statistics", {})
    cd = v.get("contentDetails", {})
    title = sn.get("title", "")
    desc = (sn.get("description") or "")[:5000]
    ch = sn.get("channelTitle", "")
    chid = sn.get("channelId", "")
    uid = chid[2:] if chid.startswith("UC") else chid
    pub = gtime(sn.get("publishedAt"))
    dur = iso_dur(cd.get("duration"))
    views = int(st.get("viewCount") or 0)
    likes = int(st.get("likeCount") or 0)
    comments = int(st.get("commentCount") or 0)
    term, label, _ = CAT_BY_ID.get(sn.get("categoryId"), ("People", "Leute & Blogs", "22"))
    play = f"{PLAY_BASE}/videoplayback?v={vid}&itag=18&type=video/mp4"
    thumbs = "".join(
        f"<media:thumbnail url={a(f'{PUBLIC_BASE}/vi/{vid}/{f}.jpg')} height='{h}' width='{w}' yt:name='{n}'/>"
        for f, h, w, n in THUMBS)
    ns = " " + NS if root else ""
    # Listen-Eintraege so, wie die App (eboot) sie liest:
    #   Favoriten:  <id>tag:youtube.com,2008:favorite:FAVID</id> + <yt:favoriteId>FAVID</yt:favoriteId>
    #   Playlists / Spaeter ansehen: <id>tag:youtube.com,2008:playlist:PLID:ITEMID</id> + <yt:position>
    # Ohne diese Felder verwirft die App die Eintraege ("kein Video vorhanden").
    # Kein rel='edit' (die App baut die Loesch-IDs selbst aus <id>/favoriteId).
    entry_id = v.get("_entry_id") or f"tag:youtube.com,2008:video:{vid}"
    edit = f"<yt:favoriteId>{x(v['_fav'])}</yt:favoriteId>" if v.get("_fav") else ""
    pos = f"<yt:position>{int(v['_pos'])}</yt:position>" if v.get("_pos") is not None else ""
    return (
        f"<entry{ns} gd:etag='W/&quot;{vid}&quot;'>"
        f"<id>{x(entry_id)}</id>"
        f"<published>{pub}</published><updated>{pub}</updated>"
        "<category scheme='http://schemas.google.com/g/2005#kind' term='http://gdata.youtube.com/schemas/2007#video'/>"
        f"<category scheme='http://gdata.youtube.com/schemas/2007/categories.cat' term={a(term)} label={a(label)}/>"
        f"<title>{x(title)}</title>"
        f"<content type='application/x-shockwave-flash' src='http://www.youtube.com/v/{vid}?version=3&amp;f=standard&amp;app=youtube_gdata'/>"
        f"<link rel='alternate' type='text/html' href='http://www.youtube.com/watch?v={vid}&amp;feature=youtube_gdata'/>"
        f"<link rel='http://gdata.youtube.com/schemas/2007#video.related' type='application/atom+xml' href='{GDATA}/feeds/api/videos/{vid}/related?v=2'/>"
        f"<link rel='self' type='application/atom+xml' href='{GDATA}/feeds/api/videos/{vid}?v=2'/>"
        f"{edit}"
        f"<author><name>{x(ch)}</name><uri>{GDATA}/feeds/api/users/{x(uid)}</uri><yt:userId>{x(uid)}</yt:userId></author>"
        "<yt:accessControl action='comment' permission='allowed'/>"
        "<yt:accessControl action='embed' permission='allowed'/>"
        "<yt:accessControl action='rate' permission='allowed'/>"
        "<yt:accessControl action='syndicate' permission='allowed'/>"
        f"<gd:comments><gd:feedLink rel='http://gdata.youtube.com/schemas/2007#comments' href='{GDATA}/feeds/api/videos/{vid}/comments?v=2' countHint='{comments}'/></gd:comments>"
        "<media:group>"
        f"<media:category label={a(label)} scheme='http://gdata.youtube.com/schemas/2007/categories.cat'>{x(term)}</media:category>"
        f"<media:content url={a(play)} type='video/mp4' medium='video' isDefault='true' expression='full' duration='{dur}' yt:format='18'/>"
        f"<media:credit role='uploader' scheme='urn:youtube' yt:display={a(ch)}>{x(uid)}</media:credit>"
        f"<media:description type='plain'>{x(desc)}</media:description>"
        "<media:keywords/>"
        "<media:license type='text/html' href='http://www.youtube.com/t/terms'>youtube</media:license>"
        f"<media:player url='http://www.youtube.com/watch?v={vid}&amp;feature=youtube_gdata_player'/>"
        f"{thumbs}"
        f"<media:title type='plain'>{x(title)}</media:title>"
        f"<yt:duration seconds='{dur}'/>"
        f"<yt:uploaded>{pub}</yt:uploaded>"
        f"<yt:uploaderId>{x(chid)}</yt:uploaderId>"
        f"<yt:videoid>{vid}</yt:videoid>"
        "</media:group>"
        f"<gd:rating average='5.0' max='5' min='1' numRaters='{likes}' rel='http://schemas.google.com/g/2005#overall'/>"
        f"<yt:statistics favoriteCount='0' viewCount='{views}'/>"
        f"<yt:rating numDislikes='0' numLikes='{likes}'/>"
        f"{pos}"
        # Direkt unter <entry>: die Listen-Jobs (Favoriten, Playlists, Spaeter ansehen)
        # nehmen die Video-ID von HIER (nicht aus media:group). Leer -> "kein Video vorhanden".
        f"<yt:videoid>{vid}</yt:videoid>"
        "</entry>"
    )


def feed_xml(feed_id, title, path, query, items, total, render=None, kind="video", extra=""):
    start = query_int(query, "start-index", 1, 1, 10000)
    per = query_int(query, "max-results", 25, 1, 50)
    page = items[start - 1:start - 1 + per]
    now = gtime(None)

    def link(rel, s):
        q = {k: v[0] for k, v in query.items()}
        q["start-index"] = str(s)
        q["max-results"] = str(per)
        href = f"{GDATA}{path}?" + urllib.parse.urlencode(q)
        return f"<link rel='{rel}' type='application/atom+xml' href={a(href)}/>"

    links = link("self", start)
    if start > 1:
        links += link("previous", max(1, start - per))
    if start - 1 + per < total:
        links += link("next", start + per)
    return (
        "<?xml version='1.0' encoding='UTF-8'?>"
        f"<feed {NS} gd:etag='W/&quot;{int(time.time())}&quot;'>"
        f"<id>{x(feed_id)}</id><updated>{now}</updated>"
        f"<category scheme='http://schemas.google.com/g/2005#kind' term='http://gdata.youtube.com/schemas/2007#{kind}'/>"
        f"<title>{x(title)}</title>"
        "<logo>http://www.gstatic.com/youtube/img/logo.png</logo>"
        f"<link rel='http://schemas.google.com/g/2005#feed' type='application/atom+xml' href='{GDATA}{x(path)}?v=2'/>"
        f"{links}"
        "<author><name>YouTube</name><uri>http://www.youtube.com/</uri></author>"
        "<generator version='2.1' uri='http://gdata.youtube.com'>YouTube data API</generator>"
        f"<openSearch:totalResults>{total}</openSearch:totalResults>"
        f"<openSearch:startIndex>{start}</openSearch:startIndex>"
        f"<openSearch:itemsPerPage>{per}</openSearch:itemsPerPage>"
        f"{extra}"
        + "".join((render or video_entry)(v) for v in page)
        + "</feed>"
    )


def categories_xml():
    cats = "".join(
        f"<atom:category term={a(t)} label={a(l)} xml:lang='de-DE'>"
        f"<yt:assignable/><yt:browsable regions='DE AT CH US GB FR IT ES'/></atom:category>"
        for t, l, _ in CATEGORIES)
    return ("<?xml version='1.0' encoding='UTF-8'?>"
            "<app:categories xmlns:app='http://www.w3.org/2007/app' "
            "xmlns:atom='http://www.w3.org/2005/Atom' "
            "xmlns:yt='http://gdata.youtube.com/schemas/2007' fixed='yes' "
            "scheme='http://gdata.youtube.com/schemas/2007/categories.cat'>"
            f"{cats}</app:categories>")


# ---------------------------------------------------------------- Daten
def popular(region, cat_id):
    def load():
        items, token = [], None
        while len(items) < FEED_MAX:
            p = dict(part="snippet,contentDetails,statistics", chart="mostPopular",
                     regionCode=region, maxResults=50, hl="de")
            if cat_id:
                p["videoCategoryId"] = cat_id
            if token:
                p["pageToken"] = token
            try:
                d = api("videos", **p)
            except ApiError as e:
                # manche Kategorien haben kein mostPopular-Chart -> leerer Feed
                if "videoChartNotFound" in str(e) or "HTTP 404" in str(e):
                    break
                raise
            items += d.get("items", [])
            token = d.get("nextPageToken")
            if not token:
                break
        return items
    return cached(("pop", region, cat_id), FEED_CACHE_S, load)


def videos_by_ids(ids):
    """Volle Video-Daten fuer bis zu 50 IDs je API-Abruf (1 Einheit je Abruf), Reihenfolge bleibt."""
    out = {}
    for i in range(0, len(ids), 50):
        chunk = ids[i:i + 50]
        d = api("videos", part="snippet,contentDetails,statistics", id=",".join(chunk), hl="de")
        for v in d.get("items", []):
            out[v["id"]] = v
    return [out[i] for i in ids if i in out]


SEARCH_MAX = 100          # max. Treffer je Suche (2 API-Seiten)
SEARCH_CACHE_S = 1800     # 30 min - Suche kostet 100 Einheiten je Seite
_search = {}              # key -> {"items": [...], "token": str|None, "t": time}
_search_lock = threading.Lock()
ORDER_MAP = {"relevance": "relevance", "published": "date", "viewcount": "viewCount",
             "rating": "rating"}


def search(q, order, duration, hd, need):
    """YouTube-Suche ueber die Data API v3, seitenweise nachgeladen und gecacht."""
    key = (q.lower(), order, duration, hd)
    with _search_lock:
        e = _search.get(key)
        if not e or time.time() - e["t"] > SEARCH_CACHE_S:
            e = {"items": [], "token": "", "t": time.time()}
            _search[key] = e
            if len(_search) > 200:      # alten Kram wegwerfen
                for k in sorted(_search, key=lambda k: _search[k]["t"])[:100]:
                    _search.pop(k, None)
        while len(e["items"]) < min(need, SEARCH_MAX) and e["token"] is not None:
            p = dict(part="id", type="video", q=q, maxResults=50, regionCode=DEFAULT_REGION,
                     relevanceLanguage="de", order=ORDER_MAP.get(order, "relevance"),
                     safeSearch="none")
            if duration in ("short", "medium", "long"):
                p["videoDuration"] = duration
            if hd:
                p["videoDefinition"] = "high"
            if e["token"]:
                p["pageToken"] = e["token"]
            d = api("search", **p)
            ids = [i["id"]["videoId"] for i in d.get("items", []) if i.get("id", {}).get("videoId")]
            e["items"] += videos_by_ids(ids)
            e["token"] = d.get("nextPageToken")
            if not ids:
                e["token"] = None
        return e["items"], (len(e["items"]) if e["token"] is None else max(len(e["items"]), SEARCH_MAX))


def video_by_id(vid):
    def load():
        d = api("videos", part="snippet,contentDetails,statistics", id=vid, hl="de")
        items = d.get("items", [])
        return items[0] if items else None
    return cached(("vid", vid), 3600, load)


class StreamError(Exception):
    pass


_streams = {}
_stream_locks = {}


ANDROID_VER = "21.26.364"
ANDROID_UA = f"com.google.android.youtube/{ANDROID_VER} (Linux; U; Android 11) gzip"
_visitor = {"id": None}
_stats = {"ok": 0, "blocked": 0}
_failed = {}          # vid -> (Zeit, Grund): Sperren kurz merken, nicht sofort erneut fragen
FAIL_TTL = 20      # YouTube sperrt nur kurz (Burst), danach geht es wieder
RETRY_DELAYS = (0, 2, 5)   # Sekunden vor 1./2./3. Versuch


def innertube_android(vid):
    """Player-Anfrage wie die Android-App (Methode aus ViTube): direkte MP4-URLs, kein JS."""
    ctx = {"client": {"clientName": "ANDROID", "clientVersion": ANDROID_VER,
                      "androidSdkVersion": 30, "userAgent": ANDROID_UA,
                      "osName": "Android", "osVersion": "11", "hl": "de", "gl": "DE"}}
    body = {"videoId": vid, "contentCheckOk": True, "racyCheckOk": True, "context": ctx}
    last = "?"
    for delay in RETRY_DELAYS:
        if delay:
            time.sleep(delay)
        hdr = {"Content-Type": "application/json", "User-Agent": ANDROID_UA,
               "X-Youtube-Client-Name": "3", "X-Youtube-Client-Version": ANDROID_VER}
        if _visitor["id"]:
            hdr["X-Goog-Visitor-Id"] = _visitor["id"]
        req = urllib.request.Request(
            "https://youtubei.googleapis.com/youtubei/v1/player?prettyPrint=false",
            data=json.dumps(body).encode(), headers=hdr)
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                d = json.load(r)
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}"
            continue
        except (urllib.error.URLError, TimeoutError) as e:
            raise StreamError(f"innertube: {e}") from e
        vd = (d.get("responseContext") or {}).get("visitorData")
        if vd:
            _visitor["id"] = vd
        ps = d.get("playabilityStatus") or {}
        if ps.get("status") == "OK":
            return d
        last = f"{ps.get('status')} {ps.get('reason') or ''}".strip()
        # nur LOGIN_REQUIRED (Bot-Sperre) ist voruebergehend; privat/geloescht nicht wiederholen
        if ps.get("status") != "LOGIN_REQUIRED":
            break
    raise StreamError(f"innertube: {last}")


def resolve_stream(vid, force=False):
    """Direkte googlevideo-URL (itag 18) fuer ein Video, gecacht bis kurz vor Ablauf."""
    with _cache_lock:
        lock = _stream_locks.setdefault(vid, threading.Lock())
    with lock:
        s = _streams.get(vid)
        if s and not force and s["exp"] > time.time() + 300:
            return s
        f = _failed.get(vid)
        if f and not force and time.time() - f[0] < FAIL_TTL:
            raise StreamError(f[1])
        for tries in (1, 2):
            try:
                s = _resolve_once(vid)
                break
            except StreamError as e:
                if tries == 1 and "googlevideo 403" in str(e):
                    continue            # URL frisch holen, manchmal hilft das
                _failed[vid] = (time.time(), str(e))
                _stats["blocked"] += 1
                raise
        _streams[vid] = s
        _failed.pop(vid, None)
        _stats["ok"] += 1
        return s


def _resolve_once(vid):
    """Eine Player-Anfrage + Pruefabruf bei googlevideo."""
    d = innertube_android(vid)
    fmts = (d.get("streamingData") or {}).get("formats") or []
    f18 = next((f for f in fmts if f.get("itag") == 18 and f.get("url")), None)
    if not f18:
        raise StreamError(f"innertube: kein itag 18 (vorhanden: {[f.get('itag') for f in fmts]})")
    url = f18["url"]
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
    exp = int(q.get("expire", [int(time.time()) + 3600])[0])
    size = int(f18.get("contentLength") or 0) or (int(q["clen"][0]) if "clen" in q else None)
    s = {"url": url, "headers": {"User-Agent": ANDROID_UA}, "exp": exp, "size": size,
         "itag": 18, "height": f18.get("height")}
    total = probe_size(s)        # prueft zugleich, ob googlevideo die URL annimmt
    s["size"] = s["size"] or total
    return s


def upstream(s, a, b):
    hdr = dict(s["headers"])
    hdr["Range"] = f"bytes={a}-{b}"
    return urllib.request.urlopen(urllib.request.Request(s["url"], headers=hdr), timeout=30)


def probe_size(s):
    try:
        with upstream(s, 0, 0) as r:
            cr = r.headers.get("Content-Range", "")
    except urllib.error.HTTPError as e:
        raise StreamError(f"googlevideo {e.code}") from e
    except (urllib.error.URLError, TimeoutError) as e:
        raise StreamError(f"googlevideo nicht erreichbar: {e}") from e
    m = re.search(r"/(\d+)$", cr)
    if not m:
        raise StreamError(f"Groesse unbekannt (Content-Range='{cr}')")
    return int(m.group(1))


def channel(uid):
    def load():
        d = api("channels", part="snippet,statistics,contentDetails", id="UC" + uid, hl="de")
        items = d.get("items", [])
        return items[0] if items else None
    return cached(("ch", uid), 3600, load)


def uploads(uid):
    def load():
        d = api("playlistItems", part="contentDetails", playlistId="UU" + uid, maxResults=50)
        ids = [i["contentDetails"]["videoId"] for i in d.get("items", [])]
        if not ids:
            return []
        v = api("videos", part="snippet,contentDetails,statistics", id=",".join(ids), hl="de")
        return v.get("items", [])
    return cached(("up", uid), FEED_CACHE_S, load)


def img_url(u):
    """Fremde Bild-URL ueber ytv ausliefern (alte TLS der Vita)."""
    return f"{PUBLIC_BASE}/img?u=" + urllib.parse.quote(u, safe="")


def user_entry(c, uid):
    sn = c.get("snippet", {})
    st = c.get("statistics", {})
    name = sn.get("title", uid)
    about = (sn.get("description") or "")[:3000]
    pub = gtime(sn.get("publishedAt"))
    thumb = ((sn.get("thumbnails") or {}).get("default") or {}).get("url", "")
    vids = int(st.get("videoCount") or 0)
    subs = int(st.get("subscriberCount") or 0)
    views = int(st.get("viewCount") or 0)
    base = f"{GDATA}/feeds/api/users/{uid}"
    return (
        "<?xml version='1.0' encoding='UTF-8'?>"
        f"<entry {NS} gd:etag='W/&quot;{uid}&quot;'>"
        f"<id>tag:youtube.com,2008:user:{uid}</id>"
        f"<published>{pub}</published><updated>{gtime(None)}</updated>"
        "<category scheme='http://schemas.google.com/g/2005#kind' term='http://gdata.youtube.com/schemas/2007#userProfile'/>"
        f"<title>{x(name)}</title><summary>{x(about)}</summary>"
        f"<content type='text'>{x(about)}</content>"
        f"<link rel='self' type='application/atom+xml' href='{base}?v=2'/>"
        f"<author><name>{x(name)}</name><uri>{base}</uri><yt:userId>{uid}</yt:userId></author>"
        f"<yt:aboutMe>{x(about)}</yt:aboutMe>"
        f"<yt:description>{x(about)}</yt:description>"
        f"<gd:feedLink rel='http://gdata.youtube.com/schemas/2007#user.uploads' href='{base}/uploads?v=2' countHint='{vids}'/>"
        f"<gd:feedLink rel='http://gdata.youtube.com/schemas/2007#user.subscriptions' href='{base}/subscriptions?v=2' countHint='0'/>"
        f"<gd:feedLink rel='http://gdata.youtube.com/schemas/2007#user.favorites' href='{base}/favorites?v=2' countHint='0'/>"
        f"<gd:feedLink rel='http://gdata.youtube.com/schemas/2007#user.watchlater' href='{base}/watch_later?v=2' countHint='0'/>"
        + (f"<media:thumbnail url={a(img_url(thumb))}/>" if thumb else "")
        + f"<yt:statistics lastWebAccess='{gtime(None)}' subscriberCount='{subs}' videoWatchCount='0' viewCount='0' totalUploadViews='{views}'/>"
        f"<yt:username display={a(name)}>{uid}</yt:username>"
        "<yt:incomplete/>"
        "</entry>"
    )


# ---------------------------------------------------------------- Routen
ATOM = "application/atom+xml; charset=UTF-8"


def r_register(h, path, query, body):
    serial = urllib.parse.parse_qs(body).get("serialNumber", [""])[0]
    d = hashlib.sha256(("vita-yt:" + serial).encode()).digest()
    out = f"DeviceId={d[:10].hex()}\nDeviceKey={base64.b64encode(d[10:26]).decode()}\n"
    return 200, "text/plain", out.encode(), {}


def r_categories(h, path, query, body):
    return 200, "application/xml; charset=UTF-8", categories_xml().encode(), {}


STD_RE = re.compile(r"^/feeds/api/standardfeeds/(?:([A-Za-z]{2})/)?([a-z_]+?)(?:_([A-Z][A-Za-z]+))?$")


def r_standardfeed(h, path, query, body):
    m = STD_RE.match(path)
    region = (m.group(1) or DEFAULT_REGION).upper()
    feed = m.group(2)
    cat = CAT_BY_TERM.get((m.group(3) or "").lower())
    # Alle alten Standardfeeds (most_popular, top_rated, most_viewed, ...) -> mostPopular
    items = popular(region, cat[2] if cat else None)
    fid = f"tag:youtube.com,2008:standardfeed:{region.lower()}:{feed}" + (f"_{cat[0]}" if cat else "")
    title = "Beliebt" + (f": {cat[1]}" if cat else "")
    xml = feed_xml(fid, title, path, query, items, len(items))
    return 200, ATOM, xml.encode(), {"GData-Version": "2.1"}


VIDEO_RE = re.compile(r"^/feeds/api/videos/([A-Za-z0-9_-]{11})$")


def r_video(h, path, query, body):
    vid = VIDEO_RE.match(path).group(1)
    v = video_by_id(vid)
    if not v:
        return 404, "text/plain", b"Video not found", {}
    xml = "<?xml version='1.0' encoding='UTF-8'?>" + video_entry(v, root=True)
    return 200, ATOM, xml.encode(), {"GData-Version": "2.1"}


THUMB_RE = re.compile(r"^/vi/([A-Za-z0-9_-]{11})/([a-z0-9]+)\.jpg$")


def r_thumb(h, path, query, body):
    vid, name = THUMB_RE.match(path).groups()
    for n in (name, "hqdefault") if name == "sddefault" else (name,):
        req = urllib.request.Request(f"https://i.ytimg.com/vi/{vid}/{n}.jpg",
                                     headers={"User-Agent": "Mozilla/5.0"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return 200, "image/jpeg", r.read(), {"Cache-Control": "public, max-age=86400"}
        except urllib.error.HTTPError:
            continue
    return 404, "text/plain", b"", {}


VID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")


def r_videoplayback(h, path, query, body):
    """Progressive MP4 mit Range-Unterstuetzung durchreichen. Schreibt selbst."""
    vid = query.get("v", [""])[0]
    if not VID_RE.match(vid):
        return 400, "text/plain", b"bad id", {}
    try:
        s = resolve_stream(vid)
    except StreamError as e:
        h.stream_err = str(e)
        return 503, "text/plain", b"stream unavailable", {}
    total = s["size"]
    start, end, partial = 0, total - 1, False
    m = re.match(r"bytes=(\d*)-(\d*)$", (h.headers.get("Range") or "").strip())
    if m and (m.group(1) or m.group(2)):
        partial = True
        if m.group(1) == "":
            start = max(0, total - int(m.group(2)))
        else:
            start = int(m.group(1))
            if m.group(2):
                end = min(int(m.group(2)), total - 1)
    if start >= total:
        return 416, "text/plain", b"", {"Content-Range": f"bytes */{total}"}
    length = end - start + 1
    code = 206 if partial else 200
    h.send_response(code)
    h.send_header("Content-Type", "video/mp4")
    h.send_header("Content-Length", str(length))
    h.send_header("Accept-Ranges", "bytes")
    if partial:
        h.send_header("Content-Range", f"bytes {start}-{end}/{total}")
    h.end_headers()
    h.streamed = (code, length)
    if h.command == "HEAD":
        return None
    pos, retried = start, False
    try:
        while pos <= end:
            b = min(end, pos + UP_CHUNK - 1)
            try:
                r = upstream(s, pos, b)
            except urllib.error.HTTPError as e:
                if e.code in (403, 410) and not retried:   # URL abgelaufen -> neu holen
                    s, retried = resolve_stream(vid, force=True), True
                    continue
                raise
            got = 0
            with r:
                while True:
                    buf = r.read(65536)
                    if not buf:
                        break
                    h.wfile.write(buf)
                    got += len(buf)
            pos += got
            if got == 0:
                break
    except (BrokenPipeError, ConnectionResetError):
        pass   # Vita hat abgebrochen/gespult - normal
    return None


USER_RE = re.compile(r"^/feeds/api/users/([A-Za-z0-9_-]{6,40})$")
UPLOADS_RE = re.compile(r"^/feeds/api/users/([A-Za-z0-9_-]{6,40})/uploads$")
EMPTY_RE = re.compile(r"^/feeds/api/videos/[A-Za-z0-9_-]{11}/(related|comments|responses)$")


def r_user(h, path, query, body):
    uid = USER_RE.match(path).group(1)
    if uid == "default":
        return 401, "text/plain", b"login required", {}
    c = channel(uid)
    if not c:
        return 404, "text/plain", b"User not found", {}
    return 200, ATOM, user_entry(c, uid).encode(), {"GData-Version": "2.1"}


def r_uploads(h, path, query, body):
    uid = UPLOADS_RE.match(path).group(1)
    if uid == "default":
        return 401, "text/plain", b"login required", {}
    items = uploads(uid)
    xml = feed_xml(f"tag:youtube.com,2008:user:{uid}:uploads", "Uploads", path, query, items, len(items))
    return 200, ATOM, xml.encode(), {"GData-Version": "2.1"}


def r_empty(h, path, query, body):
    kind = EMPTY_RE.match(path).group(1)
    xml = feed_xml(f"tag:youtube.com,2008:{kind}", kind, path, query, [], 0)
    return 200, ATOM, xml.encode(), {"GData-Version": "2.1"}


IMG_HOSTS = (".ggpht.com", ".googleusercontent.com", ".ytimg.com")


def r_img(h, path, query, body):
    u = query.get("u", [""])[0]
    host = urllib.parse.urlsplit(u).hostname or ""
    if not u.startswith("https://") or not host.endswith(IMG_HOSTS):
        return 400, "text/plain", b"", {}
    try:
        req = urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            ctype = r.headers.get("Content-Type", "image/jpeg")
            return 200, ctype, r.read(), {"Cache-Control": "public, max-age=86400"}
    except urllib.error.HTTPError as e:
        return e.code, "text/plain", b"", {}


SEARCH_RE = re.compile(r"^/feeds/api/videos$")


def r_search(h, path, query, body):
    q = (query.get("q") or query.get("vq") or [""])[0].strip()
    if not q:
        return 400, "text/plain", b"q missing", {}
    start = query_int(query, "start-index", 1, 1, 1000)
    per = query_int(query, "max-results", 25, 1, 50)
    order = (query.get("orderby") or ["relevance"])[0].lower()
    duration = (query.get("duration") or [""])[0].lower()
    hd = (query.get("hd") or [""])[0].lower() == "true"
    items, total = search(q, order, duration, hd, start - 1 + per)
    fid = "tag:youtube.com,2008:videos"
    xml = feed_xml(fid, f"Suche: {q}", path, query, items, total)
    return 200, ATOM, xml.encode(), {"GData-Version": "2.1"}


def r_suggest(h, path, query, body):
    """Suchvorschlaege 1:1 von Google durchreichen (Format bleibt so, wie die App es kennt)."""
    qs = urllib.parse.urlencode({k: v[0] for k, v in query.items()})
    req = urllib.request.Request("https://suggestqueries.google.com/complete/search?" + qs,
                                 headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            return 200, r.headers.get("Content-Type", "application/json; charset=UTF-8"), r.read(), {}
    except urllib.error.HTTPError as e:
        return e.code, "text/plain", b"", {}
    except (urllib.error.URLError, TimeoutError):
        return 503, "text/plain", b"", {}


# ================================================================ Oeffentliche Funktionen (0.8)
WEB_VER = "2.20261020.01.00"


def innertube_next_ids(vid):
    """Vorgeschlagene Videos ueber InnerTube 'next' (WEB). Nur Video-IDs per Regex -
    robust gegen Layout-Aenderungen. Gesperrt wird hier (anders als 'player') kaum."""
    body = {"context": {"client": {"clientName": "WEB", "clientVersion": WEB_VER,
                                   "hl": "de", "gl": "DE"}}, "videoId": vid}
    req = urllib.request.Request(
        "https://www.youtube.com/youtubei/v1/next?prettyPrint=false",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0",
                 "X-Youtube-Client-Name": "1", "X-Youtube-Client-Version": WEB_VER,
                 "Origin": "https://www.youtube.com"})
    with urllib.request.urlopen(req, timeout=10) as r:
        txt = r.read().decode("utf-8", "replace")
    ids = []
    for m in re.finditer(r'"videoId":"([A-Za-z0-9_-]{11})"', txt):
        i = m.group(1)
        if i != vid and i not in ids:
            ids.append(i)
    return ids[:40]


def related(vid):
    def load():
        try:
            ids = innertube_next_ids(vid)
        except Exception:   # noqa: BLE001 - Ausweg: weitere Videos vom selben Kanal
            ids = []
        if not ids:
            v = video_by_id(vid)
            chid = ((v or {}).get("snippet") or {}).get("channelId", "")
            if chid.startswith("UC"):
                return [u for u in uploads(chid[2:]) if u["id"] != vid]
            return []
        return videos_by_ids(ids)
    return cached(("rel", vid), 3600, load)


def comment_entry(c):
    top = (c.get("snippet") or {}).get("topLevelComment") or c
    sn = top.get("snippet") or {}
    cid = top.get("id", "")
    vid = sn.get("videoId", "")
    name = sn.get("authorDisplayName", "")
    chid = (sn.get("authorChannelId") or {}).get("value", "")
    uid = chid[2:] if chid.startswith("UC") else chid
    text = sn.get("textOriginal") or sn.get("textDisplay") or ""
    pub = gtime(sn.get("publishedAt"))
    upd = gtime(sn.get("updatedAt") or sn.get("publishedAt"))
    replies = int(((c.get("snippet") or {}).get("totalReplyCount")) or 0)
    return (
        f"<entry gd:etag='W/&quot;{x(cid)}&quot;'>"
        f"<id>tag:youtube.com,2008:video:{x(vid)}:comment:{x(cid)}</id>"
        f"<published>{pub}</published><updated>{upd}</updated>"
        "<category scheme='http://schemas.google.com/g/2005#kind' term='http://gdata.youtube.com/schemas/2007#comment'/>"
        f"<title>{x(text[:60])}</title><content>{x(text)}</content>"
        f"<link rel='self' type='application/atom+xml' href='{GDATA}/feeds/api/videos/{x(vid)}/comments/{x(cid)}?v=2'/>"
        f"<author><name>{x(name)}</name><uri>{GDATA}/feeds/api/users/{x(uid)}</uri><yt:userId>{x(uid)}</yt:userId></author>"
        f"<yt:channelId>{x(chid)}</yt:channelId><yt:replyCount>{replies}</yt:replyCount>"
        f"<yt:videoid>{x(vid)}</yt:videoid>"
        "</entry>")


def comments(vid, order):
    def load():
        out, token = [], None
        for _ in range(2):                       # max. 100 Kommentare
            p = dict(part="snippet", videoId=vid, maxResults=50, textFormat="plainText",
                     order="time" if order == "published" else "relevance")
            if token:
                p["pageToken"] = token
            try:
                d = api("commentThreads", **p)
            except ApiError as e:
                if "commentsDisabled" in str(e) or "HTTP 403" in str(e) or "HTTP 404" in str(e):
                    break
                raise
            out += d.get("items", [])
            token = d.get("nextPageToken")
            if not token:
                break
        return out
    return cached(("cm", vid, order), 300, load)


def channel_entry(c):
    chid = c["id"] if isinstance(c.get("id"), str) else (c.get("id") or {}).get("channelId", "")
    sn = c.get("snippet") or {}
    st = c.get("statistics") or {}
    uid = chid[2:] if chid.startswith("UC") else chid
    name = sn.get("title", "")
    desc = (sn.get("description") or "")[:2000]
    thumb = ((sn.get("thumbnails") or {}).get("default") or {}).get("url", "")
    return (
        f"<entry gd:etag='W/&quot;{x(uid)}&quot;'>"
        f"<id>tag:youtube.com,2008:channel:{x(chid)}</id><updated>{gtime(sn.get('publishedAt'))}</updated>"
        "<category scheme='http://schemas.google.com/g/2005#kind' term='http://gdata.youtube.com/schemas/2007#channel'/>"
        f"<title>{x(name)}</title><summary>{x(desc)}</summary>"
        f"<link rel='alternate' type='text/html' href='http://www.youtube.com/channel/{x(chid)}'/>"
        f"<link rel='self' type='application/atom+xml' href='{GDATA}/feeds/api/users/{x(uid)}?v=2'/>"
        f"<author><name>{x(name)}</name><uri>{GDATA}/feeds/api/users/{x(uid)}</uri><yt:userId>{x(uid)}</yt:userId></author>"
        f"<yt:channelId>{x(chid)}</yt:channelId>"
        f"<gd:feedLink rel='http://gdata.youtube.com/schemas/2007#channel.content' href='{GDATA}/feeds/api/users/{x(uid)}/uploads?v=2' countHint='{int(st.get('videoCount') or 0)}'/>"
        f"<yt:channelStatistics subscriberCount='{int(st.get('subscriberCount') or 0)}' viewCount='{int(st.get('viewCount') or 0)}'/>"
        + (f"<media:thumbnail url={a(img_url(thumb))}/>" if thumb else "")
        + f"<yt:username display={a(name)}>{x(uid)}</yt:username>"
        "</entry>")


def playlist_link_entry(p, owner_uid=None, editable=False):
    plid = p["id"] if isinstance(p.get("id"), str) else (p.get("id") or {}).get("playlistId", "")
    sn = p.get("snippet") or {}
    cd = p.get("contentDetails") or {}
    chid = sn.get("channelId", "")
    uid = owner_uid or (chid[2:] if chid.startswith("UC") else chid)
    name = sn.get("channelTitle", "")
    thumbs = sn.get("thumbnails") or {}
    thumb = (thumbs.get("medium") or thumbs.get("default") or {}).get("url", "")
    edit = (f"<link rel='edit' type='application/atom+xml' href='{GDATA}/feeds/api/users/default/playlists/{x(plid)}'/>"
            if editable else "")
    return (
        f"<entry gd:etag='W/&quot;{x(plid)}&quot;'>"
        f"<id>tag:youtube.com,2008:user:{x(uid)}:playlist:{x(plid)}</id>"
        f"<published>{gtime(sn.get('publishedAt'))}</published><updated>{gtime(sn.get('publishedAt'))}</updated>"
        "<category scheme='http://schemas.google.com/g/2005#kind' term='http://gdata.youtube.com/schemas/2007#playlistLink'/>"
        f"<title>{x(sn.get('title'))}</title><summary>{x((sn.get('description') or '')[:1000])}</summary>"
        f"<content type='application/atom+xml;type=feed' src='{GDATA}/feeds/api/playlists/{x(plid)}?v=2'/>"
        f"<link rel='alternate' type='text/html' href='http://www.youtube.com/playlist?list={x(plid)}'/>"
        f"<link rel='self' type='application/atom+xml' href='{GDATA}/feeds/api/users/{x(uid)}/playlists/{x(plid)}?v=2'/>"
        f"{edit}"
        f"<author><name>{x(name)}</name><uri>{GDATA}/feeds/api/users/{x(uid)}</uri><yt:userId>{x(uid)}</yt:userId></author>"
        f"<yt:countHint>{int(cd.get('itemCount') or 0)}</yt:countHint>"
        "<media:group>"
        + (f"<media:thumbnail url={a(img_url(thumb))} height='180' width='320' yt:name='mqdefault'/>" if thumb else "")
        + "<yt:duration seconds='0'/></media:group>"
        f"<yt:playlistId>{x(plid)}</yt:playlistId>"
        "</entry>")


_pl_item_ids = {}      # (Playlist, Video-ID) -> Playlist-Eintrags-ID (fuer Entfernen)


def playlist_items(plid, token_fn=None):
    """Videos einer Playlist (max. 200). token_fn(): Google-Token fuer private Playlists."""
    def load():
        rows, token = [], None
        for _ in range(4):
            p = dict(part="snippet,contentDetails", playlistId=plid, maxResults=50)
            if token:
                p["pageToken"] = token
            d = api_user(token_fn, "playlistItems", **p) if token_fn else api("playlistItems", **p)
            rows += d.get("items", [])
            token = d.get("nextPageToken")
            if not token:
                break
        ids = [r["contentDetails"]["videoId"] for r in rows if (r.get("contentDetails") or {}).get("videoId")]
        full = {v["id"]: v for v in videos_by_ids(ids)}
        for r in rows:
            vid_ = (r.get("contentDetails") or {}).get("videoId")
            if vid_:
                _pl_item_ids[(plid, vid_)] = r["id"]
        out = []
        for n, r in enumerate(rows, 1):
            v = full.get((r.get("contentDetails") or {}).get("videoId"))
            if not v:
                continue
            v = dict(v)
            v["_entry_id"] = f"tag:youtube.com,2008:playlist:{plid}:{r['id']}"
            v["_edit"] = f"{GDATA}/feeds/api/playlists/{plid}/{r['id']}"
            v["_pos"] = n
            out.append(v)
        return out
    key = ("pli", plid, bool(token_fn))
    if token_fn:                 # private Daten nicht lange cachen
        return cached(key + (id(token_fn),), 30, load)
    return cached(key, 600, load)


def playlist_meta(plid):
    def load():
        d = api("playlists", part="snippet,contentDetails", id=plid)
        it = d.get("items", [])
        return it[0] if it else None
    return cached(("plm", plid), 3600, load)


def channel_playlists(uid):
    def load():
        out, token = [], None
        for _ in range(2):
            p = dict(part="snippet,contentDetails", channelId="UC" + uid, maxResults=50)
            if token:
                p["pageToken"] = token
            d = api("playlists", **p)
            out += d.get("items", [])
            token = d.get("nextPageToken")
            if not token:
                break
        return out
    return cached(("chpl", uid), 1800, load)


def search_kind(q, kind, need):
    """Kanal- oder Playlist-Suche (100 Einheiten je 50 Treffer), 30 min gecacht."""
    def load():
        d = api("search", part="snippet", type=kind, q=q, maxResults=50,
                regionCode=DEFAULT_REGION, relevanceLanguage="de")
        items = d.get("items", [])
        if kind == "channel":
            ids = [i["id"]["channelId"] for i in items if (i.get("id") or {}).get("channelId")]
            if not ids:
                return []
            full = api("channels", part="snippet,statistics", id=",".join(ids), hl="de").get("items", [])
            order = {c: n for n, c in enumerate(ids)}
            return sorted(full, key=lambda c: order.get(c["id"], 99))
        ids = [i["id"]["playlistId"] for i in items if (i.get("id") or {}).get("playlistId")]
        if not ids:
            return []
        full = api("playlists", part="snippet,contentDetails", id=",".join(ids)).get("items", [])
        order = {c: n for n, c in enumerate(ids)}
        return sorted(full, key=lambda c: order.get(c["id"], 99))
    return cached(("sk", kind, q.lower()), SEARCH_CACHE_S, load)


# ---- Routen (oeffentlich)
RELATED_RE = re.compile(r"^/feeds/api/videos/([A-Za-z0-9_-]{11})/related$")
COMMENTS_RE = re.compile(r"^/feeds/api/videos/([A-Za-z0-9_-]{11})/comments$")
CHANNELS_RE = re.compile(r"^/feeds/api/channels$")
PL_SNIPPETS_RE = re.compile(r"^/feeds/api/playlists/snippets$")
PLAYLIST_RE = re.compile(r"^/feeds/api/playlists/([A-Za-z0-9_-]{10,64})$")
USER_PLAYLISTS_RE = re.compile(r"^/feeds/api/users/([A-Za-z0-9_-]{6,40})/playlists$")


def r_related(h, path, query, body):
    vid = RELATED_RE.match(path).group(1)
    items = related(vid)
    xml = feed_xml(f"tag:youtube.com,2008:video:{vid}:related", "Vorgeschlagen", path, query, items, len(items))
    return 200, ATOM, xml.encode(), {"GData-Version": "2.1"}


def r_comments(h, path, query, body):
    vid = COMMENTS_RE.match(path).group(1)
    order = (query.get("orderby") or [""])[0].lower()
    items = comments(vid, order)
    xml = feed_xml(f"tag:youtube.com,2008:video:{vid}:comments", "Kommentare", path, query,
                   items, len(items), render=comment_entry, kind="comment")
    return 200, ATOM, xml.encode(), {"GData-Version": "2.1"}


def r_channels(h, path, query, body):
    q = (query.get("q") or [""])[0].strip()
    items = search_kind(q, "channel", 50) if q else []
    xml = feed_xml("tag:youtube.com,2008:channels", f"Kanaele: {q}", path, query,
                   items, len(items), render=channel_entry, kind="channel")
    return 200, ATOM, xml.encode(), {"GData-Version": "2.1"}


def r_pl_snippets(h, path, query, body):
    q = (query.get("q") or [""])[0].strip()
    items = search_kind(q, "playlist", 50) if q else []
    xml = feed_xml("tag:youtube.com,2008:playlists", f"Playlists: {q}", path, query,
                   items, len(items), render=playlist_link_entry, kind="playlistLink")
    return 200, ATOM, xml.encode(), {"GData-Version": "2.1"}


def r_playlist(h, path, query, body):
    plid = PLAYLIST_RE.match(path).group(1)
    if EXPERIMENT == "popular":
        return experiment_popular(path, query)
    acc = auth_account(h)
    tf = (lambda: google_token(acc)) if acc else None
    items = playlist_items(plid, tf)
    meta = playlist_meta(plid) if not acc else None
    title = ((meta or {}).get("snippet") or {}).get("title") or "Playlist"
    extra = f"<yt:playlistId>{x(plid)}</yt:playlistId>"
    xml = feed_xml(f"tag:youtube.com,2008:playlist:{plid}", title, path, query,
                   items, len(items), kind="playlist", extra=extra)
    return 200, ATOM, xml.encode(), {"GData-Version": "2.1"}


def r_user_playlists(h, path, query, body):
    uid = USER_PLAYLISTS_RE.match(path).group(1)
    if uid == "default":
        return r_my_playlists(h, path, query, body)
    items = channel_playlists(uid)
    xml = feed_xml(f"tag:youtube.com,2008:user:{uid}:playlists", "Playlists", path, query,
                   items, len(items), render=lambda p: playlist_link_entry(p, uid), kind="playlistLink")
    return 200, ATOM, xml.encode(), {"GData-Version": "2.1"}


def r_cat_static(h, path, query, body):
    if path.endswith("subscriptiontypes.cat"):
        terms = [("channel", "Kanal"), ("playlist", "Playlist"), ("query", "Suche")]
        scheme = "http://gdata.youtube.com/schemas/2007/subscriptiontypes.cat"
    else:
        terms = [("PORN", "Sexuelle Inhalte"), ("VIOLENCE", "Gewalt"), ("HATE", "Hass"),
                 ("DANGEROUS", "Gefaehrliche Handlungen"), ("RIGHTS", "Rechteverletzung"),
                 ("SPAM", "Spam")]
        scheme = "http://gdata.youtube.com/schemas/2007/complaint-reasons.cat"
    cats = "".join(f"<atom:category term={a(t)} label={a(l)}/>" for t, l in terms)
    xml = ("<?xml version='1.0' encoding='UTF-8'?>"
           "<app:categories xmlns:app='http://www.w3.org/2007/app' xmlns:atom='http://www.w3.org/2005/Atom' "
           f"fixed='yes' scheme='{scheme}'>{cats}</app:categories>")
    return 200, "application/xml; charset=UTF-8", xml.encode(), {}


def r_empty_any(h, path, query, body):
    """Unbekannter GData-Feed: leere Liste statt 404, damit die App keinen Fehler zeigt."""
    print(f"HINWEIS: unbekannter Feed {path} -> leer", flush=True)
    xml = feed_xml(f"tag:youtube.com,2008:{path}", "", path, query, [], 0)
    return 200, ATOM, xml.encode(), {"GData-Version": "2.1"}


# ================================================================ Login + Konto (0.8)
# Ablauf: Die App oeffnet accounts.google.com/o/oauth2/auth (umgeleitet hierher).
# Wir holen bei Google einen Geraetecode (OAuth "TVs and Limited Input devices"),
# zeigen ihn an, der Nutzer bestaetigt auf google.com/device. Danach leiten wir die
# App mit EIGENEM Code auf vitayt.dl.playstation.net/vitayt/authorized.html?code=...
# Die App tauscht den Code bei /o/oauth2/token gegen EIGENE Tokens. Das echte
# Google-Refresh-Token bleibt nur hier auf dem Server (sqlite).
import secrets
import sqlite3

OAUTH_ID = os.environ.get("YT_OAUTH_CLIENT_ID", "")
OAUTH_SECRET = os.environ.get("YT_OAUTH_CLIENT_SECRET", "")
OAUTH_SCOPE = os.environ.get("YT_OAUTH_SCOPE", "https://www.googleapis.com/auth/youtube")
DB_PATH = os.environ.get("VITA_YT_DB", "/var/lib/vita-youtube/accounts.db")
ACCESS_TTL = 3600
REDIRECT_OK = "https://vitayt.dl.playstation.net/"

_db_lock = threading.Lock()
_db = None
_gtok = {}            # account_id -> (google_access_token, expires)
_pending = {}         # pid -> dict(device_code, user_code, url, interval, expires, next_poll, redirect, state)


def db():
    global _db
    if _db is None:
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        _db = sqlite3.connect(DB_PATH, check_same_thread=False)
        _db.executescript("""
            CREATE TABLE IF NOT EXISTS accounts(id INTEGER PRIMARY KEY, refresh TEXT NOT NULL,
                channel_id TEXT, title TEXT, created INTEGER);
            CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY, account_id INTEGER NOT NULL,
                kind TEXT NOT NULL, expires INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS watch_later(account_id INTEGER, video_id TEXT, added INTEGER,
                PRIMARY KEY(account_id, video_id));
        """)
        try:
            os.chmod(DB_PATH, 0o600)
        except OSError:
            pass
    return _db


def dbq(sql, args=(), one=False, commit=False):
    with _db_lock:
        cur = db().execute(sql, args)
        rows = cur.fetchall()
        if commit:
            db().commit()
    return (rows[0] if rows else None) if one else rows


def new_token(account_id, kind, ttl):
    t = secrets.token_urlsafe(32)
    dbq("INSERT INTO tokens VALUES(?,?,?,?)", (t, account_id, kind, int(time.time()) + ttl), commit=True)
    return t


def google_post(url, fields):
    req = urllib.request.Request(url, data=urllib.parse.urlencode(fields).encode(),
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode("utf-8", "replace"))
        except ValueError:
            return {"error": f"http_{e.code}"}


def google_token(acc_id):
    """Gueltiges Google-Access-Token fuer ein Konto (wird bei Bedarf erneuert)."""
    t = _gtok.get(acc_id)
    if t and t[1] > time.time() + 60:
        return t[0]
    row = dbq("SELECT refresh FROM accounts WHERE id=?", (acc_id,), one=True)
    if not row:
        raise AuthError("Konto unbekannt")
    d = google_post("https://oauth2.googleapis.com/token", {
        "client_id": OAUTH_ID, "client_secret": OAUTH_SECRET,
        "refresh_token": row[0], "grant_type": "refresh_token"})
    if "access_token" not in d:
        raise AuthError(f"Google-Token erneuern fehlgeschlagen: {d.get('error')}")
    _gtok[acc_id] = (d["access_token"], time.time() + int(d.get("expires_in", 3600)))
    return d["access_token"]


class AuthError(Exception):
    pass


def api_user(token_fn, path, _method="GET", _body=None, **params):
    """YouTube Data API v3 im Namen des Nutzers."""
    url = "https://www.googleapis.com/youtube/v3/" + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = json.dumps(_body).encode() if _body is not None else None
    hdr = {"Authorization": "Bearer " + token_fn()}
    if data is not None:
        hdr["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=hdr, method=_method)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            raw = r.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        msg = e.read()[:300].decode("utf-8", "replace")
        raise ApiError(f"API(user) {_method} {path} HTTP {e.code}: {msg}") from e
    except (urllib.error.URLError, TimeoutError) as e:
        raise ApiError(f"API(user) {path} nicht erreichbar: {e}") from e


def auth_account(h):
    """Konto-ID aus 'Authorization: Bearer <unser Token>' (oder None)."""
    if h is None:
        return None
    auth = h.headers.get("Authorization") or ""
    m = re.match(r"(?:Bearer|OAuth|GoogleLogin auth=)\s*(\S+)", auth)
    if not m:
        return None
    row = dbq("SELECT account_id, expires FROM tokens WHERE token=? AND kind='access'", (m.group(1),), one=True)
    if not row or row[1] < time.time():
        return None
    return row[0]


def need_account(h):
    acc = auth_account(h)
    if not acc:
        raise AuthError("nicht angemeldet")
    return acc


# ---------------------------------------------------------------- Login-Seiten
def html_page(title, body, refresh=None):
    meta = f"<meta http-equiv='refresh' content='{refresh[0]};url={x(refresh[1])}'>" if refresh else ""
    page = ("<!DOCTYPE html><html><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=960'>"
            f"<title>{x(title)}</title>{meta}"
            "<style>body{font-family:sans-serif;background:#181818;color:#eee;text-align:center;"
            "margin:0;padding:30px}h1{font-size:30px;color:#fff}.code{font-size:64px;letter-spacing:6px;"
            "font-weight:bold;color:#ff4e45;margin:24px 0}.url{font-size:30px;color:#3ea6ff}"
            "p{font-size:22px;line-height:1.5}a{color:#3ea6ff}</style></head>"
            f"<body>{body}</body></html>")
    return 200, "text/html; charset=UTF-8", page.encode(), {"Cache-Control": "no-store"}


def r_oauth_auth(h, path, query, body):
    redirect = (query.get("redirect_uri") or [REDIRECT_OK + "vitayt/authorized.html"])[0]
    if not redirect.startswith(REDIRECT_OK):
        return 400, "text/plain", b"redirect_uri not allowed", {}
    state = (query.get("state") or [""])[0]
    if not OAUTH_ID or not OAUTH_SECRET:
        return html_page("Login", "<h1>Login nicht eingerichtet</h1>"
                         "<p>Auf dem Server fehlen YT_OAUTH_CLIENT_ID / YT_OAUTH_CLIENT_SECRET.</p>")
    d = google_post("https://oauth2.googleapis.com/device/code",
                    {"client_id": OAUTH_ID, "scope": OAUTH_SCOPE})
    if "device_code" not in d:
        return html_page("Login", f"<h1>Fehler</h1><p>Google: {x(d.get('error'))} {x(d.get('error_description'))}</p>")
    pid = secrets.token_urlsafe(12)
    _pending[pid] = {"device_code": d["device_code"], "user_code": d["user_code"],
                     "url": d.get("verification_url") or d.get("verification_uri") or "https://www.google.com/device",
                     "interval": int(d.get("interval", 5)), "expires": time.time() + int(d.get("expires_in", 1800)),
                     "next_poll": 0, "redirect": redirect, "state": state}
    for k in [k for k, v in _pending.items() if v["expires"] < time.time()]:
        _pending.pop(k, None)
    return wait_page(pid)


def wait_page(pid):
    p = _pending[pid]
    url = p["url"].replace("https://", "").replace("http://", "")
    return html_page("YouTube anmelden", (
        "<h1>Bei YouTube anmelden</h1>"
        "<p>1. Oeffne auf dem Handy oder PC:</p>"
        f"<div class='url'>{x(url)}</div>"
        "<p>2. Gib diesen Code ein:</p>"
        f"<div class='code'>{x(p['user_code'])}</div>"
        "<p>3. Waehle dein Google-Konto und erlaube den Zugriff.<br>"
        "Diese Seite geht danach von selbst weiter.</p>"),
        refresh=(max(5, p["interval"]), f"/o/oauth2/auth/wait?p={pid}"))


def r_oauth_wait(h, path, query, body):
    pid = (query.get("p") or [""])[0]
    p = _pending.get(pid)
    if not p or p["expires"] < time.time():
        return html_page("Login", "<h1>Code abgelaufen</h1><p>Bitte die Anmeldung in der App neu starten.</p>")
    if time.time() < p["next_poll"]:
        return wait_page(pid)
    p["next_poll"] = time.time() + p["interval"]
    d = google_post("https://oauth2.googleapis.com/token", {
        "client_id": OAUTH_ID, "client_secret": OAUTH_SECRET, "device_code": p["device_code"],
        "grant_type": "urn:ietf:params:oauth:grant-type:device_code"})
    err = d.get("error")
    if err in ("authorization_pending", "slow_down"):
        if err == "slow_down":
            p["interval"] += 5
        return wait_page(pid)
    if err or "refresh_token" not in d:
        _pending.pop(pid, None)
        return html_page("Login", f"<h1>Anmeldung fehlgeschlagen</h1><p>{x(err or 'kein refresh_token')}</p>")
    # Konto anlegen bzw. aktualisieren (gleicher Kanal -> gleiches Konto)
    gtok = d["access_token"]
    me = api_user(lambda: gtok, "channels", part="snippet", mine="true").get("items") or [{}]
    chid = me[0].get("id", "")
    title = (me[0].get("snippet") or {}).get("title", "")
    row = dbq("SELECT id FROM accounts WHERE channel_id=? AND channel_id!=''", (chid,), one=True)
    if row:
        acc = row[0]
        dbq("UPDATE accounts SET refresh=?, title=? WHERE id=?", (d["refresh_token"], title, acc), commit=True)
    else:
        with _db_lock:
            cur = db().execute("INSERT INTO accounts(refresh, channel_id, title, created) VALUES(?,?,?,?)",
                               (d["refresh_token"], chid, title, int(time.time())))
            db().commit()
            acc = cur.lastrowid
    _gtok[acc] = (gtok, time.time() + int(d.get("expires_in", 3600)))
    code = new_token(acc, "code", 600)
    _pending.pop(pid, None)
    q = {"code": code}
    if p["state"]:
        q["state"] = p["state"]
    sep = "&" if "?" in p["redirect"] else "?"
    target = p["redirect"] + sep + urllib.parse.urlencode(q)
    print(f"LOGIN ok: Konto {acc} ({title})", flush=True)
    return 302, "text/html; charset=UTF-8", b"", {"Location": target, "Cache-Control": "no-store"}


def r_authorized(h, path, query, body):
    return html_page("Angemeldet", "<h1>Angemeldet</h1><p>Du kannst zur App zurueckkehren.</p>")


def r_oauth_token(h, path, query, body):
    f = {k: v[0] for k, v in urllib.parse.parse_qs(body).items()}
    gt = f.get("grant_type", "")
    now = int(time.time())
    if gt == "authorization_code":
        row = dbq("SELECT account_id, expires FROM tokens WHERE token=? AND kind='code'", (f.get("code", ""),), one=True)
        if not row or row[1] < now:
            return 400, "application/json", b'{"error":"invalid_grant"}', {}
        dbq("DELETE FROM tokens WHERE token=?", (f.get("code", ""),), commit=True)
        acc = row[0]
        refresh = new_token(acc, "refresh", 10 * 365 * 86400)
    elif gt == "refresh_token":
        row = dbq("SELECT account_id FROM tokens WHERE token=? AND kind='refresh'", (f.get("refresh_token", ""),), one=True)
        if not row:
            return 400, "application/json", b'{"error":"invalid_grant"}', {}
        acc = row[0]
        refresh = None
    else:
        return 400, "application/json", b'{"error":"unsupported_grant_type"}', {}
    dbq("DELETE FROM tokens WHERE kind='access' AND expires<?", (now,), commit=True)
    out = {"access_token": new_token(acc, "access", ACCESS_TTL), "token_type": "Bearer",
           "expires_in": ACCESS_TTL}
    if refresh:
        out["refresh_token"] = refresh
    return 200, "application/json; charset=utf-8", json.dumps(out).encode(), {"Cache-Control": "no-store"}


def r_oauth_revoke(h, path, query, body):
    tok = (query.get("token") or [""])[0] or urllib.parse.parse_qs(body).get("token", [""])[0]
    if tok:
        dbq("DELETE FROM tokens WHERE token=?", (tok,), commit=True)
    return 200, "application/json", b"{}", {}


# ---------------------------------------------------------------- users/default
def me_channel(acc):
    def load():
        d = api_user(lambda: google_token(acc), "channels", part="snippet,statistics,contentDetails", mine="true")
        it = d.get("items", [])
        return it[0] if it else None
    return cached(("me", acc), 600, load)


def my_uid(acc):
    c = me_channel(acc)
    return (c or {}).get("id", "UCdefault")[2:]


def subscription_entry(s):
    sn = s.get("snippet") or {}
    cd = s.get("contentDetails") or {}
    sid = s.get("id", "")
    chid = (sn.get("resourceId") or {}).get("channelId", "")
    uid = chid[2:] if chid.startswith("UC") else chid
    name = sn.get("title", "")
    thumb = ((sn.get("thumbnails") or {}).get("default") or {}).get("url", "")
    pub = gtime(sn.get("publishedAt"))
    return (
        f"<entry gd:etag='W/&quot;{x(sid)}&quot;'>"
        f"<id>tag:youtube.com,2008:user:default:subscription:{x(sid)}</id>"
        f"<published>{pub}</published><updated>{pub}</updated>"
        "<category scheme='http://schemas.google.com/g/2005#kind' term='http://gdata.youtube.com/schemas/2007#subscription'/>"
        "<category scheme='http://gdata.youtube.com/schemas/2007/subscriptiontypes.cat' term='channel'/>"
        f"<title>Aktivitaeten von: {x(name)}</title>"
        f"<content type='application/atom+xml;type=feed' src='{GDATA}/feeds/api/users/{x(uid)}/uploads?v=2'/>"
        f"<link rel='alternate' type='text/html' href='http://www.youtube.com/channel/{x(chid)}'/>"
        f"<link rel='self' type='application/atom+xml' href='{GDATA}/feeds/api/users/default/subscriptions/{x(sid)}?v=2'/>"
        f"<link rel='edit' type='application/atom+xml' href='{GDATA}/feeds/api/users/default/subscriptions/{x(sid)}'/>"
        f"<author><name>{x(name)}</name><uri>{GDATA}/feeds/api/users/{x(uid)}</uri><yt:userId>{x(uid)}</yt:userId></author>"
        f"<yt:channelId>{x(chid)}</yt:channelId>"
        f"<yt:countHint>{int(cd.get('newItemCount') or 0)}</yt:countHint>"
        + (f"<media:thumbnail url={a(img_url(thumb))}/>" if thumb else "")
        + f"<yt:username display={a(name)}>{x(uid)}</yt:username>"
        f"<yt:youTubeId id={a(sid)}>{x(sid)}</yt:youTubeId>"
        "</entry>")


def my_subscriptions(acc):
    def load():
        out, token = [], None
        for _ in range(10):                      # bis 500 Abos
            p = dict(part="snippet,contentDetails", mine="true", maxResults=50, order="alphabetical")
            if token:
                p["pageToken"] = token
            d = api_user(lambda: google_token(acc), "subscriptions", **p)
            out += d.get("items", [])
            token = d.get("nextPageToken")
            if not token:
                break
        return out
    return cached(("subs", acc), 120, load)


def new_sub_videos(acc):
    """Neueste Uploads der Abos (max. 50 Kanaele x 5 Videos), 15 min gecacht."""
    def load():
        subs = my_subscriptions(acc)[:50]
        rows = []
        for s in subs:
            chid = ((s.get("snippet") or {}).get("resourceId") or {}).get("channelId", "")
            if not chid.startswith("UC"):
                continue
            try:
                d = api("playlistItems", part="contentDetails", playlistId="UU" + chid[2:], maxResults=5)
            except ApiError:
                continue
            for it in d.get("items", []):
                cd = it.get("contentDetails") or {}
                if cd.get("videoId"):
                    rows.append((cd.get("videoPublishedAt") or "", cd["videoId"]))
        rows.sort(reverse=True)
        return videos_by_ids([v for _, v in rows[:100]])
    return cached(("nsv", acc), 900, load)


def my_playlists(acc):
    def load():
        out, token = [], None
        for _ in range(4):
            p = dict(part="snippet,contentDetails", mine="true", maxResults=50)
            if token:
                p["pageToken"] = token
            d = api_user(lambda: google_token(acc), "playlists", **p)
            out += d.get("items", [])
            token = d.get("nextPageToken")
            if not token:
                break
        return out
    return cached(("mypl", acc), 60, load)


def my_likes(acc):
    def load():
        out, token = [], None
        for _ in range(4):
            p = dict(part="snippet,contentDetails,statistics", myRating="like", maxResults=50, hl="de")
            if token:
                p["pageToken"] = token
            d = api_user(lambda: google_token(acc), "videos", **p)
            out += d.get("items", [])
            token = d.get("nextPageToken")
            if not token:
                break
        res = []
        for v in out:
            v = dict(v)
            v["_entry_id"] = f"tag:youtube.com,2008:favorite:{v['id']}"
            v["_fav"] = v["id"]
            res.append(v)
        return res
    return cached(("likes", acc), 60, load)


def my_watch_later(acc):
    ids = [r[0] for r in dbq("SELECT video_id FROM watch_later WHERE account_id=? ORDER BY added DESC", (acc,))]
    res = []
    for n, v in enumerate(videos_by_ids(ids), 1):
        v = dict(v)
        v["_entry_id"] = f"tag:youtube.com,2008:playlist:WL:{v['id']}"
        v["_pos"] = n
        res.append(v)
    return res


def forget(acc, *keys):
    with _cache_lock:
        for k in keys:
            _cache.pop((k, acc), None)
        for k in [k for k in _cache if k and k[0] == "pli" and len(k) > 2 and k[2]]:
            _cache.pop(k, None)


def feed_resp(fid, title, path, query, items, render=None, kind="video", extra=""):
    xml = feed_xml(fid, title, path, query, items, len(items), render=render, kind=kind, extra=extra)
    return 200, ATOM, xml.encode(), {"GData-Version": "2.1"}


DEF_RE = re.compile(r"^/feeds/api/users/default(?:/([a-z_]+))?(?:/([A-Za-z0-9_.-]+))?$")


def r_my_playlists(h, path, query, body):
    acc = need_account(h)
    uid = my_uid(acc)
    return feed_resp(f"tag:youtube.com,2008:user:{uid}:playlists", "Meine Playlists", path, query,
                     my_playlists(acc), render=lambda p: playlist_link_entry(p, uid, editable=True),
                     kind="playlistLink")


def experiment_popular(path, query):
    items = popular(DEFAULT_REGION, None)
    print(f"EXPERIMENT: {path} -> Startseite", flush=True)
    return feed_resp("tag:youtube.com,2008:standardfeed:de:most_popular", "Beliebt", path, query, items)


def r_default_get(h, path, query, body):
    m = DEF_RE.match(path)
    feed, item = m.group(1), m.group(2)
    acc = need_account(h)
    if EXPERIMENT == "popular" and feed in ("favorites", "watch_later"):
        return experiment_popular(path, query)
    if feed is None:
        c = me_channel(acc)
        if not c:
            return 404, "text/plain", b"no channel", {}
        return 200, ATOM, user_entry(c, c["id"][2:]).encode(), {"GData-Version": "2.1"}
    uid = my_uid(acc)
    if feed == "subscriptions":
        return feed_resp(f"tag:youtube.com,2008:user:{uid}:subscriptions", "Abos", path, query,
                         my_subscriptions(acc), render=subscription_entry, kind="subscription")
    if feed == "newsubscriptionvideos":
        return feed_resp(f"tag:youtube.com,2008:user:{uid}:newsubscriptionvideos", "Neue Videos", path,
                         query, new_sub_videos(acc))
    if feed == "playlists":
        return r_my_playlists(h, path, query, body)
    if feed == "favorites":
        return feed_resp(f"tag:youtube.com,2008:user:{uid}:favorites", "Favoriten", path, query, my_likes(acc))
    if feed == "watch_later":
        return feed_resp(f"tag:youtube.com,2008:user:{uid}:watch_later", "Spaeter ansehen", path, query,
                         my_watch_later(acc))
    if feed == "uploads":
        return feed_resp(f"tag:youtube.com,2008:user:{uid}:uploads", "Uploads", path, query, uploads(uid))
    if feed == "recommendations":
        return feed_resp(f"tag:youtube.com,2008:user:{uid}:recommendations", "Empfohlen", path, query,
                         popular(DEFAULT_REGION, None))
    return r_empty_any(h, path, query, body)


# ---------------------------------------------------------------- Schreibaktionen
VID_IN_BODY = re.compile(r"(?:<id>(?:[^<]*?[:/])?|<yt:videoid>|videos/)([A-Za-z0-9_-]{11})(?=</id>|</yt:videoid>|[?\"'<])")


def body_video_id(body):
    m = VID_IN_BODY.search(body or "")
    return m.group(1) if m else None


def body_channel_id(body):
    m = re.search(r"<yt:channelId>(UC[A-Za-z0-9_-]{22})</yt:channelId>", body or "")
    if m:
        return m.group(1)
    m = re.search(r"<yt:username>([^<]+)</yt:username>", body or "")
    if not m:
        return None
    u = m.group(1).strip()
    if re.fullmatch(r"UC[A-Za-z0-9_-]{22}", u):
        return u
    if re.fullmatch(r"[A-Za-z0-9_-]{22}", u):
        return "UC" + u
    d = api("channels", part="id", forUsername=u)          # alter Nutzername
    it = d.get("items", [])
    return it[0]["id"] if it else None


def created(xml_entry):
    xml = "<?xml version='1.0' encoding='UTF-8'?>" + xml_entry.replace("<entry ", f"<entry {NS} ", 1)
    return 201, ATOM, xml.encode(), {"GData-Version": "2.1"}


def act_rate(acc, vid, rating):
    api_user(lambda: google_token(acc), "videos/rate", "POST", None, id=vid, rating=rating)
    forget(acc, "likes")


def act_subscribe(acc, chid):
    s = api_user(lambda: google_token(acc), "subscriptions", "POST",
                 {"snippet": {"resourceId": {"kind": "youtube#channel", "channelId": chid}}}, part="snippet")
    forget(acc, "subs", "nsv")
    return s


def act_unsubscribe(acc, sid):
    api_user(lambda: google_token(acc), "subscriptions", "DELETE", None, id=sid)
    forget(acc, "subs", "nsv")


def act_watch_later(acc, vid, add):
    if add:
        dbq("INSERT OR REPLACE INTO watch_later VALUES(?,?,?)", (acc, vid, int(time.time())), commit=True)
    else:
        dbq("DELETE FROM watch_later WHERE account_id=? AND video_id=?", (acc, vid), commit=True)


def act_playlist_add(acc, plid, vid):
    it = api_user(lambda: google_token(acc), "playlistItems", "POST",
                  {"snippet": {"playlistId": plid, "resourceId": {"kind": "youtube#video", "videoId": vid}}},
                  part="snippet")
    forget(acc, "mypl")
    return it


def act_playlist_remove(acc, item_id):
    api_user(lambda: google_token(acc), "playlistItems", "DELETE", None, id=item_id)
    forget(acc, "mypl")


RATING_RE = re.compile(r"^/feeds/api/videos/([A-Za-z0-9_-]{11})/ratings$")


def r_rate(h, path, query, body):
    acc = need_account(h)
    vid = RATING_RE.match(path).group(1)
    m = re.search(r"<yt:rating[^>]*value=['\"](like|dislike)['\"]", body) or \
        re.search(r"<gd:rating[^>]*value=['\"](\d)['\"]", body)
    val = m.group(1) if m else "like"
    if val.isdigit():
        val = "like" if int(val) >= 3 else "dislike"
    act_rate(acc, vid, val)
    return 201, ATOM, b"<?xml version='1.0' encoding='UTF-8'?><entry xmlns='http://www.w3.org/2005/Atom'/>", {}


def r_comment_post(h, path, query, body):
    acc = need_account(h)
    vid = COMMENTS_RE.match(path).group(1)
    m = re.search(r"<content[^>]*>(.*?)</content>", body, re.S)
    text = re.sub(r"&lt;", "<", re.sub(r"&gt;", ">", re.sub(r"&amp;", "&", (m.group(1) if m else "").strip())))
    if not text:
        return 400, "text/plain", b"empty", {}
    c = api_user(lambda: google_token(acc), "commentThreads", "POST",
                 {"snippet": {"videoId": vid, "topLevelComment": {"snippet": {"textOriginal": text}}}},
                 part="snippet")
    with _cache_lock:
        for k in [k for k in _cache if k and k[0] == "cm" and k[1] == vid]:
            _cache.pop(k, None)
    return created(comment_entry(c))


def r_default_post(h, path, query, body):
    m = DEF_RE.match(path)
    feed, item = m.group(1), m.group(2)
    acc = need_account(h)
    if feed == "favorites":
        vid = body_video_id(body)
        if not vid:
            return 400, "text/plain", b"no video id", {}
        act_rate(acc, vid, "like")
        v = dict(video_by_id(vid) or {"id": vid})
        v["_entry_id"] = f"tag:youtube.com,2008:favorite:{vid}"
        v["_fav"] = vid
        return created(video_entry(v))
    if feed == "watch_later":
        vid = body_video_id(body)
        if not vid:
            return 400, "text/plain", b"no video id", {}
        act_watch_later(acc, vid, True)
        v = dict(video_by_id(vid) or {"id": vid})
        v["_entry_id"] = f"tag:youtube.com,2008:playlist:WL:{vid}"
        v["_pos"] = 1
        return created(video_entry(v))
    if feed == "subscriptions":
        chid = body_channel_id(body)
        if not chid:
            return 400, "text/plain", b"no channel", {}
        return created(subscription_entry(act_subscribe(acc, chid)))
    if feed == "playlists":
        t = re.search(r"<title[^>]*>(.*?)</title>", body, re.S)
        sm = re.search(r"<summary[^>]*>(.*?)</summary>", body, re.S)
        priv = "private" if "yt:private" in body else "public"
        p = api_user(lambda: google_token(acc), "playlists", "POST",
                     {"snippet": {"title": (t.group(1).strip() if t else "Playlist"),
                                  "description": (sm.group(1).strip() if sm else "")},
                      "status": {"privacyStatus": priv}}, part="snippet,status")
        forget(acc, "mypl")
        return created(playlist_link_entry(p, my_uid(acc), editable=True))
    return 404, "text/plain", b"", {}


def r_default_delete(h, path, query, body):
    m = DEF_RE.match(path)
    feed, item = m.group(1), m.group(2)
    acc = need_account(h)
    if not item:
        return 400, "text/plain", b"", {}
    if feed == "favorites":
        act_rate(acc, item, "none")
    elif feed == "watch_later":
        act_watch_later(acc, item, False)
    elif feed == "subscriptions":
        act_unsubscribe(acc, item)
    elif feed == "playlists":
        api_user(lambda: google_token(acc), "playlists", "DELETE", None, id=item)
        forget(acc, "mypl")
    else:
        return 404, "text/plain", b"", {}
    return 200, "text/plain", b"", {}


PL_ITEM_RE = re.compile(r"^/feeds/api/playlists/([A-Za-z0-9_-]{10,64})(?:/([A-Za-z0-9_=-]+))?$")


def r_playlist_post(h, path, query, body):
    acc = need_account(h)
    plid = PL_ITEM_RE.match(path).group(1)
    vid = body_video_id(body)
    if not vid:
        return 400, "text/plain", b"no video id", {}
    it = act_playlist_add(acc, plid, vid)
    v = dict(video_by_id(vid) or {"id": vid})
    v["_entry_id"] = f"tag:youtube.com,2008:playlist:{plid}:{it.get('id', '')}"
    v["_pos"] = 1
    return created(video_entry(v))


def playlist_item_id(acc, plid, ident):
    """App schickt Video-ID oder Eintrags-ID -> immer Eintrags-ID liefern."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", ident):
        return ident
    if (plid, ident) not in _pl_item_ids:
        playlist_items(plid, lambda: google_token(acc))       # Zuordnung neu laden
    return _pl_item_ids.get((plid, ident), ident)


def r_playlist_delete(h, path, query, body):
    acc = need_account(h)
    m = PL_ITEM_RE.match(path)
    if not m.group(2):
        return 400, "text/plain", b"", {}
    act_playlist_remove(acc, playlist_item_id(acc, m.group(1), m.group(2)))
    return 200, "text/plain", b"", {}


# ---------------------------------------------------------------- Batch
BATCH_RE = re.compile(r"^/feeds/api/(videos|users/default/(favorites|watch_later|subscriptions|playlists)|"
                      r"users/[A-Za-z0-9_-]+/favorites|playlists/([A-Za-z0-9_-]{10,64}))/batch$")


def r_batch(h, path, query, body):
    m = BATCH_RE.match(path)
    target = m.group(2) or ("videos" if m.group(1) == "videos" else ("playlist" if m.group(3) else "favorites"))
    plid = m.group(3)
    default_op = (re.search(r"<feed[^>]*>.*?<batch:operation\s+type=['\"](\w+)['\"]", body, re.S) or [None, "insert"])[1]
    acc = auth_account(h)
    out = []
    for ent in re.findall(r"<entry\b.*?</entry>|<entry\b[^>]*/>", body, re.S):
        op_m = re.search(r"<batch:operation\s+type=['\"](\w+)['\"]", ent)
        op = op_m.group(1) if op_m else default_op
        bid_m = re.search(r"<batch:id>(.*?)</batch:id>", ent, re.S)
        bid = bid_m.group(1) if bid_m else ""
        id_m = re.search(r"<id>(.*?)</id>", ent, re.S)
        ident = (id_m.group(1).strip() if id_m else "")
        last = ident.rstrip("/").split("/")[-1].split("?")[0].split(":")[-1]
        code, reason, payload = 200, "Success", ""
        try:
            if target == "videos":           # query: Videos einzeln nachladen
                v = video_by_id(last)
                if v:
                    payload = video_entry(v)[len("<entry"):]
                else:
                    code, reason = 404, "Not Found"
            elif not acc:
                code, reason = 401, "Unauthorized"
            elif target == "favorites":
                vid = body_video_id(ent) if op == "insert" else last
                act_rate(acc, vid, "like" if op == "insert" else "none")
                code = 201 if op == "insert" else 200
            elif target == "watch_later":
                vid = body_video_id(ent) if op == "insert" else last
                act_watch_later(acc, vid, op == "insert")
                code = 201 if op == "insert" else 200
            elif target == "subscriptions":
                if op == "insert":
                    act_subscribe(acc, body_channel_id(ent))
                    code = 201
                else:
                    act_unsubscribe(acc, last)
            elif target == "playlists":
                if op == "delete":
                    api_user(lambda: google_token(acc), "playlists", "DELETE", None, id=last)
                    forget(acc, "mypl")
            elif target == "playlist":
                if op == "insert":
                    act_playlist_add(acc, plid, body_video_id(ent))
                    code = 201
                else:
                    act_playlist_remove(acc, playlist_item_id(acc, plid, last))
        except (ApiError, AuthError, TypeError) as e:
            code, reason = 500, x(str(e))[:120]
        if payload:
            out.append("<entry" + payload.replace("</entry>",
                       f"<batch:id>{x(bid)}</batch:id><batch:operation type='{x(op)}'/>"
                       f"<batch:status code='{code}' reason='{reason}'/></entry>"))
        else:
            out.append(f"<entry><id>{x(ident)}</id><batch:id>{x(bid)}</batch:id>"
                       f"<batch:operation type='{x(op)}'/><batch:status code='{code}' reason='{reason}'/></entry>")
    xml = ("<?xml version='1.0' encoding='UTF-8'?>"
           f"<feed {NS} xmlns:batch='http://schemas.google.com/gdata/batch'>"
           f"<id>{GDATA}{x(path)}</id><updated>{gtime(None)}</updated><title>Batch</title>"
           + "".join(out) + "</feed>")
    return 200, ATOM, xml.encode(), {"GData-Version": "2.1"}


def r_health(h, path, query, body):
    try:
        n_acc = dbq("SELECT COUNT(*) FROM accounts", one=True)[0]
    except Exception:   # noqa: BLE001
        n_acc = None
    out = {"ok": True, "service": "vita-youtube-adapter", "version": VERSION,
           "api_key": bool(API_KEY), "oauth": bool(OAUTH_ID and OAUTH_SECRET), "accounts": n_acc,
           "play_base": PLAY_BASE, "stream_stats": _stats,
           "streams_cached": len(_streams), "blocked_recent": len(_failed)}
    return 200, "application/json", json.dumps(out).encode(), {}


ROUTES = [  # (Methode, Regex, Funktion) - Reihenfolge zaehlt, erster Treffer gewinnt
    # Geraet + Login
    ("POST", re.compile(r"^/youtube/accounts/registerDevice$"), r_register),
    ("GET", re.compile(r"^/o/oauth2/auth$"), r_oauth_auth),
    ("GET", re.compile(r"^/o/oauth2/auth/wait$"), r_oauth_wait),
    ("POST", re.compile(r"^/o/oauth2/token$"), r_oauth_token),
    ("GET", re.compile(r"^/o/oauth2/revoke$"), r_oauth_revoke),
    ("POST", re.compile(r"^/o/oauth2/revoke$"), r_oauth_revoke),
    ("GET", re.compile(r"^/vitayt/authorized\.html$"), r_authorized),
    # Kategorien
    ("GET", re.compile(r"^/schemas/2007/categories\.cat$"), r_categories),
    ("GET", re.compile(r"^/schemas/2007/(subscriptiontypes|complaint-reasons)\.cat$"), r_cat_static),
    # Batch (vor allem anderen POST)
    ("POST", BATCH_RE, r_batch),
    # eigenes Konto (vor den allgemeinen users/<id>-Routen!)
    ("GET", DEF_RE, r_default_get),
    ("POST", DEF_RE, r_default_post),
    ("DELETE", DEF_RE, r_default_delete),
    # Videos
    ("GET", STD_RE, r_standardfeed),
    ("GET", RELATED_RE, r_related),
    ("GET", COMMENTS_RE, r_comments),
    ("POST", COMMENTS_RE, r_comment_post),
    ("POST", RATING_RE, r_rate),
    ("GET", VIDEO_RE, r_video),
    ("GET", SEARCH_RE, r_search),
    ("GET", re.compile(r"^/complete/search$"), r_suggest),
    ("GET", THUMB_RE, r_thumb),
    ("GET", re.compile(r"^/videoplayback$"), r_videoplayback),
    # Kanaele + Playlists
    ("GET", CHANNELS_RE, r_channels),
    ("GET", PL_SNIPPETS_RE, r_pl_snippets),
    ("GET", PLAYLIST_RE, r_playlist),
    ("POST", PL_ITEM_RE, r_playlist_post),
    ("DELETE", PL_ITEM_RE, r_playlist_delete),
    ("GET", USER_RE, r_user),
    ("GET", UPLOADS_RE, r_uploads),
    ("GET", USER_PLAYLISTS_RE, r_user_playlists),
    ("GET", EMPTY_RE, r_empty),
    # Sonstiges
    ("GET", re.compile(r"^/img$"), r_img),
    ("GET", re.compile(r"^/health$"), r_health),
    ("GET", re.compile(r"^/feeds/api/"), r_empty_any),      # Unbekanntes: leer statt Fehler
]


# ---------------------------------------------------------------- Server
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "vita-youtube/" + VERSION

    def handle_any(self):
        t0 = time.time()
        n = int(self.headers.get("Content-Length") or 0)
        body = (self.rfile.read(n) if n else b"").decode("utf-8", "replace")
        u = urllib.parse.urlsplit(self.path)
        path, query = u.path, urllib.parse.parse_qs(u.query)
        method = "GET" if self.command == "HEAD" else self.command
        code, ctype, data, extra = 404, "text/plain", b"", {}
        err = None
        self.streamed = None
        self.stream_err = None
        for m, rx, fn in ROUTES:
            if m == method and rx.match(path):
                try:
                    res = fn(self, path, query, body)
                    if res is not None:
                        code, ctype, data, extra = res
                except AuthError as e:
                    code, data, err = 401, b"<errors><error><code>TokenInvalid</code></error></errors>", str(e)
                    ctype = "application/vnd.google.gdata.error+xml"
                except ApiError as e:
                    code, data, err = 503, b"Service Unavailable", str(e)
                except Exception as e:  # nie die Verbindung haengen lassen
                    code, data, err = 500, b"Internal Error", f"{type(e).__name__}: {e}"
                break
        err = err or self.stream_err
        if self.streamed:   # Antwort wurde schon direkt geschrieben
            if err:         # Fehler mitten im Stream: Verbindung schliessen
                self.close_connection = True
            self.log_request_json(self.streamed[0], self.streamed[1], t0, body, err)
            return
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        for k, v in extra.items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)
        if DUMP_DIR and path.startswith(("/feeds/", "/o/oauth2/auth")):
            try:
                os.makedirs(DUMP_DIR, exist_ok=True)
                name = time.strftime("%H%M%S") + "_" + re.sub(r"[^A-Za-z0-9_.-]+", "_", path)[:80] + f"_{code}.xml"
                with open(os.path.join(DUMP_DIR, name), "wb") as f:
                    f.write(data)
                old = sorted(os.listdir(DUMP_DIR))
                for fn in old[:-60]:                 # nur die letzten 60 behalten
                    os.remove(os.path.join(DUMP_DIR, fn))
            except OSError:
                pass
        self.log_request_json(code, len(data), t0, body, err)

    do_GET = do_POST = do_PUT = do_DELETE = do_HEAD = handle_any

    def log_request_json(self, code, size, t0, body, err):
        ms = int((time.time() - t0) * 1000)
        ip = self.headers.get("X-Real-IP", self.client_address[0])
        thumb = self.path.startswith("/vi/")
        rec = {"t": time.strftime("%Y-%m-%d %H:%M:%S"), "ip": ip, "m": self.command,
               "path": self.path, "code": code, "size": size, "ms": ms}
        if self.headers.get("Range"):
            rec["range"] = self.headers.get("Range")
        if err:
            rec["err"] = err
        if not thumb:
            hdr = dict(self.headers.items())
            for k in list(hdr):
                if k.lower() in ("authorization", "cookie"):
                    hdr[k] = hdr[k][:12] + "...(geschwaerzt)"
            rec["hdr"] = hdr
            if body:
                rec["body"] = "(geschwaerzt)" if "/oauth2/" in self.path else body[:4000]
        if not thumb or code != 200:
            rng = f" [{self.headers.get('Range')}]" if self.headers.get("Range") else ""
            print(f"{ip} {self.command} {self.path}{rng} -> {code} {size}B {ms}ms" + (f" ERR {err}" if err else ""), flush=True)
        with _log_lock:
            try:
                if os.path.exists(REQ_LOG) and os.path.getsize(REQ_LOG) > 20 * 1024 * 1024:
                    os.replace(REQ_LOG, REQ_LOG + ".1")      # einfache Rotation, max. ~40 MB
                with open(REQ_LOG, "a") as f:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            except OSError:
                pass

    def log_message(self, *args):
        pass


def main():
    if not API_KEY:
        print("WARNUNG: YT_API_KEY fehlt - Feeds liefern 503", flush=True)
    print(f"vita-youtube-adapter {VERSION} auf {LISTEN_HOST}:{LISTEN_PORT}, Basis {PUBLIC_BASE}", flush=True)
    ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
