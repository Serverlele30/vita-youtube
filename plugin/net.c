/*
 * vytp_net - Server-Teil des PS-Vita-YouTube-Plugins (Stufe 2)
 *
 * Laeuft in der offiziellen YouTube-App (PCSF00150), nachgeladen von vytp.suprx.
 * Lauscht auf 127.0.0.1:8090. Der Adapter auf dem VPS traegt als Video-URL
 *   http://127.0.0.1:8090/videoplayback?v=<ID>&itag=18&type=video/mp4
 * ein. Fuer jede Anfrage:
 *   1. Player-API (InnerTube, ANDROID-Client) VON DER VITA AUS fragen
 *      -> direkte MP4-URL (itag 18) + Groesse. Die URL ist an die Vita-IP gebunden.
 *   2. HEAD: Groesse melden. GET (+Range): MP4 stueckweise per HTTPS bei
 *      googlevideo holen und unverschluesselt an die App weiterreichen.
 * HTTPS laeuft ueber das System-Modul SceHttp (mit iTLS-Enso modernes TLS).
 *
 * Diagnose: ux0:data/vytp_log.txt und GET http://45.129.183.3/vytp/ping?e=...
 */
#include <psp2/kernel/modulemgr.h>
#include <psp2/kernel/threadmgr.h>
#include <psp2/kernel/processmgr.h>
#include <psp2/kernel/clib.h>
#include <psp2/io/fcntl.h>
#include <psp2/io/stat.h>
#include <psp2/io/fcntl.h>
#include <psp2/net/net.h>
#include <psp2/net/http.h>

#define VYTP_VERSION "3.1"
#ifndef VERBOSE
#define VERBOSE 0   /* 1 = jede Anfrage loggen/melden (nur zur Fehlersuche) */
#endif
#define VYTP_PORT 8090
#define LOG_DIR   "ux0:data/vytp"
#define LOG_PATH  "ux0:data/vytp/log.txt"
#define LOG_PATH2 "ux0:data/vytp_log.txt"
#define PING_IP   0x2D81B703u   /* 45.129.183.3 */
#define PING_HOST "ytv.serverlele.de"

#define ANDROID_VER "21.26.364"
#define ANDROID_UA  "com.google.android.youtube/" ANDROID_VER " (Linux; U; Android 11) gzip"
#define PLAYER_URL  "https://youtubei.googleapis.com/youtubei/v1/player?prettyPrint=false" \
                    "&fields=responseContext(visitorData),playabilityStatus(status,reason)," \
                    "streamingData(formats(itag,url,contentLength))"
#define PLAYER_URL_FULL "https://youtubei.googleapis.com/youtubei/v1/player?prettyPrint=false"

/* Speicher sparsam: das Plugin lebt im Speicher der App (3.0 war zu gierig -> Absturz) */
#define MAX_CLIENTS 5                   /* HEAD-keep-alive + Stream + Spulen gleichzeitig */
#define UP_CHUNK    (2 * 1024 * 1024)   /* googlevideo in 2-MB-Stuecken holen (wie 2.3) */
#define IO_BUF      (16 * 1024)
#define N_CACHE     2                   /* zuletzt aufgeloeste Videos */
#define CACHE_TTL_S (3 * 3600)          /* googlevideo-URLs gelten ~6 h */
#define TRIES       3                   /* Versuche bei TLS-/Netzfehlern (z. B. 0x80431075) */
#define IDLE_RCV_US (30 * 1000 * 1000)  /* App-Verbindung ohne Anfrage nach 30 s schliessen */

/* -nostdlib: falls gcc memset/memcpy erzeugt */
__attribute__((weak, optimize("no-tree-loop-distribute-patterns")))
void *memset(void *d, int c, __SIZE_TYPE__ n) { volatile unsigned char *p = d; while (n--) *p++ = (unsigned char)c; return d; }
__attribute__((weak, optimize("no-tree-loop-distribute-patterns")))
void *memcpy(void *d, const void *s, __SIZE_TYPE__ n) {
    volatile unsigned char *p = d; const unsigned char *q = s; while (n--) *p++ = *q++; return d; }

static volatile int g_run = 1;
static SceUID g_lock = -1;         /* schuetzt Cache + Besucher-ID */
static volatile int g_clients = 0;

/* ------------------------------------------------------------ Log / Ping */
static void log_to(const char *path, const char *msg) {
    SceUID fd = sceIoOpen(path, SCE_O_WRONLY | SCE_O_CREAT | SCE_O_APPEND, 0666);
    if (fd < 0) return;
    sceIoWrite(fd, msg, sceClibStrnlen(msg, 512));
    sceIoWrite(fd, "\n", 1);
    sceIoClose(fd);
}

