/*
 * iOS unlock shim for the gen-2 com.netflix.games SDK via its Swift @objc API
 * (NetflixGames.framework), used by GameMaker titles. We swizzle three provider objects the game
 * drives on the SDK singleton:
 *   accessAPI     (AccessProvider)    - grant player access. Two models across SDK versions:
 *                   completion (older builds) requestPlayerAccessWithCompletionHandler:, and event
 *                   (newer builds) registerEventHandler: + showNetflixAccessUIIfNecessary -> we push
 *                   a synthetic granted onPlayerAccessChangeWithAccessEvent: (eventType 1 = nil->present).
 *   profilesAPI   (NGPProfilesProvider) - currentProfileAndReturnError: -> a synthetic offline profile.
 *   cloudSavesAPI (CloudSavesProvider)  - blob store over a local file store; read-miss returns
 *                   blobNameNotFound (-1001) so the game starts fresh instead of decoding an empty blob.
 *
 * Two Swift-framework hazards drive the structure below: (1) touching Swift @objc classes before
 * NetflixGames' initializers run crashes at launch, so the shim declares a load dependency on it
 * (BUILD.md) and defers all SDK-touching work to lazy_init(); the constructor only swizzles.
 * (2) The SDK's Swift model classes have trapping bare inits and Swift-value-type properties, so we
 * hand the game duck-typed synthetic objects (never construct/ivar-write a Swift SDK class).
 */
#include "../nfx_objc_common.h"

#define ACCESS_CLS  "_TtC12NetflixGames14AccessProvider"
#define CLOUD_CLS   "_TtC12NetflixGames18CloudSavesProvider"
#define PROFILE_CLS "NGPProfilesProvider"
#define CHECKER_CLS "NGPAccessChecker"

static long g_blob_not_found = -1001;

/* long-lived synthetic objects (built at load) */
static id g_access_info = 0;    /* NfxAccessInfo(playerID="offline-player"), duck-typed */
static id g_profile = 0;        /* NfxOfflineProfile, duck-typed */
static id g_event = 0;          /* NfxOfflineAccessEvent (current=g_access_info, previous=nil, eventType=1) */
static id g_access_handler = 0; /* the game's NfxListener, captured at registerEventHandler: */
static int g_access_pending = 0;

static char k_blob, k_cont;   /* associated-object keys for per-read blob data */

/* ---- synthetic-object method IMPs ---- */
static id imp_ret_player(id s, SEL c)  { (void)s;(void)c; return nsstr("offline-player"); }
static id imp_ret_offline(id s, SEL c) { (void)s;(void)c; return nsstr("Offline"); }
static id imp_ret_token(id s, SEL c)   { (void)s;(void)c; return nsstr("offline"); }
static id imp_ret_en(id s, SEL c)      { (void)s;(void)c; return nsstr("en"); }
static id imp_ret_us(id s, SEL c)      { (void)s;(void)c; return nsstr("US"); }
static id imp_ret_nil(id s, SEL c)     { (void)s;(void)c; return (id)0; }
static id imp_event_current(id s, SEL c)  { (void)s;(void)c; return g_access_info; }
static id imp_event_previous(id s, SEL c) { (void)s;(void)c; return (id)0; }
static long imp_event_type(id s, SEL c)   { (void)s;(void)c; return 1; }  /* nil->present == granted */
static id imp_blob(id s, SEL c)           { (void)c; return objc_getAssociatedObject(s, &k_blob); }  /* NSData */
static id imp_container(id s, SEL c)      { (void)c; return objc_getAssociatedObject(s, &k_cont); }  /* NfxBlobContainer */

/* built lazily on first use, never in the constructor (see the header); idempotent */
static void build_synthetics(void);
static int g_inited = 0;
static void lazy_init(void) {
    if (g_inited) return;
    g_inited = 1;
    if (CLS("NetflixBlobStoreErrorCode"))
        g_blob_not_found = msgL(CLS("NetflixBlobStoreErrorCode"), "blobNameNotFound");
    build_synthetics();
}

