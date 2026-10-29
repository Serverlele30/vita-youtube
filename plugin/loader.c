/*
 * vytp - Lader fuer das PS-Vita-YouTube-Plugin
 *
 * taiHEN laedt Plugins ganz am Anfang des App-Starts. Zu dem Zeitpunkt ist
 * das Netzwerk-Modul (SceNet) der App oft noch nicht geladen - ein Plugin,
 * das SceNet-Funktionen importiert, wird dann stillschweigend verworfen.
 *
 * Darum importiert dieser Lader KEINE Netzwerk-Funktionen. Er wartet kurz,
 * laedt SceNet selbst nach und startet dann ux0:tai/vytp_net.suprx (Server).
 */
#include <psp2/kernel/modulemgr.h>
#include <psp2/kernel/threadmgr.h>
#include <psp2/kernel/clib.h>
#include <psp2/io/fcntl.h>
#include <psp2/io/stat.h>
#include <psp2/sysmodule.h>
#include <taihen.h>

#define LOG_DIR  "ux0:data/vytp"
#define LOG1     "ux0:data/vytp/log.txt"
#define LOG2     "ux0:data/vytp_log.txt"
#define NET_PATH "ux0:tai/vytp_net.suprx"
#define DIAG_PATH "ux0:tai/vytp_diag.suprx"

static void log_to(const char *path, const char *msg) {
    SceUID fd = sceIoOpen(path, SCE_O_WRONLY | SCE_O_CREAT | SCE_O_APPEND, 0666);
    if (fd < 0) return;
    sceIoWrite(fd, msg, sceClibStrnlen(msg, 256));
    sceIoWrite(fd, "\n", 1);
    sceIoClose(fd);
}

static void log_int(const char *what, int v) {
    char buf[128];
    sceClibSnprintf(buf, sizeof buf, "%s 0x%08X", what, v);
    log_to(LOG1, buf);
    log_to(LOG2, buf);
}

/* Adressen im Speicher der App umschreiben (Login-Browser geht sonst direkt zu Google).
 * Neue Adresse ist kuerzer -> passt an dieselbe Stelle, Rest wird mit 0 aufgefuellt. */
static const struct { const char *from; const char *to; } PATCHES[] = {
    {"https://accounts.google.com/o/oauth2/auth",  "https://ytv.serverlele.de/o/oauth2/auth"},
    {"https://accounts.google.com/o/oauth2/token", "https://ytv.serverlele.de/o/oauth2/token"},
    /* Die App setzt Content-Length selbst, libhttp setzt sie nochmal -> nginx: 400
     * "duplicate header line". Eigene Kopfzeile umbenennen, die automatische bleibt. */
    {"Content-Length", "X-Vita-CLength"},
};

