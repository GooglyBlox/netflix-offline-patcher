/*
 * iOS unlock shim for the gen-2 com.netflix.games SDK (ngp_* C ABI).
 *
 * The SDK returns every result to its caller as a JSON string over a C callback that
 * is passed as an argument to the ngp_* call. We dyld-interpose the gate functions and
 * answer with a granted / offline result. The engine stores its TaskCompletionSource
 * before making the native call, so answering synchronously is race-free. Self-contained:
 * the only external symbols are the eight interpose targets, which dyld resolves.
 *
 * Wire formats (from the IL2CPP dump + the SDK's string table):
 *   access grant : {"playerAccessInfo":{"playerID":"<id>"},"tracker":<n>,"errorCode":0,"errorMessage":""}
 *   auth code    : {"tracker":<n>,"errorCode":0,"errorMessage":"","playerAuthorizationCode":"offline"}
 *   cloud list   : {"result":{"slotIds":[]},"error":null}
 *   cloud error  : {"result":null,"error":{"code":404,"message":"offline"}}
 */

typedef void (*disp1_t)(const char *);
typedef void (*disp2_t)(int, const char *);

/* Stable across launches: the game keys its local save on a hash of this. */
static const char PLAYER_ID[] = "offline-player";

static char *j_cpy(char *p, const char *s) { while (*s) *p++ = *s++; return p; }

static char *j_int(char *p, int v)
{
    char tmp[12];
    int i = 0;
    unsigned n;
    if (v < 0) { *p++ = '-'; n = (unsigned)(-(long)v); }
    else       { n = (unsigned)v; }
    if (n == 0) { *p++ = '0'; return p; }
    while (n) { tmp[i++] = (char)('0' + (n % 10)); n /= 10; }
    while (i) *p++ = tmp[--i];
    return p;
}

/* --- replacements for the gate functions --- */

/* The login wall. Hand back a granted PlayerAccessInfo for a synthetic offline
 * member instead of the dead Netflix handshake. */
static void nfoff_request_player_access(int tracker, disp1_t disp)
{
    char buf[192], *p = buf;
    if (!disp) return;
    p = j_cpy(p, "{\"playerAccessInfo\":{\"playerID\":\"");
    p = j_cpy(p, PLAYER_ID);
    p = j_cpy(p, "\"},\"tracker\":");
    p = j_int(p, tracker);
    p = j_cpy(p, ",\"errorCode\":0,\"errorMessage\":\"\"}");
    *p = 0;
    disp(buf);
}

/* Some titles fetch a player authorization code after access. Grant it locally. */
static void nfoff_get_player_authorization_code(int tracker, disp1_t disp)
{
    char buf[160], *p = buf;
    if (!disp) return;
    p = j_cpy(p, "{\"tracker\":");
    p = j_int(p, tracker);
    p = j_cpy(p, ",\"errorCode\":0,\"errorMessage\":\"\",\"playerAuthorizationCode\":\"offline\"}");
    *p = 0;
    disp(buf);
}

/* Obsolete access-UI entry (older SDK path). Never present the Netflix login UI. */
static void nfoff_show_access_ui(void) { /* no-op */ }

/* Cloud-save list: no saved blobs, so the game starts from its local save. */
static void nfoff_blob_get_blobs(int cid, disp2_t cb)
{
    if (cb) cb(cid, "{\"result\":{\"slotIds\":[]},\"error\":null}");
}

/* Cloud-save read/write/delete/resolve: no server. Report an offline error so the
 * game falls back to (and keeps using) its local save instead of hanging. */
static void nfoff_blob_read(int cid, const char *name, disp2_t cb)
{
    (void)name;
    if (cb) cb(cid, "{\"result\":null,\"error\":{\"code\":404,\"message\":\"offline\"}}");
}
static void nfoff_blob_write(int cid, const char *name, const void *data, int len, disp2_t cb)
{
    (void)name; (void)data; (void)len;
    if (cb) cb(cid, "{\"result\":null,\"error\":{\"code\":404,\"message\":\"offline\"}}");
}
static void nfoff_blob_delete(int cid, const char *name, disp2_t cb)
{
    (void)name;
    if (cb) cb(cid, "{\"result\":null,\"error\":{\"code\":404,\"message\":\"offline\"}}");
}
static void nfoff_blob_resolve_conflict(int cid, const char *name, int res, disp2_t cb)
{
    (void)name; (void)res;
    if (cb) cb(cid, "{\"result\":null,\"error\":{\"code\":404,\"message\":\"offline\"}}");
}

/* Interpose table. weak_import: a symbol a given title lacks binds to NULL and dyld
 * treats that tuple as a no-op, so one prebuilt dylib fits every gen-2 title. */
#define NGP_IMPORT __attribute__((weak_import))
extern NGP_IMPORT void ngp_request_player_access(int, disp1_t);
extern NGP_IMPORT void ngp_access_get_player_authorization_code(int, disp1_t);
extern NGP_IMPORT void ngp_show_netflix_access_ui_if_necessary(void);
extern NGP_IMPORT void ngp_blob_store_get_blobs(int, disp2_t);
extern NGP_IMPORT void ngp_blob_store_read(int, const char *, disp2_t);
extern NGP_IMPORT void ngp_blob_store_write(int, const char *, const void *, int, disp2_t);
extern NGP_IMPORT void ngp_blob_store_delete(int, const char *, disp2_t);
extern NGP_IMPORT void ngp_blob_store_resolve_conflict(int, const char *, int, disp2_t);

#define INTERPOSE(newf, oldf)                                                  \
    __attribute__((used)) static struct { const void *r; const void *o; }      \
    _interpose_##oldf __attribute__((section("__DATA,__interpose"))) =          \
        { (const void *)(newf), (const void *)(oldf) }

INTERPOSE(nfoff_request_player_access,        ngp_request_player_access);
INTERPOSE(nfoff_get_player_authorization_code, ngp_access_get_player_authorization_code);
INTERPOSE(nfoff_show_access_ui,               ngp_show_netflix_access_ui_if_necessary);
INTERPOSE(nfoff_blob_get_blobs,               ngp_blob_store_get_blobs);
INTERPOSE(nfoff_blob_read,                    ngp_blob_store_read);
INTERPOSE(nfoff_blob_write,                   ngp_blob_store_write);
INTERPOSE(nfoff_blob_delete,                  ngp_blob_store_delete);
INTERPOSE(nfoff_blob_resolve_conflict,        ngp_blob_store_resolve_conflict);