static void log_line(const char *msg) {
    log_to(LOG_PATH, msg);
    log_to(LOG_PATH2, msg);
}

static void ping(const char *event) {
    char q[200];
    int j = 0;
    for (int i = 0; event[i] && j < (int)sizeof q - 1; i++) {
        char ch = event[i];
        int ok = (ch >= 'a' && ch <= 'z') || (ch >= 'A' && ch <= 'Z') ||
                 (ch >= '0' && ch <= '9') || ch == '-' || ch == '.' || ch == '/';
        q[j++] = ok ? ch : '_';
    }
    q[j] = 0;

    int s = sceNetSocket("vytp_ping", SCE_NET_AF_INET, SCE_NET_SOCK_STREAM, 0);
    if (s < 0) return;
    SceNetSockaddrIn a;
    sceClibMemset(&a, 0, sizeof a);
    a.sin_len = sizeof a;
    a.sin_family = SCE_NET_AF_INET;
    a.sin_port = sceNetHtons(80);
    a.sin_addr.s_addr = sceNetHtonl(PING_IP);
    if (sceNetConnect(s, (SceNetSockaddr *)&a, sizeof a) >= 0) {
        char req[320];
        int n = sceClibSnprintf(req, sizeof req,
            "GET /vytp/ping?e=%s HTTP/1.0\r\nHost: " PING_HOST "\r\n"
            "User-Agent: vytp/" VYTP_VERSION "\r\n\r\n", q);
        sceNetSend(s, req, n, 0);
        char buf[64];
        sceNetRecv(s, buf, sizeof buf, 0);
    }
    sceNetSocketClose(s);
}

/* Log + Ping in einem (fuer Fehler und Start) */
static void event(const char *fmt, const char *s1, int v) {
    char buf[200];
    sceClibSnprintf(buf, sizeof buf, fmt, s1 ? s1 : "", v);
    log_line(buf);
    ping(buf);
}

#if VERBOSE
#define info(fmt, s1, v) event(fmt, s1, v)
#define vlog(msg) log_line(msg)
#define vping(msg) ping(msg)
#else
#define info(fmt, s1, v) do { } while (0)
#define vlog(msg) do { } while (0)
#define vping(msg) do { } while (0)
#endif

/* ------------------------------------------------------------ kleine Helfer */
static int send_all(int s, const char *p, int len) {
    while (len > 0) {
        int n = sceNetSend(s, p, len, 0);
        if (n <= 0) return -1;
        p += n;
        len -= n;
    }
    return 0;
}

static int send_simple(int c, int code, const char *text) {
    char hdr[200];
    int tl = sceClibStrnlen(text, 100);
    int n = sceClibSnprintf(hdr, sizeof hdr,
        "HTTP/1.1 %d X\r\nContent-Type: text/plain\r\nContent-Length: %d\r\n"
        "Connection: close\r\n\r\n", code, tl);
    if (send_all(c, hdr, n) < 0) return -1;
    return send_all(c, text, tl);
}

/* unsigned long long -> Dezimaltext (eigene Funktion, %llu ist im Vita-snprintf unsicher) */
static const char *u64s(unsigned long long v, char *out) {
    char tmp[24];
    int i = 0;
    do { tmp[i++] = (char)('0' + (int)(v % 10)); v /= 10; } while (v && i < 23);
    int j = 0;
    while (i > 0) out[j++] = tmp[--i];
    out[j] = 0;
    return out;
}

static unsigned long long parse_u64(const char *p) {
    unsigned long long v = 0;
    while (*p >= '0' && *p <= '9') { v = v * 10 + (unsigned)(*p - '0'); p++; }
    return v;
}

/* Wert eines Query-Parameters (?v=...) holen */
static int query_param(const char *line, const char *name, char *out, int cap) {
    const char *q = sceClibStrstr(line, "?");
    if (!q) return -1;
    int nl = sceClibStrnlen(name, 32);
    const char *p = q + 1;
    while (*p && *p != ' ') {
        if (sceClibStrncmp(p, name, nl) == 0 && p[nl] == '=') {
            p += nl + 1;
            int i = 0;
            while (*p && *p != '&' && *p != ' ' && i < cap - 1) out[i++] = *p++;
            out[i] = 0;
            return i;
        }
        while (*p && *p != '&' && *p != ' ') p++;
        if (*p == '&') p++;
    }
    return -1;
}

