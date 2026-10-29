/*
 * vytp_diag - Diagnose-Hooks fuer den Favoriten-/Playlist-Job der YouTube-App
 * (nur zur Fehlersuche, wird vom Lader zusaetzlich geladen, wenn vorhanden).
 * Protokolliert Rueckgabewerte nach ux0:data/vytp_diag.txt
 */
#include <psp2/kernel/modulemgr.h>
#include <psp2/kernel/clib.h>
#include <psp2/io/fcntl.h>
#include <taihen.h>

#define LOGP "ux0:data/vytp_diag.txt"
#define BASE 0x81000000u

static void dlog(const char *fmt, int a, int b, int c) {
    char m[160];
    int n = sceClibSnprintf(m, sizeof m, fmt, a, b, c);
    SceUID fd = sceIoOpen(LOGP, SCE_O_WRONLY | SCE_O_CREAT | SCE_O_APPEND, 0666);
    if (fd < 0) return;
    sceIoWrite(fd, m, n);
    sceIoWrite(fd, "\n", 1);
    sceIoClose(fd);
}

#define N_HOOKS 11
static tai_hook_ref_t ref[N_HOOKS];
static SceUID hid[N_HOOKS];

/* 0 Favoriten-Abruf */
static int h_fetch(int a, int b, int c, int d, int e, int f) {
    int r = TAI_CONTINUE(int, ref[0], a, b, c, d, e, f);
    dlog("fetch_fav ret=0x%08X entries=%d (vec cap %d)", r, d ? ((int *)d)[1] : -1, d ? ((int *)d)[2] : -1);
    return r;
}
/* 1 Ergebnispruefung */
static int h_check(int a, int b, int c, int d) {
    int r = TAI_CONTINUE(int, ref[1], a, b, c, d);
    if (a) dlog("check(0x%08X,%d) ret=0x%08X", a, b, r);
    return r;
}
/* 2 Eintraege -> Objekte */
static int h_conv(int a, int b, int c, int d) {
    int r = TAI_CONTINUE(int, ref[2], a, b, c, d);
    dlog("conv in=%d out=%d %d", a ? ((int *)a)[1] : -1, c ? ((int *)c)[1] : -1, 0);
    return r;
}
/* 3 Entscheidung vor Batch */
static int h_dec(int a, int b, int c, int d, int e, int f) {
    int r = TAI_CONTINUE(int, ref[3], a, b, c, d, e, f);
    dlog("decide ret=0x%08X %d %d", r, 0, 0);
    return r;
}
/* 4 ID-Sammler */
static int h_coll(int a, int b, int c, int d) {
    int r = TAI_CONTINUE(int, ref[4], a, b, c, d);
    dlog("collect ret=0x%08X items=%d %d", r, b ? ((int *)b)[1] : -1, 0);
    return r;
}
/* 5 Batch-Anfrage */
static int h_batch(int a, int b, int c, int d) {
    int r = TAI_CONTINUE(int, ref[5], a, b, c, d);
    dlog("videos/batch ret=0x%08X %d %d", r, 0, 0);
    return r;
}
/* 6 ID-Getter: out-String {ptr,len} */
static int g_getcnt;
static int h_getid(int out, int obj, int c, int d) {
    int r = TAI_CONTINUE(int, ref[6], out, obj, c, d);
    if (g_getcnt++ < 6 && out) {
        const char *p = *(const char **)out;
        int len = ((int *)out)[1];
        char m[80];
        int i = 0;
        for (; i < len && i < 60 && p; i++) m[i] = p[i];
        m[i] = 0;
        char line[120];
        sceClibSnprintf(line, sizeof line, "getid len=%d '%s'", len, m);
        dlog("%s", (int)line, 0, 0);
    }
    return r;
}
/* 7 nach Batch weiter */
static int h_after(int a, int b, int c, int d) {
    int r = TAI_CONTINUE(int, ref[7], a, b, c, d);
    dlog("after_batch ret=0x%08X %d %d", r, 0, 0);
    return r;
}

/* 8 Vorpruefung (z. B. Netz/Login) */
static int h_pre(int a, int b, int c, int d) {
    int r = TAI_CONTINUE(int, ref[8], a, b, c, d);
    if (r) dlog("precheck ret=0x%08X", r, 0, 0);
    return r;
}
/* 9 HTTP-Versand: r2 = URL (char*) */
static int h_send(int a, int b, int url, int d, int e, int f) {
    int r = TAI_CONTINUE(int, ref[9], a, b, url, d, e, f);
    char line[150];
    char u[100];
    int i = 0;
    const char *p = (const char *)url;
    for (; p && p[i] && i < 99; i++) u[i] = p[i];
    u[i] = 0;
    sceClibSnprintf(line, sizeof line, "send ret=0x%08X url=%s", r, u);
    dlog("%s", (int)line, 0, 0);
    return r;
}
/* 10 Parser */
static int h_parse(int a, int b, int c, int d) {
    int r = TAI_CONTINUE(int, ref[10], a, b, c, d);
    if (r) dlog("parse ret=0x%08X", r, 0, 0);
    return r;
}

static const struct { unsigned addr; void *fn; } HOOKS[N_HOOKS] = {
    {0x812c3c5e, h_fetch}, {0x812a445e, h_check}, {0x812a5696, h_conv}, {0x812a554e, h_dec},
    {0x812a5198, h_coll},  {0x812c2032, h_batch}, {0x812b289a, h_getid}, {0x812a5094, h_after},
    {0x812b74d0, h_pre},   {0x812bc0c6, h_send},  {0x812b8d86, h_parse},
};

int module_start(SceSize args, void *argp);
int _start(SceSize args, void *argp) __attribute__((weak, alias("module_start")));
int module_start(SceSize args, void *argp) {
    (void)args; (void)argp;
    tai_module_info_t ti;
    sceClibMemset(&ti, 0, sizeof ti);
    ti.size = sizeof ti;
    int r = taiGetModuleInfo(TAI_MAIN_MODULE, &ti);
    dlog("--- diag start, main modid=0x%08X r=0x%08X %d", ti.modid, r, 0);
    for (int i = 0; i < N_HOOKS; i++) {
        hid[i] = taiHookFunctionOffset(&ref[i], ti.modid, 0, HOOKS[i].addr - BASE, 1, HOOKS[i].fn);
        dlog("hook %d @0x%08X -> 0x%08X", i, (int)HOOKS[i].addr, hid[i]);
    }
    return SCE_KERNEL_START_SUCCESS;
}

int module_stop(SceSize args, void *argp) {
    (void)args; (void)argp;
    return SCE_KERNEL_STOP_SUCCESS;
}
