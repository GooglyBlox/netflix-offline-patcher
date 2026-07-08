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

/* IntelliSense parses in MSVC mode and rejects Clang's __attribute__; strip it for the language
 * server only. clang (the real build) never defines __INTELLISENSE__, so this is a no-op there. */
#ifdef __INTELLISENSE__
#define __attribute__(x)
#endif

typedef void (*cb_t)(const char *);        /* (resultMessage) */
typedef void (*cb2_t)(int, const char *);  /* (correlationId, resultMessage) */

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
extern int  *__error(void);            /* Darwin errno location */
extern void  syslog(int, const char *, ...);   /* -> device console (3uTools real-time log) */
extern void *malloc(unsigned long);
extern void  free(void *);
extern int   dup2(int, int);           /* redirect the engine's stdout/stderr to a file */
extern char  _dispatch_main_q;                 /* the main dispatch queue object (take its address) */
extern void  dispatch_async_f(void *queue, void *ctx, void (*work)(void *));
#define errno (*__error())
#define O_RDONLY 0x0000
#define O_WRONLY 0x0001
#define O_APPEND 0x0008
#define O_CREAT  0x0200
#define O_TRUNC  0x0400

static cb_t g_event_cb = 0;
static cb_t g_cloud_cb = 0;

/* ---- diagnostics: append a line to <app>/Documents/nfx_shim.log (falls back to tmp). The
 * user retrieves it from the app sandbox (3uTools / iMazing) to see exactly what the shim
 * did. Harmless: writes only inside the app's own container. ---- */
static void dbg(const char *msg) {
    syslog(5, "NFXSHIM %s", msg);   /* LOG_NOTICE -> device console */
    const char *h = getenv("HOME");
    char path[700];
    snprintf(path, sizeof path, "%s/Documents/nfx_shim.log", h ? h : "/tmp");
    int fd = open(path, O_WRONLY | O_CREAT | O_APPEND, 0644);
    if (fd < 0) {
        snprintf(path, sizeof path, "%s/tmp/nfx_shim.log", h ? h : "/tmp");
        fd = open(path, O_WRONLY | O_CREAT | O_APPEND, 0644);
    }
    if (fd >= 0) { write(fd, msg, strlen(msg)); close(fd); }
}
static void dbg2(const char *tag, const char *a, int n1, int n2) {
    char line[900];
    snprintf(line, sizeof line, "%s id=%s n=%d rc=%d\n", tag, a ? a : "-", n1, n2);
    dbg(line);
}

/* Create <app>/Documents and the slot dir (mkdir won't make parents). */
static void ensure_dirs(void) {
    const char *h = getenv("HOME");
    char p[600];
    snprintf(p, sizeof p, "%s/Documents", h ? h : "/tmp"); mkdir(p, 0755);
    snprintf(p, sizeof p, "%s/Documents/nfx_slots", h ? h : "/tmp"); mkdir(p, 0755);
}

/* Runs when the shim dylib is loaded (proves the injection + interpose landed). Ensures the
 * Documents dir exists so every later log line reaches the file, not the tmp fallback. */
__attribute__((constructor))
static void nfx0_loaded(void) {
    ensure_dirs();
    const char *h = getenv("HOME");
    /* Capture the engine's stdout/stderr (Unity's printf_console -> Debug.Log lands here) so we
     * can see the GAME's own boot log, not just our ngp_ calls. Written to Documents. */
    char sp[700];
    snprintf(sp, sizeof sp, "%s/Documents/nfx_stdio.log", h ? h : "/tmp");
    int sfd = open(sp, O_WRONLY | O_CREAT | O_TRUNC, 0644);
    if (sfd >= 0) { dup2(sfd, 1); dup2(sfd, 2); if (sfd > 2) close(sfd); }
    char l[800];
    snprintf(l, sizeof l, "==== nfx0 shim loaded, HOME=%s ====\n", h ? h : "(null)");
    dbg(l);
}

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

/* Deliver a cloud-save reply ASYNCHRONOUSLY on the main thread. The real SDK answers after a
 * network round-trip, and the engine's async state machine expects that: replying inline would
 * run the whole continuation chain deep inside our call (and it can block on a UI await -> hang).
 * We copy the JSON, hop to the main queue, then invoke the C# dispatcher there. */
