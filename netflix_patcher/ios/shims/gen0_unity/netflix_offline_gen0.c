/*
 * iOS unlock shim for the gen-0 NGP SDK (NGP.framework, ngp_* C ABI, event-driven auth).
 *
 * Auth is event-based (like Android gen-0): the game registers an event dispatcher, calls
 * ngp_check_user_authentication(), and the SDK answers with an onUserStateChange event whose
 * NetflixSdkState carries the signed-in profile. We capture the dispatcher and fire a
 * synthetic authenticated event. Progress on these titles lives only in the cloud slot
 * store, so we replace the dead slot server with a local file store keyed under the app's
 * Documents dir (read-miss returns ErrorUnknownSlotId so the game starts fresh, then saves).
 *
 * JSON formats recovered from the IL2CPP dump + the SDK string table.
 */

typedef void (*cb_t)(const char *);

/* libSystem (declared; resolved at runtime via flat lookup). Darwin open() flag values. */
extern char *getenv(const char *);
extern int   snprintf(char *, unsigned long, const char *, ...);
extern int   open(const char *, int, ...);
extern long  read(int, void *, unsigned long);
extern long  write(int, const void *, unsigned long);
extern int   close(int);
extern int   mkdir(const char *, unsigned short);
extern int   unlink(const char *);
extern unsigned long strlen(const char *);
#define O_RDONLY 0x0000
#define O_WRONLY 0x0001
#define O_CREAT  0x0200
#define O_TRUNC  0x0400

static cb_t g_event_cb = 0;
static cb_t g_cloud_cb = 0;

/* onUserStateChange with a signed-in profile. eventMsg is a JSON string (escaped inside). */
static const char AUTH_EVENT[] =
  "{\"eventId\":\"onUserStateChange\",\"eventMsg\":\""
  "{\\\"currentProfile\\\":{\\\"gamerProfileId\\\":\\\"offline-player\\\",\\\"loggingId\\\":\\\"offline\\\","
  "\\\"netflixAccessToken\\\":\\\"offline\\\",\\\"locale\\\":{\\\"language\\\":\\\"en\\\",\\\"country\\\":\\\"US\\\","
  "\\\"variant\\\":\\\"\\\"},\\\"playerId\\\":\\\"offline-player\\\"},\\\"previousProfile\\\":null}"
  "\"}";

static const char CUR_PLAYER[] = "{\"playerId\":\"offline-player\",\"handle\":\"Offline\"}";

