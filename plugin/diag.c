/*
 * vytp_diag (Runde 2) - Diagnose fuer das LOESCHEN aus Favoriten / Spaeter ansehen / Playlists
 * (nur zur Fehlersuche; der Lader laedt sie, wenn ux0:tai/vytp_diag.suprx existiert).
 * Schreibt nach ux0:data/vytp_diag.txt:
 *   - conv:   Listen-Eintraege beim Laden (Typ 0=Fav 1=WL 2=Playlist) + alle Textfelder des 1. Eintrags
 *   - delete: Loesch-Job: Typ, Anzahl, Loesch-Info je Eintrag, Rueckgabe
 *   - req_*:  die drei Loesch-Anfragen, send: jede HTTP-Anfrage
 */
#include <psp2/kernel/modulemgr.h>
#include <psp2/kernel/clib.h>
#include <psp2/io/fcntl.h>
#include <taihen.h>

/* DIAG-VERSION 3 */
/* ohne Standardbibliothek: gcc erzeugt trotzdem memset/memcpy-Aufrufe -> selbst bereitstellen */
__attribute__((optimize("no-tree-loop-distribute-patterns")))
void *memset(void *d, int c, __SIZE_TYPE__ n) { volatile unsigned char *p = d; while (n--) *p++ = (unsigned char)c; return d; }
__attribute__((optimize("no-tree-loop-distribute-patterns")))
void *memcpy(void *d, const void *s, __SIZE_TYPE__ n) {
    volatile unsigned char *p = d; const unsigned char *q = s; while (n--) *p++ = *q++; return d; }

#define LOGP "ux0:data/vytp_diag.txt"
#define BASE 0x81000000u
#define EMPTY 0x81454858u   /* leerer String der App */

static int slen(const char *s) { int n = 0; while (s[n] && n < 400) n++; return n; }
static void wlog(const char *s) {
    SceUID fd = sceIoOpen(LOGP, SCE_O_WRONLY | SCE_O_CREAT | SCE_O_APPEND, 0666);
    if (fd < 0) return;
    sceIoWrite(fd, s, slen(s));
    sceIoWrite(fd, "\n", 1);
    sceIoClose(fd);
}
#define LOG(...) do { char _m[400]; sceClibSnprintf(_m, sizeof _m, __VA_ARGS__); wlog(_m); } while (0)

/* plausibler String {char*,len}? nur Zeiger nahe am Objekt oder im App-Datensegment */
static int str_ok(unsigned obj, unsigned p, int len) {
    if (len <= 0 || len > 200) return 0;
    if (p == EMPTY) return 0;
    if (p >= 0x81000000u && p < 0x81480000u) return 1;
    unsigned d = p > obj ? p - obj : obj - p;
    return d < 0x01000000u;
}
static void cpy(char *dst, const char *p, int len, int max) {
    int i = 0;
    for (; i < len && i < max - 1; i++) {
        char c = p[i];
        dst[i] = (c >= 32 && c < 127) ? c : '.';
    }
    dst[i] = 0;
}

#define N_HOOKS 6
static tai_hook_ref_t ref[N_HOOKS];
static SceUID hid[N_HOOKS];

/* 0  0x812a5696(entries{ptr,count}, int *type|NULL, out) - Eintraege -> Listenobjekte */
static int conv_dumped[3];
static int h_conv(int a, int b, int c, int d) {
    int type = b ? *(int *)b : -1;
    int n = a ? ((int *)a)[1] : -1;
    LOG("conv typ=%d eintraege=%d", type, n);
    if (a && n > 0 && type >= 0 && type < 3 && !conv_dumped[type]) {
        conv_dumped[type] = 1;
        unsigned e = (unsigned)((int *)a)[0];
        for (int off = 0; off + 8 <= 0x158; off += 4) {
            unsigned p = *(unsigned *)(e + off);
            int len = *(int *)(e + off + 4);
            if (!str_ok(e, p, len)) continue;
            char s[90];
            cpy(s, (const char *)p, len, sizeof s);
            LOG("  typ%d +0x%03X len=%d '%s'", type, off, len, s);
        }
    }
    return TAI_CONTINUE(int, ref[0], a, b, c, d);
}

