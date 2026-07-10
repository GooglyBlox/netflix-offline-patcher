/*
 * iOS unlock shim for the gen-0 NGP SDK via its Obj-C facade (NGP.framework, class NetflixSDK),
 * used by Unreal titles. The game registers an event receiver and calls checkUserAuth; the SDK
 * answers over the network with [receiver onUserStateChange: state]. We swizzle the NetflixSDK
 * class methods: capture the receiver, and on checkUserAuth deliver a synthetic signed-in
 * NetflixSDKState(NetflixProfile) on the main queue. Cloud save (slot API) is backed by a local
 * file store (read-miss -> ErrorUnknownSlotId); such titles are often auth-only (engine-local saves).
 */
#include "../nfx_objc_common.h"

#define NFX_UNKNOWN_SLOT 1001   /* gen-0 read-miss code (Android ERROR_UNKNOWN_SLOT_ID) */

static id g_receiver = 0;       /* the game's NetflixSDKEventHandler (kept) */
static int g_auth_pending = 0;  /* checkUserAuth fired before a receiver was registered */

/* ---- build + deliver the synthetic signed-in state on the main queue ---- */
static id build_offline_state(void) {
    /* Locale("en","US","") */
    id loc = keep(msg0(CLS("Locale"), "alloc"));
    loc = ((id(*)(id, SEL, id, id, id))objc_msgSend)(loc, SL("initWithLanguage:country:variant:"),
                                                     nsstr("en"), nsstr("US"), nsstr(""));
    keep(loc);
    /* NetflixProfile with the offline identity (fields per the C-ABI AUTH_EVENT JSON) */
    id prof = alloc_init("NetflixProfile");
    set_ivar(prof, "_playerId",          nsstr("offline-player"));
    set_ivar(prof, "_gamerProfileId",    nsstr("offline-player"));
    set_ivar(prof, "_loggingId",         nsstr("offline"));
    set_ivar(prof, "_netflixAccessToken", nsstr("offline"));
    set_ivar(prof, "_profileGUID",       nsstr("offline-player"));
    set_ivar(prof, "_locale",            loc);
    /* NetflixSDKState(currentProfile: prof, previousProfile: nil) */
    id state = keep(msg0(CLS("NetflixSDKState"), "alloc"));
    state = ((id(*)(id, SEL, id, id))objc_msgSend)(state, SL("initWithCurrentProfile:previousProfile:"),
                                                   prof, (id)0);
    return keep(state);
}
static void deliver_auth_worker(void *unused) {
    (void)unused;
    if (!g_receiver) { nfx_log("deliver_auth: no receiver\n"); return; }
    id state = build_offline_state();
    nfx_log("deliver_auth: [receiver onUserStateChange: offlineState]\n");
    ((void(*)(id, SEL, id))objc_msgSend)(g_receiver, SL("onUserStateChange:"), state);
}
static void deliver_auth_async(void) {
    dispatch_async_f((void *)&_dispatch_main_q, 0, deliver_auth_worker);
}

/* ---- swizzled NetflixSDK class methods ---- */
static IMP orig_register = 0;
static void my_registerEventReceiver(id self, SEL _cmd, id receiver) {
    g_receiver = keep(receiver);
    nfx_log("registerEventReceiver: captured\n");
    if (orig_register) ((void(*)(id, SEL, id))orig_register)(self, _cmd, receiver);
    if (g_auth_pending) { g_auth_pending = 0; deliver_auth_async(); }
}
static void my_checkUserAuth(id self, SEL _cmd) {
    (void)self; (void)_cmd;
    nfx_log("checkUserAuth -> synthetic signed-in\n");
    if (g_receiver) deliver_auth_async();
    else g_auth_pending = 1;
}
/* never present Netflix UI offline */
static void my_showAccessButton(id self, SEL _cmd) { (void)self; (void)_cmd; nfx_log("showNetflixAccessButton (no-op)\n"); }
static void my_showMenu(id self, SEL _cmd, int a)  { (void)self; (void)_cmd; (void)a; nfx_log("showNetflixMenu (no-op)\n"); }

/* ---- cloud slot store (async on the main queue) ---- */
typedef struct { int op; void *block; id name; id data; } slotctx;
enum { OP_READ = 1, OP_SAVE, OP_IDS, OP_DELETE };