typedef struct { cb_t cb; cb2_t cb2; int id; char json[1]; } dctx;
static void deferred_worker(void *p) {
    dctx *c = (dctx *)p;
    { char l[400]; snprintf(l, sizeof l, "deferred_worker FIRED (main thread) json=%.200s\n", c->json); dbg(l); }
    if (c->cb)  c->cb(c->json);
    if (c->cb2) c->cb2(c->id, c->json);
    dbg("deferred_worker returned (reply delivered)\n");
    free(c);
}
static dctx *dctx_new(const char *json) {
    unsigned long n = strlen(json);
    dctx *c = (dctx *)malloc(sizeof(dctx) + n);
    if (!c) return 0;
    c->cb = 0; c->cb2 = 0; c->id = 0;
    char *d = c->json; const char *s = json; while ((*d++ = *s++)) {}
    return c;
}
static void deliver(cb_t cb, const char *json) {
    if (!cb) return;
    dctx *c = dctx_new(json);
    if (!c) { cb(json); return; }
    c->cb = cb;
    dispatch_async_f((void *)&_dispatch_main_q, c, deferred_worker);
}
static void deliver2(cb2_t cb, int id, const char *json) {
    if (!cb) return;
    dctx *c = dctx_new(json);
    if (!c) { cb(id, json); return; }
    c->cb2 = cb; c->id = id;
    dispatch_async_f((void *)&_dispatch_main_q, c, deferred_worker);
}

/* dispatch a cloud-save result envelope: {identifier,type,result:{status,...}}. `type` must be
 * the native result-class name (ReadSlotResult, not readSlot); NGPCloudSaveEvent routes on it and
 * a verb-form type matches nothing, so the reply is silently dropped. Same in 0.12.1 and 0.13.0. */
static void cloud_status(int tracker, const char *type, int status) {
    char buf[256], *p = buf;
    if (!g_cloud_cb) { dbg2("cloud_status NO_CB", type, tracker, status); return; }
    p = scpy(p, "{\"identifier\":"); p = sint(p, tracker);
    p = scpy(p, ",\"type\":\""); p = scpy(p, type);
    p = scpy(p, "\",\"result\":{\"status\":"); p = sint(p, status);
    p = scpy(p, "}}"); *p = 0;
    deliver(g_cloud_cb, buf);
}

/* ---- replacements ---- */
static void nfx0_set_event_dispatcher(cb_t cb) {
    g_event_cb = cb;
    { const char *h = getenv("HOME"); char l[700];
      snprintf(l, sizeof l, "set_event_dispatcher cb=%d HOME=%s\n", cb ? 1 : 0, h ? h : "(null)"); dbg(l); }
}
static void nfx0_set_cloud_save_dispatcher(cb_t cb) { g_cloud_cb = cb; dbg2("set_cloud_dispatcher", 0, cb ? 1 : 0, 0); }
static void nfx0_check_user_authentication(void)    { dbg("check_user_authentication -> auth event"); if (g_event_cb) g_event_cb(AUTH_EVENT); }
static void nfx0_current_player_identity(cb_t cb)   { dbg2("current_player_identity", 0, cb ? 1 : 0, 0); if (cb) cb(CUR_PLAYER); }

static void nfx0_get_slot_ids(int tracker) {
    /* enumerate via a manifest file we maintain on save/delete */
    char idx[512]; slot_dir(idx, sizeof idx);
    char *e = idx + strlen(idx); scpy(e, "/index");
    int fd = open(idx, O_RDONLY);
    char ids[4096]; long n = (fd >= 0) ? read(fd, ids, sizeof ids - 1) : -1;
    if (fd >= 0) close(fd);
    dbg2("get_slot_ids", idx, tracker, (int)n);
    char buf[4608], *p = buf;
    p = scpy(p, "{\"identifier\":"); p = sint(p, tracker);
    p = scpy(p, ",\"type\":\"GetSlotIdsResult\",\"result\":{\"status\":0,\"slotIds\":[");
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
    if (g_cloud_cb) deliver(g_cloud_cb, buf); else dbg("get_slot_ids NO_CB");
}

/* static (not stack): the read buffers are large and iOS secondary-thread stacks are small.
 * Cloud ops are serialized by the game (one at a time, same thread), so this is safe. */