static int valid_vid(const char *v) {
    int n = 0;
    for (; v[n]; n++) {
        char ch = v[n];
        if (!((ch >= 'a' && ch <= 'z') || (ch >= 'A' && ch <= 'Z') ||
              (ch >= '0' && ch <= '9') || ch == '-' || ch == '_')) return 0;
    }
    return n == 11;
}

/* JSON-String ab p (nach dem oeffnenden ") nach out kopieren, \uXXXX/\/ aufloesen */
static int json_str(const char *p, char *out, int cap) {
    int i = 0;
    while (*p && *p != '"' && i < cap - 1) {
        if (*p == '\\' && p[1]) {
            p++;
            if (*p == 'u') {
                int v = 0;
                for (int k = 1; k <= 4; k++) {
                    char h = p[k];
                    v <<= 4;
                    if (h >= '0' && h <= '9') v |= h - '0';
                    else if (h >= 'a' && h <= 'f') v |= h - 'a' + 10;
                    else if (h >= 'A' && h <= 'F') v |= h - 'A' + 10;
                }
                out[i++] = (v < 128) ? (char)v : '?';
                p += 5;
                continue;
            }
            out[i++] = *p++;   /* \/ \" \\ */
            continue;
        }
        out[i++] = *p++;
    }
    out[i] = 0;
    return (*p == '"') ? i : -1;
}

/* ------------------------------------------------------------ SceHttp */
static unsigned int now_s(void) { return (unsigned int)(sceKernelGetProcessTimeWide() / 1000000ull); }
static int g_tmpl = -1;

static int http_template(void) {
    if (g_tmpl >= 0) return g_tmpl;
    int r = sceHttpInit(1 * 1024 * 1024);   /* App hat meist schon init -> Fehler egal */
    if (r < 0) log_line("http-init: schon initialisiert oder Fehler (ok)");
    g_tmpl = sceHttpCreateTemplate(ANDROID_UA, SCE_HTTP_VERSION_1_1, SCE_TRUE);
    if (g_tmpl < 0) { event("http-template-fehler 0x%s%08X", "", g_tmpl); return g_tmpl; }
    /* feste Zeitlimits statt endlos haengen (App zeigt sonst irgendwann einen Fehler) */
    sceHttpSetResolveTimeOut(g_tmpl, 5 * 1000 * 1000);
    sceHttpSetResolveRetry(g_tmpl, 2);
    sceHttpSetConnectTimeOut(g_tmpl, 8 * 1000 * 1000);
    sceHttpSetSendTimeOut(g_tmpl, 10 * 1000 * 1000);
    sceHttpSetRecvTimeOut(g_tmpl, 15 * 1000 * 1000);
    return g_tmpl;
}

/* KEIN globaler Verbindungs-Pool (offene TLS-Verbindungen belegen den HTTP-Speicher der App).
 * Nur innerhalb EINES Streams wird die googlevideo-Verbindung fuer die Folge-Stuecke behalten. */
static SceUID g_slot_lock = -1;

typedef struct { int req, conn; } hreq_t;

/* Ein HTTP-Request mit Wiederholung bei TLS-/Netzfehlern; Status in *status. 0 = ok.
 * reuse=1: vorhandene h->conn weiterverwenden (keep-alive), sonst neue Verbindung. */
static int http_open(hreq_t *h, int reuse, int method, const char *url, const char **hdrs,
                     const void *body, int body_len, int *status) {
    int tmpl = http_template();
    if (tmpl < 0) return tmpl;
    int r = -1;
    h->req = -1;
    if (!reuse && h->conn >= 0) { sceHttpDeleteConnection(h->conn); h->conn = -1; }
    for (int t = 0; t < TRIES; t++) {
        if (t) sceKernelDelayThread((t == 1 ? 300 : 1200) * 1000);
        int reused = h->conn >= 0;
        if (!reused) {
            h->conn = sceHttpCreateConnectionWithURL(tmpl, url, SCE_TRUE);
            if (h->conn < 0) { r = h->conn; h->conn = -1; continue; }
        }
        h->req = sceHttpCreateRequestWithURL(h->conn, method, url, (SceULong64)(body ? body_len : 0));
        if (h->req < 0) {
            r = h->req; h->req = -1;
            sceHttpDeleteConnection(h->conn); h->conn = -1;
            if (reused) t--;
            continue;
        }
        for (int i = 0; hdrs && hdrs[i]; i += 2)
            sceHttpAddRequestHeader(h->req, hdrs[i], hdrs[i + 1], SCE_HTTP_HEADER_OVERWRITE);
        r = sceHttpSendRequest(h->req, body, body ? body_len : 0);
        if (r < 0) {
            sceHttpDeleteRequest(h->req); h->req = -1;
            sceHttpDeleteConnection(h->conn); h->conn = -1;   /* kaputte/geschlossene Verbindung */
            if (reused) t--;                 /* alte keep-alive-Verbindung zaehlt nicht als Versuch */
            continue;
        }
        sceHttpGetStatusCode(h->req, status);
        return 0;
    }
    return r;
}