static int patch_strings(void) {
    tai_module_info_t ti;
    sceClibMemset(&ti, 0, sizeof ti);
    ti.size = sizeof ti;
    int r = taiGetModuleInfo(TAI_MAIN_MODULE, &ti);
    if (r < 0) { log_int("patch: taiGetModuleInfo =", r); return r; }

    SceKernelModuleInfo mi;
    sceClibMemset(&mi, 0, sizeof mi);
    mi.size = sizeof mi;
    r = sceKernelGetModuleInfo(ti.modid, &mi);
    if (r < 0) { log_int("patch: sceKernelGetModuleInfo =", r); return r; }

    int done = 0;
    for (int seg = 0; seg < 4; seg++) {
        const char *base = (const char *)mi.segments[seg].vaddr;
        unsigned int size = mi.segments[seg].memsz;
        if (!base || size < 64) continue;
        for (unsigned int p = 0; p + 64 < size; p++) {
            for (unsigned int k = 0; k < sizeof PATCHES / sizeof PATCHES[0]; k++) {
                const char *f = PATCHES[k].from;
                int fl = sceClibStrnlen(f, 80);
                if (base[p] != f[0] || p + fl + 1 > size) continue;
                if (sceClibStrncmp(base + p, f, fl) != 0 || base[p + fl] != 0) continue;
                char buf[96];
                sceClibMemset(buf, 0, sizeof buf);
                int tl = sceClibStrnlen(PATCHES[k].to, 80);
                sceClibMemcpy(buf, PATCHES[k].to, tl);
                SceUID uid = taiInjectData(ti.modid, seg, p, buf, fl + 1);
                char m[128];
                sceClibSnprintf(m, sizeof m, "patch: seg %d +0x%X %s -> 0x%08X", seg, p, PATCHES[k].from, uid);
                log_to(LOG1, m);
                log_to(LOG2, m);
                if (uid >= 0) done++;
            }
        }
    }
    /* Host in allen Anfrage-Adressen: gdata.youtube.com -> ytv.serverlele.de (beide 17 Zeichen).
     * Noetig, weil z. B. videos/batch per unverschluesseltem HTTP an gdata geht und tls_fixer
     * das nicht umleitet. Nur "http(s)://gdata.youtube.com" gefolgt von "/feeds" oder Stringende,
     * NICHT die XML-Namensraeume (.../schemas/2007), sonst kann die App nichts mehr lesen. */
    static const char HOST_OLD[] = "gdata.youtube.com";
    static const char HOST_NEW[] = "ytv.serverlele.de";
    int hosts = 0;
    for (int seg = 0; seg < 4; seg++) {
        const char *base = (const char *)mi.segments[seg].vaddr;
        unsigned int size = mi.segments[seg].memsz;
        if (!base || size < 64) continue;
        for (unsigned int p = 8; p + 40 < size; p++) {
            if (base[p] != 'g' || sceClibStrncmp(base + p, HOST_OLD, 17) != 0) continue;
            int http = sceClibStrncmp(base + p - 7, "http://", 7) == 0;
            int https = p >= 8 && sceClibStrncmp(base + p - 8, "https://", 8) == 0;
            if (!http && !https) continue;
            const char *after = base + p + 17;
            if (!(after[0] == 0 || sceClibStrncmp(after, "/feeds", 6) == 0)) continue;
            if (taiInjectData(ti.modid, seg, p, HOST_NEW, 17) >= 0) hosts++;
        }
    }
    log_int("patch: hosts ersetzt =", hosts);
    log_int("patch: ersetzt =", done);
    return done;
}

static int loader_thread(SceSize args, void *argp) {
    (void)args; (void)argp;
    patch_strings();                          /* sofort, bevor jemand auf "Anmelden" tippt */
    {   /* optionale Diagnose-Hooks */
        SceIoStat st;
        if (sceIoGetstat(DIAG_PATH, &st) >= 0) {
            int ds = 0;
            SceUID d = sceKernelLoadStartModule(DIAG_PATH, 0, NULL, 0, NULL, &ds);
            log_int("loader: vytp_diag laden =", d);
        }
    }
    sceKernelDelayThread(2 * 1000 * 1000);   /* App erst hochfahren lassen */

    int r = sceSysmoduleLoadModule(SCE_SYSMODULE_NET);
    log_int("loader: SceNet laden =", r);
    r = sceSysmoduleLoadModule(SCE_SYSMODULE_HTTPS);   /* bringt SceHttp + SceSsl mit */
    log_int("loader: SceHttps laden =", r);

    int status = 0;
    SceUID uid = sceKernelLoadStartModule(NET_PATH, 0, NULL, 0, NULL, &status);
    log_int("loader: vytp_net laden =", uid);
    log_int("loader: vytp_net status =", status);
    return sceKernelExitDeleteThread(0);
}

int module_start(SceSize args, void *argp);
int _start(SceSize args, void *argp) __attribute__((weak, alias("module_start")));
int module_start(SceSize args, void *argp) {
    (void)args; (void)argp;
    sceIoMkdir(LOG_DIR, 0777);
    sceIoRemove(LOG1);   /* Log bei jedem App-Start neu, waechst nicht endlos auf der SD */
    sceIoRemove(LOG2);
    log_int("--- vytp loader gestartet, Version", 0x0320);
    SceUID t = sceKernelCreateThread("vytp_loader", loader_thread, 0x10000100, 0x2000, 0, 0, NULL);
    if (t >= 0)
        sceKernelStartThread(t, 0, NULL);
    else
        log_int("loader: thread fehlgeschlagen", t);
    return SCE_KERNEL_START_SUCCESS;
}

int module_stop(SceSize args, void *argp) {
    (void)args; (void)argp;
    return SCE_KERNEL_STOP_SUCCESS;
}