static void build_synthetics(void) {
    /* offline PlayerAccessInfo */
    Class ai = make_class("NfxAccessInfo");
    if (ai) {
        add_method(ai, "playerID", (IMP)imp_ret_player, "@@:");
        g_access_info = keep(msg0(msg0((id)ai, "alloc"), "init"));
    }
    /* blob container + read result (per-read instances carry the data via associated objects) */
    Class bc = make_class("NfxBlobContainer");
    if (bc) {
        add_method(bc, "blob", (IMP)imp_blob, "@@:");
        add_method(bc, "serverSyncTimestamp", (IMP)imp_ret_nil, "@@:");
    }
    Class rr = make_class("NfxReadResult");
    if (rr) {
        add_method(rr, "blobContainer", (IMP)imp_container, "@@:");
        add_method(rr, "conflict", (IMP)imp_ret_nil, "@@:");
    }
    /* offline profile: answers every profile getter the glue reads */
    Class p = make_class("NfxOfflineProfile");
    if (p) {
        add_method(p, "playerID",           (IMP)imp_ret_player,  "@@:");
        add_method(p, "gamerProfileId",     (IMP)imp_ret_player,  "@@:");
        add_method(p, "id",                 (IMP)imp_ret_player,  "@@:");
        add_method(p, "guid",               (IMP)imp_ret_player,  "@@:");
        add_method(p, "gamerId",            (IMP)imp_ret_player,  "@@:");
        add_method(p, "loggingId",          (IMP)imp_ret_player,  "@@:");
        add_method(p, "handle",             (IMP)imp_ret_offline, "@@:");
        add_method(p, "name",               (IMP)imp_ret_offline, "@@:");
        add_method(p, "gamerHandle",        (IMP)imp_ret_offline, "@@:");
        add_method(p, "publicHandle",       (IMP)imp_ret_offline, "@@:");
        add_method(p, "preferredLanguage",  (IMP)imp_ret_en,      "@@:");
        add_method(p, "language",           (IMP)imp_ret_en,      "@@:");
        add_method(p, "primaryLanguage",    (IMP)imp_ret_en,      "@@:");
        add_method(p, "country",            (IMP)imp_ret_us,      "@@:");
        add_method(p, "netflixAccessToken", (IMP)imp_ret_token,   "@@:");
        add_method(p, "gamerAccessToken",   (IMP)imp_ret_token,   "@@:");
        g_profile = keep(msg0(msg0((id)p, "alloc"), "init"));
    }
    /* granted access event (glue reads current/previous/eventType) */
    Class e = make_class("NfxOfflineAccessEvent");
    if (e) {
        add_method(e, "current",   (IMP)imp_event_current,  "@@:");
        add_method(e, "previous",  (IMP)imp_event_previous, "@@:");
        add_method(e, "eventType", (IMP)imp_event_type,     "q@:");
        g_event = keep(msg0(msg0((id)e, "alloc"), "init"));
    }
}

/* deliver the granted access event to the captured handler (main queue). */
static void deliver_access_worker(void *u) {
    (void)u;
    if (!g_access_handler || !g_event) { nfx_log("deliver_access: no handler/event\n"); return; }
    nfx_log("deliver_access: [handler onPlayerAccessChangeWithAccessEvent: granted]\n");
    ((void(*)(id, SEL, id))objc_msgSend)(g_access_handler, SL("onPlayerAccessChangeWithAccessEvent:"), g_event);
}
static void deliver_access_async(void) { dispatch_async_f((void *)&_dispatch_main_q, 0, deliver_access_worker); }

/* ---- blob store (async on the main queue) ---- */
typedef struct { int op; void *block; id name; id data; } gctx;
enum { OP_ACCESS_CB = 1, OP_AUTHCODE, OP_READ, OP_LIST, OP_DELETE, OP_RESOLVE };