/* keep_conn=1: Verbindung fuer den naechsten Request offen lassen (nur wenn Antwort komplett) */
static void http_close(hreq_t *h, int keep_conn) {
    if (h->req >= 0) sceHttpDeleteRequest(h->req);
    h->req = -1;
    if (!keep_conn && h->conn >= 0) { sceHttpDeleteConnection(h->conn); h->conn = -1; }
}

/* ------------------------------------------------------------ Player-API + Cache */
typedef struct {
    char vid[12];
    char url[4096];
    unsigned long long len;
    char hdrs[1024];        /* gespiegelte googlevideo-Kopfzeilen */
    unsigned int t;         /* Zeitpunkt der Aufloesung (Sekunden) */
    unsigned int used;
} vcache_t;
static vcache_t g_vc[N_CACHE];
static char g_visitor[400];          /* X-Goog-Visitor-Id */
static char g_resp[96 * 1024];       /* Player-Antwort (gekuerzt per fields=) */
static char g_errmsg[64];


static int read_body(int req, char *buf, int cap) {
    int got = 0;
    while (got < cap - 1) {
        int n = sceHttpReadData(req, buf + got, cap - 1 - got);
        if (n <= 0) break;
        got += n;
    }
    buf[got] = 0;
    return got;
}

/* Rest einer Antwort verwerfen, damit die Verbindung wiederverwendbar bleibt */
static int drain(int req) {
    char tmp[256];
    for (int i = 0; i < 64; i++) {
        int n = sceHttpReadData(req, tmp, sizeof tmp);
        if (n == 0) return 1;
        if (n < 0) return 0;
    }
    return 0;
}

/* Einmal Player fragen. 1 = OK (URL in e), 0 = gesperrt (retry sinnvoll), <0 = Fehler */
static int player_once(const char *vid, int use_fields, vcache_t *e) {
    char body[512];
    int bl = sceClibSnprintf(body, sizeof body,
        "{\"context\":{\"client\":{\"clientName\":\"ANDROID\",\"clientVersion\":\"" ANDROID_VER "\","
        "\"androidSdkVersion\":30,\"userAgent\":\"" ANDROID_UA "\",\"osName\":\"Android\","
        "\"osVersion\":\"11\",\"hl\":\"de\",\"gl\":\"DE\"}},"
        "\"videoId\":\"%s\",\"contentCheckOk\":true,\"racyCheckOk\":true}", vid);

    const char *hdrs[12];
    int h = 0;
    hdrs[h++] = "Content-Type";            hdrs[h++] = "application/json";
    hdrs[h++] = "X-Youtube-Client-Name";   hdrs[h++] = "3";
    hdrs[h++] = "X-Youtube-Client-Version"; hdrs[h++] = ANDROID_VER;
    if (g_visitor[0]) { hdrs[h++] = "X-Goog-Visitor-Id"; hdrs[h++] = g_visitor; }
    hdrs[h] = 0;

    hreq_t hr = {-1, -1};
    int status = 0;
    int r = http_open(&hr, 0, SCE_HTTP_METHOD_POST, use_fields ? PLAYER_URL : PLAYER_URL_FULL,
                      hdrs, body, bl, &status);
    if (r < 0) { event("player-https-fehler 0x%s%08X", "", r); return r; }
    int got = read_body(hr.req, g_resp, sizeof g_resp);
    http_close(&hr, 0);
    if (status != 200) {
        event("player-http %s%d", "", status);
        return status == 400 ? -400 : (status >= 500 ? 0 : -1);
    }

    const char *vd = sceClibStrstr(g_resp, "\"visitorData\":\"");
    if (vd) json_str(vd + 15, g_visitor, sizeof g_visitor);

    const char *ps = sceClibStrstr(g_resp, "\"playabilityStatus\":{");
    const char *st = ps ? sceClibStrstr(ps, "\"status\":\"") : 0;
    char status_s[32];
    status_s[0] = '?'; status_s[1] = 0;
    if (st) json_str(st + 10, status_s, sizeof status_s);
    if (sceClibStrncmp(status_s, "OK", 3) != 0) {
        sceClibSnprintf(g_errmsg, sizeof g_errmsg, "%s", status_s);
        event("player-status %s (%d B)", status_s, got);
        return sceClibStrncmp(status_s, "LOGIN_REQUIRED", 15) == 0 ? 0 : -2;
    }

    /* itag 18 suchen (nicht 180, 181 ...) */
    const char *it = g_resp;
    while ((it = sceClibStrstr(it, "\"itag\":18")) != 0) {
        if (it[9] == ',' || it[9] == '}') break;
        it += 9;
    }
    if (!it) { event("kein-itag18 %s%d", "", got); return -3; }
    const char *ob = it;
    while (ob > g_resp && *ob != '{') ob--;
    const char *oe = sceClibStrstr(it + 9, "\"itag\":");
    if (!oe) oe = g_resp + got;
    const char *u = sceClibStrstr(ob, "\"url\":\"");
    if (u && u >= oe) u = 0;
    const char *cl = sceClibStrstr(ob, "\"contentLength\":\"");
    if (cl && cl >= oe) cl = 0;
    if (!u) { event("itag18-ohne-url %s%d", "", got); return -3; }
    if (json_str(u + 7, e->url, sizeof e->url) < 0) {
        event("url-zu-lang %s%d", "", got);
        return -3;
    }
    e->len = cl ? parse_u64(cl + 17) : 0;
    e->hdrs[0] = 0;
    return 1;
}