static char g_read_data[262144];
static char g_read_buf[262400];

/* Read-miss reply is per-slot: MAIN returns an empty save (status 0, "" -> empty byte[]); other
 * slots return ErrorUnknownSlotId (1001). Both route to handleReadSlotResult and complete the
 * read Task via SetResult (miss just builds a container with null data), verified on-device. */

/* Delivery is per-slot: MAIN inline so its load continuation resets the backend _currOp to NONE
 * inside the read call (saves gate on that); other slots async on the main queue. */
static int slot_wants_inline(const char *id) {
    return id && id[0] == 'M' && id[1] == 'A' && id[2] == 'I' && id[3] == 'N' && id[4] == 0;  /* "MAIN" */
}
static void deliver_read(const char *slotId, const char *json) {
    if (slot_wants_inline(slotId)) { if (g_cloud_cb) g_cloud_cb(json); }
    else deliver(g_cloud_cb, json);
}

static void nfx0_read_slot(int tracker, const char *slotId) {
    char path[600]; slot_path(path, sizeof path, slotId);
    int fd = open(path, O_RDONLY);
    dbg2("read_slot", slotId, tracker, fd);
    if (fd < 0) {
        /* Read-miss is per-slot: MAIN wants an empty LOADED slot (status 0, "") so the game's
         * save group flips to "loaded"; other slots want ErrorUnknownSlotId (1001), which their
         * read path maps to "no save" and completes the Task (a status-0 there would not). */
        if (slot_wants_inline(slotId) && g_cloud_cb) {   /* MAIN only */
            char buf[160], *p = buf;
            p = scpy(p, "{\"identifier\":"); p = sint(p, tracker);
            p = scpy(p, ",\"type\":\"ReadSlotResult\",\"result\":{\"status\":0,\"data\":\"\"}}"); *p = 0;
            deliver_read(slotId, buf);
        } else {
            /* other slots: read-miss = ErrorUnknownSlotId, delivered async */
            cloud_status(tracker, "ReadSlotResult", 1001);
        }
        return;
    }
    long n = 0, cap = (long)sizeof g_read_data - 1;  /* drain the whole file: read() can return short */
    for (;;) { long r = read(fd, g_read_data + n, cap - n); if (r <= 0) break; n += r; if (n >= cap) break; }
    int truncated = 0; { char probe[1]; if (read(fd, probe, 1) == 1) truncated = 1; }
    close(fd);
    g_read_data[n] = 0;
    { char l[120]; snprintf(l, sizeof l, "read_slot GOT bytes=%ld trunc=%d\n", n, truncated); dbg(l); }
    if (truncated) { cloud_status(tracker, "ReadSlotResult", 1001); return; }  /* too big: no save beats a corrupt blob */
    char *p = g_read_buf;
    p = scpy(p, "{\"identifier\":"); p = sint(p, tracker);
    p = scpy(p, ",\"type\":\"ReadSlotResult\",\"result\":{\"status\":0,\"data\":\"");
    p = scpy(p, g_read_data);
    p = scpy(p, "\"}}"); *p = 0;
    deliver_read(slotId, g_read_buf);
}