static void gen2_worker(void *p) {
    gctx *c = (gctx *)p;
    switch (c->op) {
    case OP_ACCESS_CB:   /* older completion path: hand back the granted PlayerAccessInfo */
        nfx_log("requestPlayerAccess completion -> granted\n");
        inv_block_2(c->block, g_access_info, (id)0);
        break;
    case OP_AUTHCODE:
        nfx_log("playerAuthorizationCode -> offline\n");
        inv_block_2(c->block, nsstr("offline"), (id)0);
        break;
    case OP_READ: {
        id data = nfx_store_read(c->name);
        if (data) {   /* synthetic result -> container -> NSData */
            id cont = keep(msg0(msg0((id)CLS("NfxBlobContainer"), "alloc"), "init"));
            objc_setAssociatedObject(cont, &k_blob, data, NFX_ASSOC_RETAIN);
            id res = keep(msg0(msg0((id)CLS("NfxReadResult"), "alloc"), "init"));
            objc_setAssociatedObject(res, &k_cont, cont, NFX_ASSOC_RETAIN);
            nfx_log("readPlayerBlob HIT\n");
            inv_block_2(c->block, res, (id)0);
        } else {
            nfx_log("readPlayerBlob MISS -> blobNameNotFound\n");
            inv_block_2(c->block, (id)0, nserror("NetflixGamesError", g_blob_not_found));
        }
        break;
    }
    case OP_LIST:
        nfx_log("playerBlobs list\n");
        inv_block_2(c->block, nfx_store_list(), (id)0);
        break;
    case OP_DELETE:
        nfx_store_delete(c->name);
        nfx_log("deletePlayerBlob\n");
        inv_block_2(c->block, (id)0, (id)0);   /* nil result; glue only checks error==nil */
        break;
    case OP_RESOLVE:
        nfx_log("resolveConflict -> ok\n");
        inv_block_1(c->block, (id)0);
        break;
    }
}
/* name goes in c->name (OP_READ/OP_DELETE read it); c->data is unused here */
static void gen2_dispatch(int op, void *block, id name) {
    gctx *c = (gctx *)malloc(sizeof(gctx));
    c->op = op; c->block = block ? _Block_copy(block) : 0; c->name = name ? keep(name) : 0; c->data = 0;
    dispatch_async_f((void *)&_dispatch_main_q, c, gen2_worker);
}
/* write completion only; the file was already written synchronously in my_writeBlob */
typedef struct { void *block; } wctx;
static void gen2_write_worker(void *p) {
    wctx *c = (wctx *)p;
    inv_block_2(c->block, (id)0, (id)0);
}

/* ---- swizzled AccessProvider methods ---- */
static IMP orig_register = 0;
static void my_registerEventHandler(id self, SEL _cmd, id handler) {
    lazy_init();
    g_access_handler = keep(handler);
    nfx_log("registerEventHandler: captured\n");
    if (orig_register) ((void(*)(id, SEL, id))orig_register)(self, _cmd, handler);
    if (g_access_pending) { g_access_pending = 0; deliver_access_async(); }
}
static void my_showAccessUi(id self, SEL _cmd) {
    (void)self; (void)_cmd;
    lazy_init();
    nfx_log("showNetflixAccessUIIfNecessary -> grant via event\n");
    if (g_access_handler) deliver_access_async();
    else g_access_pending = 1;
}
static void my_showAccessBtn(id self, SEL _cmd)     { (void)self;(void)_cmd; nfx_log("showNetflixAccessButton (no-op)\n"); }
static void my_requestAccess(id self, SEL _cmd, void *block) { (void)self;(void)_cmd; lazy_init(); gen2_dispatch(OP_ACCESS_CB, block, 0); }
static void my_authCode(id self, SEL _cmd, void *block)      { (void)self;(void)_cmd; lazy_init(); gen2_dispatch(OP_AUTHCODE, block, 0); }
/* NGPAccessChecker: the check itself just needs to complete (grant arrives via the event). */
static void my_checkAccess(id self, SEL _cmd, void *block) {
    (void)self; (void)_cmd; lazy_init(); nfx_log("checkAccess -> complete\n");
    if (block) { void *b = _Block_copy(block); ((void(*)(void *))((struct nfx_block *)b)->invoke)(b); }
}