/* Gesamtgroesse per Range-Probe, falls contentLength fehlte */
static int probe_len(vcache_t *e) {
    const char *hdrs[] = {"Range", "bytes=0-0", 0};
    hreq_t hr = {-1, -1};
    int status = 0;
    if (http_open(&hr, 0, SCE_HTTP_METHOD_GET, e->url, hdrs, 0, 0, &status) < 0) return -1;
    char *all = 0;
    unsigned int alen = 0;
    int ok = -1;
    if (status == 206 && sceHttpGetAllResponseHeaders(hr.req, &all, &alen) >= 0) {
        const char *val = 0;
        unsigned int vlen = 0;
        if (sceHttpParseResponseHeader(all, alen, "Content-Range", &val, &vlen) >= 0 && val) {
            for (unsigned int i = 0; i < vlen; i++) if (val[i] == '/') {
                e->len = parse_u64(val + i + 1);
                ok = 0;
                break;
            }
        }
    }
    drain(hr.req);
    http_close(&hr, 0);
    if (ok < 0) event("probe-http %s%d", "", status);
    return ok;
}

/* Kopfzeilen der echten googlevideo-Antwort (per HEAD) fuer die App uebernehmen */
static void add_hdr(char *dst, int cap, const char *all, unsigned int alen, const char *name) {
    const char *val = 0;
    unsigned int vlen = 0;
    if (sceHttpParseResponseHeader(all, alen, name, &val, &vlen) < 0 || !val) return;
    int used = sceClibStrnlen(dst, cap);
    int nl = sceClibStrnlen(name, 64);
    if (used + nl + (int)vlen + 5 >= cap) return;
    char *o = dst + used;
    for (int i = 0; i < nl; i++) *o++ = name[i];
    *o++ = ':'; *o++ = ' ';
    for (unsigned int i = 0; i < vlen; i++) *o++ = val[i];
    *o++ = '\r'; *o++ = '\n'; *o = 0;
}

static void upstream_headers(vcache_t *e) {
    hreq_t hr = {-1, -1};
    int status = 0;
    int r = http_open(&hr, 0, SCE_HTTP_METHOD_HEAD, e->url, 0, 0, 0, &status);
    if (r < 0) { event("gv-head-fehler 0x%s%08X", "", r); return; }
    char *all = 0;
    unsigned int alen = 0;
    e->hdrs[0] = 0;
    if (sceHttpGetAllResponseHeaders(hr.req, &all, &alen) >= 0 && all) {
        add_hdr(e->hdrs, sizeof e->hdrs, all, alen, "Last-Modified");
        add_hdr(e->hdrs, sizeof e->hdrs, all, alen, "Cache-Control");
        add_hdr(e->hdrs, sizeof e->hdrs, all, alen, "Expires");
        add_hdr(e->hdrs, sizeof e->hdrs, all, alen, "Date");
        add_hdr(e->hdrs, sizeof e->hdrs, all, alen, "Server");
        add_hdr(e->hdrs, sizeof e->hdrs, all, alen, "X-Content-Type-Options");
        if (!e->hdrs[0]) { e->hdrs[0] = ' '; e->hdrs[1] = 0; }   /* "versucht" markieren */
    }
    http_close(&hr, 0);
    info("gv-head %s%d", "", status);
}