/* 1  0x8129a576(this, acc, list{ptr,count}, type) - Loesch-Job */
static int h_del(int a, int b, int c, int d) {
    int n = c ? ((int *)c)[1] : -1;
    LOG("delete typ=%d auswahl=%d", d, n);
    for (int i = 0; c && i < n && i < 3; i++) {
        unsigned it = (unsigned)((int **)((int *)c)[0])[i];
        if (!it) { LOG("  [%d] null", i); continue; }
        unsigned info = *(unsigned *)(it + 0x20);
        char s[90];
        s[0] = '-'; s[1] = 0;
        int ityp = -1, len = 0;
        if (info) {
            ityp = *(int *)info;
            unsigned p = *(unsigned *)(info + 4);
            len = *(int *)(info + 8);
            if (str_ok(info, p, len)) cpy(s, (const char *)p, len, sizeof s);
        }
        unsigned vt = *(unsigned *)it;
        LOG("  [%d] obj=0x%08X vtab=0x%08X info=0x%08X typ=%d id(%d)='%s'", i, it, vt, info, ityp, len, s);
    }
    int r = TAI_CONTINUE(int, ref[1], a, b, c, d);
    LOG("delete ret=%d", r);
    return r;
}

/* 2-4 die eigentlichen Loesch-Anfragen */
static int h_rfav(int a, int b, int c, int d) {
    LOG("req_fav_delete anzahl=%d", c ? ((int *)c)[1] : -1);
    int r = TAI_CONTINUE(int, ref[2], a, b, c, d);
    LOG("req_fav_delete ret=0x%08X", r);
    return r;
}
static int h_rwl(int a, int b, int c, int d) {
    int ulen = b ? ((int *)b)[2] : -1;
    char u[60];
    u[0] = '-'; u[1] = 0;
    if (b && str_ok((unsigned)b, (unsigned)((int *)b)[1], ulen)) cpy(u, (const char *)((int *)b)[1], ulen, sizeof u);
    LOG("req_wl_delete anzahl=%d user='%s'", c ? ((int *)c)[1] : -1, u);
    int r = TAI_CONTINUE(int, ref[3], a, b, c, d);
    LOG("req_wl_delete ret=0x%08X", r);
    return r;
}
static int h_rpl(int a, int b, int c, int d) {
    LOG("req_pl_delete anzahl=%d", c ? ((int *)c)[1] : -1);
    int r = TAI_CONTINUE(int, ref[4], a, b, c, d);
    LOG("req_pl_delete ret=0x%08X", r);
    return r;
}
/* 5 HTTP-Versand: r2 = URL */
static int h_send(int a, int b, int url, int d, int e, int f) {
    int r = TAI_CONTINUE(int, ref[5], a, b, url, d, e, f);
    char u[120];
    int i = 0;
    const char *p = (const char *)url;
    for (; p && p[i] && i < 119; i++) u[i] = p[i];
    u[i] = 0;
    LOG("send ret=0x%08X url=%s", r, u);
    return r;
}

static const struct { unsigned addr; void *fn; } HOOKS[N_HOOKS] = {
    {0x812a5696, h_conv}, {0x8129a576, h_del},
    {0x812c30fa, h_rfav}, {0x812c8764, h_rwl}, {0x812c8b52, h_rpl},
    {0x812bc0c6, h_send},
};

int module_start(SceSize args, void *argp);
int _start(SceSize args, void *argp) __attribute__((weak, alias("module_start")));
int module_start(SceSize args, void *argp) {
    (void)args; (void)argp;
    tai_module_info_t ti;
    sceClibMemset(&ti, 0, sizeof ti);
    ti.size = sizeof ti;
    int r = taiGetModuleInfo(TAI_MAIN_MODULE, &ti);
    LOG("--- diag2 (Loeschen) start, modid=0x%08X r=0x%08X", ti.modid, r);
    for (int i = 0; i < N_HOOKS; i++) {
        hid[i] = taiHookFunctionOffset(&ref[i], ti.modid, 0, HOOKS[i].addr - BASE, 1, HOOKS[i].fn);
        LOG("hook %d @0x%08X -> 0x%08X", i, HOOKS[i].addr, hid[i]);
    }
    return SCE_KERNEL_START_SUCCESS;
}

int module_stop(SceSize args, void *argp) {
    (void)args; (void)argp;
    return SCE_KERNEL_STOP_SUCCESS;
}