/* ---- swizzled profile provider ---- */
static id my_currentProfile(id self, SEL _cmd, id *errp) {
    (void)self; (void)_cmd;
    lazy_init();
    if (errp) *errp = 0;
    nfx_log("currentProfileAndReturnError: -> offline profile\n");
    return g_profile;
}

/* ---- swizzled CloudSavesProvider methods ---- */
static void my_readBlob(id self, SEL _cmd, id name, void *block)  { (void)self;(void)_cmd; lazy_init(); gen2_dispatch(OP_READ, block, name); }
static void my_listBlobs(id self, SEL _cmd, void *block)          { (void)self;(void)_cmd; lazy_init(); gen2_dispatch(OP_LIST, block, 0); }
static void my_deleteBlob(id self, SEL _cmd, id name, void *block){ (void)self;(void)_cmd; lazy_init(); gen2_dispatch(OP_DELETE, block, name); }
static void my_resolve(id self, SEL _cmd, id name, long res, void *block) { (void)self;(void)_cmd;(void)name;(void)res; lazy_init(); gen2_dispatch(OP_RESOLVE, block, 0); }
static void my_writeBlob(id self, SEL _cmd, id name, id container, void *block) {
    (void)self; (void)_cmd;
    lazy_init();
    id data = container ? msg0(container, "blob") : 0;
    /* write synchronously (durable across a save-on-quit); only the completion is deferred */
    int ok = (name && data) ? nfx_store_write(name, data) : 0;
    nfx_log(ok ? "writePlayerBlob wrote (sync)\n" : "writePlayerBlob: no name/data\n");
    wctx *c = (wctx *)malloc(sizeof(wctx));
    c->block = block ? _Block_copy(block) : 0;
    dispatch_async_f((void *)&_dispatch_main_q, c, gen2_write_worker);
}

__attribute__((constructor))
static void nfx_gen2_objc_load(void) {
    nfx_ensure_store();
    nfx_log("==== gen-2 Obj-C/Swift (NetflixGames) shim loaded ====\n");
    /* swizzle only; no Swift SDK calls here (see the header) - synthetics build in lazy_init() */
    if (CLS(ACCESS_CLS)) {
        orig_register = swizzle_instance_method(ACCESS_CLS, "registerEventHandler:", (IMP)my_registerEventHandler);
        swizzle_instance_method(ACCESS_CLS, "showNetflixAccessUIIfNecessary", (IMP)my_showAccessUi);
        swizzle_instance_method(ACCESS_CLS, "showNetflixAccessButton", (IMP)my_showAccessBtn);
        swizzle_instance_method(ACCESS_CLS, "playerAuthorizationCodeWithCompletionHandler:", (IMP)my_authCode);
        /* older SDK builds also expose the completion path; a title without it just no-ops here. */
        swizzle_instance_method(ACCESS_CLS, "requestPlayerAccessWithCompletionHandler:", (IMP)my_requestAccess);
        nfx_log("AccessProvider swizzles installed\n");
    }
    if (CLS(CHECKER_CLS))
        swizzle_instance_method(CHECKER_CLS, "checkAccessWithCompletionHandler:", (IMP)my_checkAccess);
    if (CLS(PROFILE_CLS)) {
        swizzle_instance_method(PROFILE_CLS, "currentProfileAndReturnError:", (IMP)my_currentProfile);
        nfx_log("ProfilesProvider swizzle installed\n");
    }
    if (CLS(CLOUD_CLS)) {
        swizzle_instance_method(CLOUD_CLS, "readPlayerBlobWithName:completionHandler:", (IMP)my_readBlob);
        swizzle_instance_method(CLOUD_CLS, "writePlayerBlobWithName:blobContainer:completionHandler:", (IMP)my_writeBlob);
        swizzle_instance_method(CLOUD_CLS, "deletePlayerBlobWithName:completionHandler:", (IMP)my_deleteBlob);
        swizzle_instance_method(CLOUD_CLS, "playerBlobsWithCompletionHandler:", (IMP)my_listBlobs);
        swizzle_instance_method(CLOUD_CLS, "resolveConflictWithName:resolution:completionHandler:", (IMP)my_resolve);
        nfx_log("CloudSavesProvider swizzles installed\n");
    }
}