static void nfx0_save_slot(int tracker, const char *slotId, const unsigned char *data, int len) {
    char dir[512]; ensure_dirs(); slot_dir(dir, sizeof dir); mkdir(dir, 0755);
    char path[600]; slot_path(path, sizeof path, slotId);
    int fd = open(path, O_WRONLY | O_CREAT | O_TRUNC, 0644);
    { char l[800]; snprintf(l, sizeof l, "save_slot id=%s len=%d path=%s open_fd=%d errno=%d\n",
        slotId ? slotId : "-", len, path, fd, fd < 0 ? errno : 0); dbg(l); }
    if (fd >= 0) {
        static char b64[349536];  /* 4/3 * 256KiB + slack */
        int m = b64enc(data ? data : (const unsigned char *)"", len > 0 ? len : 0, b64);
        long off = 0;  /* loop: a single write() can be short; a truncated blob would fail to decode on read */
        while (off < m) { long w = write(fd, b64 + off, m - off); if (w <= 0) break; off += w; }
        close(fd);
        { char l[160]; snprintf(l, sizeof l, "save_slot WROTE b64_len=%d written=%ld%s\n",
            m, off, off == m ? "" : " SHORT_WRITE!"); dbg(l); }
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
    cloud_status(tracker, "SaveSlotResult", 0);
}

static void nfx0_delete_slot(int tracker, const char *slotId) {
    char path[600]; slot_path(path, sizeof path, slotId); unlink(path);
    cloud_status(tracker, "DeleteSlotResult", 0);
}
static void nfx0_resolve_conflict(int tracker, const char *slotId, int resolution) {
    (void)slotId; (void)resolution;
    cloud_status(tracker, "ResolveConflictResult", 0);
}

/* ---- remaining gen-0 funcs: fire-and-forget ones are no-ops; callback ones are answered
 * gracefully (empty, async) so no boot step awaits a dead-server reply forever. All logged
 * so the next log shows the full call sequence.
 * NOTE: ngp_set_config_dispatcher is deliberately NOT interposed - the real SDK delivers a
 * (bundled/cached) config offline, and some titles gate boot on that config event; capturing the
 * dispatcher ourselves would starve it and hang right after auth. ---- */

static void nfx0_get_player_identities(int tracker, const char *json, cb_t cb) {
    (void)json; dbg2("get_player_identities", 0, tracker, 0);
    char buf[96], *p = buf;
    p = scpy(p, "{\"tracker\":"); p = sint(p, tracker);
    p = scpy(p, ",\"resultStatus\":0,\"description\":\"\",\"identities\":[]}"); *p = 0;
    deliver(cb, buf);
}
static void nfx0_get_achievements(int cid, cb2_t cb)                      { dbg2("get_achievements", 0, cid, 0); deliver2(cb, cid, "{}"); }
static void nfx0_unlock_achievement(int cid, const char *n, cb2_t cb)    { dbg2("unlock_achievement", n, cid, 0); deliver2(cb, cid, "{}"); }
static void nfx0_get_aggregated_stat(int cid, const char *n, cb2_t cb)   { dbg2("get_aggregated_stat", n, cid, 0); deliver2(cb, cid, "{}"); }
static void nfx0_submit_stat_now(int cid, const char *n, long v, cb2_t cb){ (void)v; dbg2("submit_stat_now", n, cid, 0); deliver2(cb, cid, "{}"); }
static void nfx0_lb_top(int cid, const char *n, int m, cb2_t cb)         { (void)m; dbg2("lb_top", n, cid, 0); deliver2(cb, cid, "{}"); }
static void nfx0_lb_current(int cid, const char *n, cb2_t cb)            { dbg2("lb_current", n, cid, 0); deliver2(cb, cid, "{}"); }
static void nfx0_lb_more(int cid, const char *n, int m, const char *c, int d, cb2_t cb) { (void)m; (void)c; (void)d; dbg2("lb_more", n, cid, 0); deliver2(cb, cid, "{}"); }
static void nfx0_lb_around(int cid, const char *n, int m, cb2_t cb)      { (void)m; dbg2("lb_around", n, cid, 0); deliver2(cb, cid, "{}"); }
static void nfx0_lb_info(int cid, const char *n, cb2_t cb)               { dbg2("lb_info", n, cid, 0); deliver2(cb, cid, "{}"); }

/* ---- non-UI fire-and-forget funcs: no-op'd (and logged). These are telemetry / locale /
 * push / deeplink / messaging notifications; offline they have nothing to talk to, and their
 * real impls can block on the dead Netflix backend. Unlike the UI show/hide calls they do not
 * pump the main runloop, so no-opping them is safe and removes a post-auth stall path. The UI
 * calls (show/hide menu+button, achievements panel) and set_config_dispatcher stay REAL. ---- */
static void nfx0_set_locale(const char *a)                   { dbg2("set_locale", a, 0, 0); }
static void nfx0_send_cl_event(const char *a, const char *b) { (void)b; dbg2("send_cl_event", a, 0, 0); }
static void nfx0_submit_stat(const char *a, long v)          { (void)v; dbg2("submit_stat", a, 0, 0); }
static void nfx0_on_push_token(const char *a)                { dbg2("on_push_token", a, 0, 0); }
static void nfx0_on_deeplink(const char *a, int b)           { dbg2("on_deeplink_received", a, b, 0); }
static void nfx0_on_messaging(int a, const char *b)          { dbg2("on_messaging_event", b, a, 0); }
static void nfx0_on_game_state_saved(const char *a)          { dbg2("on_game_state_saved", a, 0, 0); }

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
extern NGP_IMPORT void ngp_get_player_identities(int, const char *, cb_t);
extern NGP_IMPORT void ngp_get_achievements(int, cb2_t);
extern NGP_IMPORT void ngp_unlock_achievement(int, const char *, cb2_t);
extern NGP_IMPORT void ngp_get_aggregated_stat(int, const char *, cb2_t);
extern NGP_IMPORT void ngp_submit_stat_now(int, const char *, long, cb2_t);
extern NGP_IMPORT void ngp_get_top_leaderboard_entries(int, const char *, int, cb2_t);
extern NGP_IMPORT void ngp_get_current_player_entry(int, const char *, cb2_t);
extern NGP_IMPORT void ngp_get_more_leaderboard_entries(int, const char *, int, const char *, int, cb2_t);
extern NGP_IMPORT void ngp_get_entries_around_current_player(int, const char *, int, cb2_t);
extern NGP_IMPORT void ngp_get_leaderboard_info(int, const char *, cb2_t);
extern NGP_IMPORT void ngp_set_locale(const char *);
extern NGP_IMPORT void ngp_send_cl_event(const char *, const char *);
extern NGP_IMPORT void ngp_submit_stat(const char *, long);
extern NGP_IMPORT void ngp_on_push_token(const char *);
extern NGP_IMPORT void ngp_on_deeplink_received(const char *, int);
extern NGP_IMPORT void ngp_on_messaging_event(int, const char *);
extern NGP_IMPORT void ngp_on_game_state_saved(const char *);

#define INTERPOSE(newf, oldf) \
    __attribute__((used)) static struct { const void *r; const void *o; } \
    _ip_##oldf __attribute__((section("__DATA,__interpose"))) = \
        { (const void *)(newf), (const void *)(oldf) }

INTERPOSE(nfx0_get_player_identities,     ngp_get_player_identities);
INTERPOSE(nfx0_get_achievements,          ngp_get_achievements);
INTERPOSE(nfx0_unlock_achievement,        ngp_unlock_achievement);
INTERPOSE(nfx0_get_aggregated_stat,       ngp_get_aggregated_stat);
INTERPOSE(nfx0_submit_stat_now,           ngp_submit_stat_now);
INTERPOSE(nfx0_lb_top,                    ngp_get_top_leaderboard_entries);
INTERPOSE(nfx0_lb_current,                ngp_get_current_player_entry);
INTERPOSE(nfx0_lb_more,                   ngp_get_more_leaderboard_entries);
INTERPOSE(nfx0_lb_around,                 ngp_get_entries_around_current_player);
INTERPOSE(nfx0_lb_info,                   ngp_get_leaderboard_info);
INTERPOSE(nfx0_set_event_dispatcher,      _ngp_set_event_dispatcher);
INTERPOSE(nfx0_set_cloud_save_dispatcher, _ngp_set_cloud_save_dispatcher);
INTERPOSE(nfx0_check_user_authentication, ngp_check_user_authentication);
INTERPOSE(nfx0_current_player_identity,   ngp_current_player_identity);
INTERPOSE(nfx0_get_slot_ids,              ngp_get_slot_ids);
INTERPOSE(nfx0_read_slot,                 ngp_read_slot);
INTERPOSE(nfx0_save_slot,                 ngp_save_slot);
INTERPOSE(nfx0_delete_slot,               ngp_delete_slot);
INTERPOSE(nfx0_resolve_conflict,          ngp_resolve_conflict);
INTERPOSE(nfx0_set_locale,                ngp_set_locale);
INTERPOSE(nfx0_send_cl_event,             ngp_send_cl_event);
INTERPOSE(nfx0_submit_stat,               ngp_submit_stat);
INTERPOSE(nfx0_on_push_token,             ngp_on_push_token);
INTERPOSE(nfx0_on_deeplink,               ngp_on_deeplink_received);
INTERPOSE(nfx0_on_messaging,              ngp_on_messaging_event);
INTERPOSE(nfx0_on_game_state_saved,       ngp_on_game_state_saved);