/* Video aufloesen (Cache mit N_CACHE Eintraegen). Aufruf NUR mit g_lock. Rueckgabe Eintrag/0 */
static vcache_t *resolve(const char *vid, int force) {
    vcache_t *e = 0, *lru = &g_vc[0];
    for (int i = 0; i < N_CACHE; i++) {
        if (sceClibStrncmp(g_vc[i].vid, vid, 12) == 0) e = &g_vc[i];
        if (g_vc[i].used < lru->used) lru = &g_vc[i];
    }
    unsigned int t = now_s();
    if (e && !force && e->url[0] && t - e->t < CACHE_TTL_S) { e->used = t + 1; return e; }
    if (!e) e = lru;
    e->vid[0] = 0;
    e->url[0] = 0;
    int use_fields = 1;
    static const int delays_ms[] = {0, 1000, 3000};
    for (int a = 0; a < 3; a++) {
        if (delays_ms[a]) sceKernelDelayThread(delays_ms[a] * 1000);
        int r = player_once(vid, use_fields, e);
        if (r == -400 && use_fields) { use_fields = 0; a--; continue; }  /* fields nicht akzeptiert */
        if (r == 1) {
            if (e->len == 0 && probe_len(e) < 0) return 0;
            sceClibStrncpy(e->vid, vid, sizeof e->vid);
            e->t = t;
            e->used = t + 1;
            info("aufgeloest %s %d", vid, (int)(e->len / 1024));
            return e;
        }
        if (r != 0) return 0;      /* 0 = kurz gesperrt / 5xx -> nochmal; sonst endgueltig */
    }
    return 0;
}

/* ------------------------------------------------------------ Client */
static char g_iobuf[MAX_CLIENTS][IO_BUF];
static char g_slot_url[MAX_CLIENTS][4096];   /* eigene URL-Kopie je Stream (Cache kann wechseln) */
static volatile int g_slot_used[MAX_CLIENTS];

/* start..end an die App senden. 0 = fertig, 1 = App hat abgebrochen, -1 = Fehler */
static int stream_range(int c, char *buf, char *url, unsigned long long start,
                        unsigned long long end, const char *vid) {
    unsigned long long pos = start;
    int refreshed = 0, fails = 0, ret = 0;
    hreq_t hr = {-1, -1};                     /* eine googlevideo-Verbindung fuer alle Stuecke */
    while (pos <= end) {
        unsigned long long b = pos + UP_CHUNK - 1;
        if (b > end) b = end;
        char range[64], s1[24], s2[24];
        sceClibSnprintf(range, sizeof range, "bytes=%s-%s", u64s(pos, s1), u64s(b, s2));
        const char *hdrs[] = {"Range", range, 0};
        int status = 0;
        int r = http_open(&hr, 1, SCE_HTTP_METHOD_GET, url, hdrs, 0, 0, &status);
        if (r < 0) {
            if (++fails <= 2) { sceKernelDelayThread(1000 * 1000); continue; }
            event("gv-https-fehler 0x%s%08X", "", r);
            ret = -1;
            break;
        }
        if ((status == 403 || status == 410) && !refreshed) {   /* URL abgelaufen -> neu holen */
            http_close(&hr, 0);
            sceKernelLockMutex(g_lock, 1, NULL);
            vcache_t *e = resolve(vid, 1);
            if (e) sceClibStrncpy(url, e->url, 4096);
            sceKernelUnlockMutex(g_lock, 1);
            if (!e) { ret = -1; break; }
            refreshed = 1;
            continue;
        }
        if (status != 206 && status != 200) {
            http_close(&hr, 0);
            if (status >= 500 && ++fails <= 2) { sceKernelDelayThread(1000 * 1000); continue; }
            event("gv-http %s%d", "", status);
            ret = -1;
            break;
        }
        if (pos == start) info("gv-ok %s%d", "", status);
        int n = 0;
        while (pos <= b) {
            n = sceHttpReadData(hr.req, buf, IO_BUF);
            if (n <= 0) break;
            if (send_all(c, buf, n) < 0) { ret = 1; break; }   /* App hat abgebrochen */
            pos += (unsigned)n;
        }
        if (ret == 1) { http_close(&hr, 0); break; }
        int complete = (pos > b) && drain(hr.req);
        http_close(&hr, complete);
        if (pos <= b) {                       /* Stueck unvollstaendig -> ab pos weitermachen */
            if (++fails <= 3) { info("gv-luecke %s%d", "", n); continue; }
            ret = -1;
            break;
        }
        fails = 0;
    }
    http_close(&hr, 0);
    return ret;
}