static void slot_worker(void *p) {
    slotctx *c = (slotctx *)p;
    if (c->op == OP_READ) {
        id data = nfx_store_read(c->name);
        if (data) {
            id si = ((id(*)(id, SEL, id, id))objc_msgSend)(CLS("SlotInfo"), SL("fromData:withTimestamp:"), data, (id)0);
            id r  = ((id(*)(id, SEL, id, id))objc_msgSend)(CLS("ReadSlotResult"), SL("OK:createTimestamp:"), si, (id)0);
            nfx_log("readSlot HIT\n"); inv_block_1(c->block, r);
        } else {
            id r = ((id(*)(id, SEL, int, id))objc_msgSend)(CLS("ReadSlotResult"), SL("error:description:"),
                                                           NFX_UNKNOWN_SLOT, nsstr("unknown slot"));
            nfx_log("readSlot MISS -> ErrorUnknownSlotId\n"); inv_block_1(c->block, r);
        }
    } else if (c->op == OP_SAVE) {
        int ok = nfx_store_write(c->name, c->data);
        id r = ((id(*)(id, SEL, int, id))objc_msgSend)(CLS("SaveSlotResult"), SL("status:errorDescription:"), 0, (id)0);
        nfx_log(ok ? "saveSlot OK\n" : "saveSlot write FAILED (reporting OK)\n"); inv_block_1(c->block, r);
    } else if (c->op == OP_IDS) {
        id ids = nfx_store_list();
        id r = ((id(*)(id, SEL, int, id))objc_msgSend)(CLS("GetSlotIdsResult"), SL("status:slotIds:"), 0, ids);
        nfx_log("getSlotIds\n"); inv_block_1(c->block, r);
    } else { /* OP_DELETE */
        nfx_store_delete(c->name);
        id r = CLS("DeleteSlotResult") ? alloc_init("DeleteSlotResult") : (id)0;
        nfx_log("deleteSlot\n"); inv_block_1(c->block, r);
    }
}
static void slot_dispatch(int op, void *block, id name, id data) {
    slotctx *c = (slotctx *)malloc(sizeof(slotctx));
    c->op = op; c->block = block ? _Block_copy(block) : 0;
    c->name = name ? keep(name) : 0; c->data = data ? keep(data) : 0;
    dispatch_async_f((void *)&_dispatch_main_q, c, slot_worker);
}
static void my_getSlotIds(id self, SEL _cmd, void *cb)                  { (void)self;(void)_cmd; slot_dispatch(OP_IDS, cb, 0, 0); }
static void my_readSlot(id self, SEL _cmd, id slotId, void *cb)         { (void)self;(void)_cmd; slot_dispatch(OP_READ, cb, slotId, 0); }
static void my_deleteSlot(id self, SEL _cmd, id slotId, void *cb)       { (void)self;(void)_cmd; slot_dispatch(OP_DELETE, cb, slotId, 0); }
static void my_saveSlot(id self, SEL _cmd, id slotId, id slotInfo, void *cb) {
    (void)self; (void)_cmd;
    id data = slotInfo ? msg0(slotInfo, "data") : 0;   /* extract NSData now (slotInfo is transient) */
    slot_dispatch(OP_SAVE, cb, slotId, data);
}

__attribute__((constructor))
static void nfx_gen0_objc_load(void) {
    nfx_ensure_store();
    nfx_log("==== gen-0 Obj-C (NetflixSDK) shim loaded ====\n");
    if (!CLS("NetflixSDK")) { nfx_log("NetflixSDK class absent - nothing to do\n"); return; }
    orig_register = swizzle_class_method("NetflixSDK", "registerEventReceiver:", (IMP)my_registerEventReceiver);
    swizzle_class_method("NetflixSDK", "checkUserAuth",            (IMP)my_checkUserAuth);
    swizzle_class_method("NetflixSDK", "showNetflixAccessButton",  (IMP)my_showAccessButton);
    swizzle_class_method("NetflixSDK", "showNetflixMenu:",         (IMP)my_showMenu);
    swizzle_class_method("NetflixSDK", "getSlotIds:",              (IMP)my_getSlotIds);
    swizzle_class_method("NetflixSDK", "readSlot:callback:",       (IMP)my_readSlot);
    swizzle_class_method("NetflixSDK", "saveSlot:slotInfo:callback:", (IMP)my_saveSlot);
    swizzle_class_method("NetflixSDK", "deleteSlot:callback:",     (IMP)my_deleteSlot);
    nfx_log("gen-0 Obj-C swizzles installed\n");
}