/* ---- small helpers (no libc string.h; keep it self-contained) ---- */
static char *scpy(char *p, const char *s) { while (*s) *p++ = *s++; return p; }
static char *sint(char *p, int v) {
    char t[12]; int i = 0; unsigned n = (v < 0) ? (*p++ = '-', (unsigned)(-(long)v)) : (unsigned)v;
    if (!n) { *p++ = '0'; return p; }
    while (n) { t[i++] = (char)('0' + n % 10); n /= 10; }
    while (i) *p++ = t[--i];
    return p;
}
/* slotId -> filesystem-safe (replace anything non-alnum with '_') */
static void safe_id(const char *id, char *out, int cap) {
    int i = 0;
    for (; id && id[i] && i < cap - 1; i++) {
        char c = id[i];
        out[i] = ((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || (c >= '0' && c <= '9')) ? c : '_';
    }
    out[i] = 0;
}
static void slot_dir(char *out, int cap) {
    const char *h = getenv("HOME");
    snprintf(out, cap, "%s/Documents/nfx_slots", h ? h : "/tmp");
}
static void slot_path(char *out, int cap, const char *id) {
    char s[128]; safe_id(id, s, sizeof s);
    const char *h = getenv("HOME");
    snprintf(out, cap, "%s/Documents/nfx_slots/%s.b64", h ? h : "/tmp", s);
}

static const char B64[] = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
static int b64enc(const unsigned char *in, int len, char *out) {
    int o = 0, i = 0;
    for (; i + 2 < len; i += 3) {
        out[o++] = B64[in[i] >> 2];
        out[o++] = B64[((in[i] & 3) << 4) | (in[i + 1] >> 4)];
        out[o++] = B64[((in[i + 1] & 15) << 2) | (in[i + 2] >> 6)];
        out[o++] = B64[in[i + 2] & 63];
    }
    if (len - i == 1) {
        out[o++] = B64[in[i] >> 2];
        out[o++] = B64[(in[i] & 3) << 4];
        out[o++] = '='; out[o++] = '=';
    } else if (len - i == 2) {
        out[o++] = B64[in[i] >> 2];
        out[o++] = B64[((in[i] & 3) << 4) | (in[i + 1] >> 4)];
        out[o++] = B64[(in[i + 1] & 15) << 2];
        out[o++] = '=';
    }
    out[o] = 0;
    return o;
}

/* dispatch a cloud-save result envelope: {identifier,type,result:{status,...,data,slotIds}} */
static void cloud_status(int tracker, const char *type, int status) {
    char buf[256], *p = buf;
    if (!g_cloud_cb) return;
    p = scpy(p, "{\"identifier\":"); p = sint(p, tracker);
    p = scpy(p, ",\"type\":\""); p = scpy(p, type);
    p = scpy(p, "\",\"result\":{\"status\":"); p = sint(p, status);
    p = scpy(p, "}}"); *p = 0;
    g_cloud_cb(buf);
}

/* ---- replacements ---- */
static void nfx0_set_event_dispatcher(cb_t cb)       { g_event_cb = cb; }
static void nfx0_set_cloud_save_dispatcher(cb_t cb)  { g_cloud_cb = cb; }
static void nfx0_check_user_authentication(void)     { if (g_event_cb) g_event_cb(AUTH_EVENT); }
static void nfx0_current_player_identity(cb_t cb)    { if (cb) cb(CUR_PLAYER); }

static void nfx0_get_slot_ids(int tracker) {
    /* enumerate via a manifest file we maintain on save/delete */
    char idx[512]; slot_dir(idx, sizeof idx);
    char *e = idx + strlen(idx); scpy(e, "/index");
    int fd = open(idx, O_RDONLY);
    char ids[4096]; long n = (fd >= 0) ? read(fd, ids, sizeof ids - 1) : -1;
    if (fd >= 0) close(fd);
    char buf[4608], *p = buf;
    p = scpy(p, "{\"identifier\":"); p = sint(p, tracker);
    p = scpy(p, ",\"type\":\"getSlotIds\",\"result\":{\"status\":0,\"slotIds\":[");
    int first = 1;
    for (long i = 0; n > 0 && i < n; ) {
        long j = i; while (j < n && ids[j] != '\n') j++;
        if (j > i) {
            if (!first) *p++ = ',';
            *p++ = '"';
            for (long k = i; k < j; k++) *p++ = ids[k];
            *p++ = '"'; first = 0;
        }
        i = j + 1;
    }
    p = scpy(p, "]}}"); *p = 0;
    if (g_cloud_cb) g_cloud_cb(buf);
}

static void nfx0_read_slot(int tracker, const char *slotId) {
    char path[600]; slot_path(path, sizeof path, slotId);
    int fd = open(path, O_RDONLY);
    if (fd < 0) { cloud_status(tracker, "readSlot", 1001); return; }  /* ErrorUnknownSlotId */
    char data[262144]; long n = read(fd, data, sizeof data - 1); close(fd);
    if (n < 0) n = 0; data[n] = 0;
    char buf[262400], *p = buf;
    p = scpy(p, "{\"identifier\":"); p = sint(p, tracker);
    p = scpy(p, ",\"type\":\"readSlot\",\"result\":{\"status\":0,\"data\":\"");
    p = scpy(p, data);
    p = scpy(p, "\"}}"); *p = 0;
    if (g_cloud_cb) g_cloud_cb(buf);
}

static void nfx0_save_slot(int tracker, const char *slotId, const unsigned char *data, int len) {
    char dir[512]; slot_dir(dir, sizeof dir); mkdir(dir, 0755);
    char path[600]; slot_path(path, sizeof path, slotId);
    int fd = open(path, O_WRONLY | O_CREAT | O_TRUNC, 0644);
    if (fd >= 0) {
        static char b64[349536];  /* 4/3 * 256KiB + slack */
        int m = b64enc(data ? data : (const unsigned char *)"", len > 0 ? len : 0, b64);
        write(fd, b64, m); close(fd);
        /* append slotId to the manifest if new */
        char idx[520]; char *e = idx; e = scpy(e, dir); e = scpy(e, "/index");
        char sid[128]; safe_id(slotId, sid, sizeof sid);
        int rfd = open(idx, O_RDONLY); char cur[4096]; long cn = (rfd >= 0) ? read(rfd, cur, sizeof cur - 1) : 0;
        if (rfd >= 0) close(rfd); if (cn < 0) cn = 0; cur[cn] = 0;
        int seen = 0;
        for (long i = 0; i < cn; ) { long j = i; while (j < cn && cur[j] != '\n') j++;
            if (j - i == (long)strlen(sid)) { int eq = 1; for (long k = 0; k < j - i; k++) if (cur[i + k] != sid[k]) { eq = 0; break; } if (eq) seen = 1; }
            i = j + 1; }
        if (!seen) { int afd = open(idx, O_WRONLY | O_CREAT, 0644);
            if (afd >= 0) { write(afd, cur, cn); write(afd, sid, strlen(sid)); write(afd, "\n", 1); close(afd); } }
    }
    cloud_status(tracker, "saveSlot", 0);
}

static void nfx0_delete_slot(int tracker, const char *slotId) {
    char path[600]; slot_path(path, sizeof path, slotId); unlink(path);
    cloud_status(tracker, "deleteSlot", 0);
}
static void nfx0_resolve_conflict(int tracker, const char *slotId, int resolution) {
    (void)slotId; (void)resolution;
    cloud_status(tracker, "resolveConflict", 0);
}

/* ---- interpose table (weak_import: fits any gen-0 NGP title) ---- */
#define NGP_IMPORT __attribute__((weak_import))
extern NGP_IMPORT void _ngp_set_event_dispatcher(cb_t);
extern NGP_IMPORT void _ngp_set_cloud_save_dispatcher(cb_t);
extern NGP_IMPORT void ngp_check_user_authentication(void);
extern NGP_IMPORT void ngp_current_player_identity(cb_t);
extern NGP_IMPORT void ngp_get_slot_ids(int);
extern NGP_IMPORT void ngp_read_slot(int, const char *);
extern NGP_IMPORT void ngp_save_slot(int, const char *, const void *, int);
extern NGP_IMPORT void ngp_delete_slot(int, const char *);
extern NGP_IMPORT void ngp_resolve_conflict(int, const char *, int);

#define INTERPOSE(newf, oldf) \
    __attribute__((used)) static struct { const void *r; const void *o; } \
    _ip_##oldf __attribute__((section("__DATA,__interpose"))) = \
        { (const void *)(newf), (const void *)(oldf) }

INTERPOSE(nfx0_set_event_dispatcher,      _ngp_set_event_dispatcher);
INTERPOSE(nfx0_set_cloud_save_dispatcher, _ngp_set_cloud_save_dispatcher);
INTERPOSE(nfx0_check_user_authentication, ngp_check_user_authentication);
INTERPOSE(nfx0_current_player_identity,   ngp_current_player_identity);
INTERPOSE(nfx0_get_slot_ids,              ngp_get_slot_ids);
INTERPOSE(nfx0_read_slot,                 ngp_read_slot);
INTERPOSE(nfx0_save_slot,                 ngp_save_slot);
INTERPOSE(nfx0_delete_slot,               ngp_delete_slot);
INTERPOSE(nfx0_resolve_conflict,          ngp_resolve_conflict);