static int handle_one(int c, char *buf, char *url, char *req, int got) {
    (void)got;
    vlog("--- Anfrage der App ---");
    vlog(req);

    int is_head = sceClibStrncmp(req, "HEAD ", 5) == 0;
    int is_get = sceClibStrncmp(req, "GET ", 4) == 0;
    char vid[16];
    if ((!is_head && !is_get) || !sceClibStrstr(req, "/videoplayback") ||
        query_param(req, "v", vid, sizeof vid) < 0 || !valid_vid(vid)) {
        send_simple(c, 404, "not found");
        return 0;
    }

    /* Range-Header lesen */
    unsigned long long rs = 0, re = 0;
    int has_range = 0, suffix = 0;
    const char *rh = sceClibStrstr(req, "\nRange: bytes=");
    if (!rh) rh = sceClibStrstr(req, "\nrange: bytes=");
    if (rh) {
        const char *p = rh + 14;
        has_range = 1;
        if (*p == '-') { suffix = 1; re = parse_u64(p + 1); }
        else {
            rs = parse_u64(p);
            while (*p >= '0' && *p <= '9') p++;
            if (*p == '-' && p[1] >= '0' && p[1] <= '9') re = parse_u64(p + 1);
            else re = (unsigned long long)-1;
        }
    }

    char up_hdrs[1024];
    sceKernelLockMutex(g_lock, 1, NULL);
    vcache_t *e = resolve(vid, 0);
    if (e && !e->hdrs[0]) upstream_headers(e);
    unsigned long long total = e ? e->len : 0;
    if (e) {
        sceClibStrncpy(url, e->url, 4096);
        sceClibStrncpy(up_hdrs, e->hdrs[0] == ' ' ? "" : e->hdrs, sizeof up_hdrs);
    }
    sceKernelUnlockMutex(g_lock, 1);
    if (!e || total == 0) {
        send_simple(c, 503, g_errmsg[0] ? g_errmsg : "stream unavailable");
        return 0;
    }

    unsigned long long start = 0, end = total - 1;
    if (has_range) {
        if (suffix) { start = (re >= total) ? 0 : total - re; }
        else {
            start = rs;
            if (re != (unsigned long long)-1 && re < total) end = re;
        }
        if (start >= total) {
            char h416[128], t1[24];
            int n = sceClibSnprintf(h416, sizeof h416,
                "HTTP/1.1 416 X\r\nContent-Range: bytes */%s\r\nContent-Length: 0\r\n"
                "Connection: close\r\n\r\n", u64s(total, t1));
            send_all(c, h416, n);
            return 0;
        }
    }

    char hdr[1500], a1[24], a2[24], a3[24], a4[24];
    const char *connh = is_head ? "keep-alive" : "close";
    int n;
    if (has_range)
        n = sceClibSnprintf(hdr, sizeof hdr,
            "HTTP/1.1 206 Partial Content\r\n%sContent-Type: video/mp4\r\nAccept-Ranges: bytes\r\n"
            "Content-Range: bytes %s-%s/%s\r\nContent-Length: %s\r\nConnection: %s\r\n\r\n",
            up_hdrs, u64s(start, a1), u64s(end, a2), u64s(total, a3), u64s(end - start + 1, a4), connh);
    else
        n = sceClibSnprintf(hdr, sizeof hdr,
            "HTTP/1.1 200 OK\r\n%sContent-Type: video/mp4\r\nAccept-Ranges: bytes\r\n"
            "Content-Length: %s\r\nConnection: %s\r\n\r\n", up_hdrs, u64s(total, a1), connh);
    if (n >= (int)sizeof hdr) n = sizeof hdr - 1;
    if (send_all(c, hdr, n) < 0) return 0;
    if (is_head) return 1;   /* Verbindung offen lassen, App macht ggf. weiter */

    int sr = stream_range(c, buf, url, start, end, vid);
    if (sr < 0) event("stream-abbruch %s %d", vid, (int)(start / 1024));
    else if (sr == 1) info("app-hat-abgebrochen %s %d", vid, 0);
    else info("stream-fertig %s %d", vid, 0);
    return 0;
}

/* Verbindung bedienen: mehrere Anfragen hintereinander (keep-alive nach HEAD) */
static void handle_client(int c, int slot) {
    static char reqs[MAX_CLIENTS][1536];
    char *req = reqs[slot];
    int tmo = IDLE_RCV_US;
    sceNetSetsockopt(c, SCE_NET_SOL_SOCKET, SCE_NET_SO_RCVTIMEO, &tmo, sizeof tmo);
    for (int round = 0; round < 8; round++) {
        int got = 0;
        req[0] = 0;
        while (got < 1535) {
            int n = sceNetRecv(c, req + got, 1535 - got, 0);
            if (n <= 0) break;
            got += n;
            req[got] = 0;
            if (sceClibStrstr(req, "\r\n\r\n")) break;
        }
        if (got <= 0) return;
        if (!handle_one(c, g_iobuf[slot], g_slot_url[slot], req, got)) return;
    }
}

static int client_thread(SceSize args, void *argp) {
    int c = (args >= sizeof(int)) ? *(int *)argp : -1;
    int slot = -1;
    sceKernelLockMutex(g_slot_lock, 1, NULL);
    for (int i = 0; i < MAX_CLIENTS; i++)
        if (!g_slot_used[i]) { g_slot_used[i] = 1; slot = i; break; }
    sceKernelUnlockMutex(g_slot_lock, 1);
    if (c >= 0) {
        if (slot >= 0) handle_client(c, slot);
        else send_simple(c, 503, "busy");
        sceNetSocketClose(c);
    }
    if (slot >= 0) g_slot_used[slot] = 0;
    g_clients--;
    return sceKernelExitDeleteThread(0);
}

static int server_thread(SceSize args, void *argp) {
    (void)args; (void)argp;
    int s = -1;
    for (int tries = 0; g_run && tries < 120; tries++) {
        s = sceNetSocket("vytp_srv", SCE_NET_AF_INET, SCE_NET_SOCK_STREAM, 0);
        if (s >= 0) break;
        sceKernelDelayThread(1000 * 1000);
    }
    if (s < 0) { log_line("socket fehlgeschlagen"); return 0; }

    int one = 1;
    sceNetSetsockopt(s, SCE_NET_SOL_SOCKET, SCE_NET_SO_REUSEADDR, &one, sizeof one);
    SceNetSockaddrIn addr;
    sceClibMemset(&addr, 0, sizeof addr);
    addr.sin_len = sizeof addr;
    addr.sin_family = SCE_NET_AF_INET;
    addr.sin_port = sceNetHtons(VYTP_PORT);
    addr.sin_addr.s_addr = sceNetHtonl(0x7F000001);
    int r = sceNetBind(s, (SceNetSockaddr *)&addr, sizeof addr);
    if (r < 0) { event("bind-fehler 0x%s%08X", "", r); sceNetSocketClose(s); return 0; }
    r = sceNetListen(s, 8);
    if (r < 0) { event("listen-fehler 0x%s%08X", "", r); sceNetSocketClose(s); return 0; }
    event("server-laeuft-8090 v%s %d", VYTP_VERSION, 0);

    while (g_run) {
        SceNetSockaddrIn peer;
        unsigned int plen = sizeof peer;
        int c = sceNetAccept(s, (SceNetSockaddr *)&peer, &plen);
        if (c < 0) { sceKernelDelayThread(100 * 1000); continue; }
        if (g_clients >= MAX_CLIENTS) { send_simple(c, 503, "busy"); sceNetSocketClose(c); continue; }
        SceUID t = sceKernelCreateThread("vytp_client", client_thread, 0x10000100, 0x8000, 0, 0, NULL);
        if (t < 0) { send_simple(c, 503, "no thread"); sceNetSocketClose(c); continue; }
        g_clients++;
        sceKernelStartThread(t, sizeof c, &c);
    }
    sceNetSocketClose(s);
    return 0;
}

int module_start(SceSize args, void *argp);
int _start(SceSize args, void *argp) __attribute__((weak, alias("module_start")));
int module_start(SceSize args, void *argp) {
    (void)args; (void)argp;
    sceIoMkdir(LOG_DIR, 0777);
    log_line("vytp_net " VYTP_VERSION " gestartet");
    g_lock = sceKernelCreateMutex("vytp_lock", 0, 0, NULL);
    g_slot_lock = sceKernelCreateMutex("vytp_slot", 0, 0, NULL);
    SceUID t = sceKernelCreateThread("vytp_server", server_thread, 0x10000100, 0x4000, 0, 0, NULL);
    if (t >= 0) sceKernelStartThread(t, 0, NULL);
    else log_line("server-thread fehlgeschlagen");
    return SCE_KERNEL_START_SUCCESS;
}

int module_stop(SceSize args, void *argp) {
    (void)args; (void)argp;
    g_run = 0;
    return SCE_KERNEL_STOP_SUCCESS;
}
