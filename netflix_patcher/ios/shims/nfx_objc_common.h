/*
 * Shared preamble for the Obj-C-binding shims (gen0_objc, gen2_objc). Unlike the C-ABI shims,
 * UE4/GameMaker reach the SDK through its Obj-C/Swift API, so these swizzle SDK methods in a
 * load-time constructor. Self-contained: everything is declared here and resolved at runtime via
 * -undefined dynamic_lookup (libSystem re-exports libobjc + libdispatch; Foundation classes are
 * looked up by name). Objects we create are retained once (keep()) and never freed - a few small
 * long-lived objects, so leaking sidesteps ARC bookkeeping in headerless C. See BUILD.md.
 */
#ifndef NFX_OBJC_COMMON_H
#define NFX_OBJC_COMMON_H

/* IntelliSense parses in MSVC mode and rejects Clang's __attribute__; no-op it for the language
 * server only (clang never defines __INTELLISENSE__). */
#ifdef __INTELLISENSE__
#define __attribute__(x)
#endif

/* ---- Objective-C runtime (libobjc, via libSystem re-export) ---- */
typedef void *id;
typedef void *Class;
typedef void *SEL;
typedef void *Method;
typedef void *Ivar;
typedef void *IMP;

extern id     objc_getClass(const char *);
extern SEL    sel_registerName(const char *);
extern Class  object_getClass(id);
extern Method class_getInstanceMethod(Class, SEL);
extern Method class_getClassMethod(Class, SEL);
extern IMP    method_getImplementation(Method);
extern IMP    method_setImplementation(Method, IMP);
extern Ivar   class_getInstanceVariable(Class, const char *);
extern void   object_setIvar(id, Ivar, id);
extern id     object_getIvar(id, Ivar);
extern id     objc_retain(id);              /* nil-safe */
extern id     objc_msgSend(id, SEL, ...);   /* cast to the right prototype at each call site */
extern Class  objc_allocateClassPair(Class superclass, const char *name, unsigned long extra);
extern void   objc_registerClassPair(Class);
extern char   class_addMethod(Class, SEL, IMP, const char *types);
extern void   objc_setAssociatedObject(id, const void *key, id value, long policy);
extern id     objc_getAssociatedObject(id, const void *key);
#define NFX_ASSOC_RETAIN 1   /* OBJC_ASSOCIATION_RETAIN_NONATOMIC */

/* ---- libc (libSystem) ---- */
extern char  *getenv(const char *);
extern int    snprintf(char *, unsigned long, const char *, ...);
extern unsigned long strlen(const char *);
extern void  *malloc(unsigned long);
extern void   syslog(int, const char *, ...);   /* -> device console */
extern int    open(const char *, int, ...);
extern long   write(int, const void *, unsigned long);
extern int    close(int);
extern int    mkdir(const char *, unsigned short);

/* ---- libdispatch + Blocks (libSystem) ---- */
extern char   _dispatch_main_q;                                  /* main queue object; take & */
extern void   dispatch_async_f(void *queue, void *ctx, void (*work)(void *));
extern void  *_Block_copy(const void *);

#define CLS(n) objc_getClass(n)
#define SL(n)  sel_registerName(n)

/* pin an object for the app lifetime */
static id keep(id o) { if (o) objc_retain(o); return o; }

/* append a line to <app>/Documents/nfx_shim.log (+ device console). */
#define O_WRONLY 0x0001
#define O_CREAT  0x0200
#define O_APPEND 0x0008
static void nfx_log(const char *msg) {
    syslog(5, "NFXSHIM %s", msg);
    const char *h = getenv("HOME");
    char path[700];
    snprintf(path, sizeof path, "%s/Documents/nfx_shim.log", h ? h : "/tmp");
    int fd = open(path, O_WRONLY | O_CREAT | O_APPEND, 0644);
    if (fd >= 0) { write(fd, msg, strlen(msg)); close(fd); }
}

/* ---- typed objc_msgSend helpers ---- */
static id  msg0(id o, const char *s)            { return ((id(*)(id, SEL))objc_msgSend)(o, SL(s)); }
static id  msg1(id o, const char *s, id a)      { return ((id(*)(id, SEL, id))objc_msgSend)(o, SL(s), a); }
static id  msg2(id o, const char *s, id a, id b){ return ((id(*)(id, SEL, id, id))objc_msgSend)(o, SL(s), a, b); }
static long msgL(id o, const char *s)           { return ((long(*)(id, SEL))objc_msgSend)(o, SL(s)); }

/* Foundation object builders (all kept) */
static id nsstr(const char *c) {
    return keep(((id(*)(id, SEL, const char *))objc_msgSend)(CLS("NSString"), SL("stringWithUTF8String:"), c ? c : ""));
}
static id nsdata(const void *b, unsigned long n) {
    return keep(((id(*)(id, SEL, const void *, unsigned long))objc_msgSend)(CLS("NSData"), SL("dataWithBytes:length:"), b, n));
}
static id nsnum(long v) {
    return keep(((id(*)(id, SEL, long))objc_msgSend)(CLS("NSNumber"), SL("numberWithLongLong:"), v));
}
static id nserror(const char *domain, long code) {
    return keep(((id(*)(id, SEL, id, long, id))objc_msgSend)(CLS("NSError"), SL("errorWithDomain:code:userInfo:"), nsstr(domain), code, (id)0));
}

/* set an Obj-C ivar by name (value kept). Only valid for Obj-C ivars, not Swift value types. */
static void set_ivar(id obj, const char *name, id val) {
    if (!obj) return;
    Ivar iv = class_getInstanceVariable(object_getClass(obj), name);
    if (iv) object_setIvar(obj, iv, keep(val));
}

/* [[cls alloc] init] by name (kept) */
static id alloc_init(const char *cls) {
    return keep(msg0(msg0(CLS(cls), "alloc"), "init"));
}

/* invoke a game-provided completion block. Block ABI: {isa; flags; reserved; invoke; ...}. */
struct nfx_block { void *isa; int flags; int reserved; void *invoke; };
static void inv_block_1(void *blk, id a) {
    if (!blk) return;
    ((void(*)(void *, id))((struct nfx_block *)blk)->invoke)(blk, a);
}
static void inv_block_2(void *blk, id a, id b) {
    if (!blk) return;
    ((void(*)(void *, id, id))((struct nfx_block *)blk)->invoke)(blk, a, b);
}

/* make/register a duck-typed NSObject subclass at runtime (idempotent) */
static Class make_class(const char *name) {
    Class c = CLS(name);
    if (c) return c;
    c = objc_allocateClassPair(CLS("NSObject"), name, 0);
    if (c) objc_registerClassPair(c);
    return c;
}
static void add_method(Class c, const char *sel, IMP imp, const char *types) {
    if (c) class_addMethod(c, SL(sel), imp, types);
}

/* ---- swizzle helpers (return the original IMP, or 0 if absent) ---- */
static IMP swizzle_class_method(const char *cls, const char *sel, IMP newimp) {
    Class c = CLS(cls);
    if (!c) return 0;
    Method m = class_getClassMethod(c, SL(sel));
    if (!m) return 0;
    IMP old = method_getImplementation(m);
    method_setImplementation(m, newimp);
    return old;
}
static IMP swizzle_instance_method(const char *cls, const char *sel, IMP newimp) {
    Class c = CLS(cls);
    if (!c) return 0;
    Method m = class_getInstanceMethod(c, SL(sel));
    if (!m) return 0;
    IMP old = method_getImplementation(m);
    method_setImplementation(m, newimp);
    return old;
}

/* ---- app-container file store (Documents/nfx_store/<safe-id>), Foundation file APIs ---- */
static void nfx_store_dir(char *out, int cap) {
    const char *h = getenv("HOME");
    snprintf(out, cap, "%s/Documents/nfx_store", h ? h : "/tmp");
}
static void nfx_ensure_store(void) {
    const char *h = getenv("HOME");
    char p[600];
    snprintf(p, sizeof p, "%s/Documents", h ? h : "/tmp"); mkdir(p, 0755);
    nfx_store_dir(p, sizeof p); mkdir(p, 0755);
}
static void nfx_safe_id(const char *id_, char *out, int cap) {
    int i = 0;
    for (; id_ && id_[i] && i < cap - 1; i++) {
        char ch = id_[i];
        int ok = (ch >= 'a' && ch <= 'z') || (ch >= 'A' && ch <= 'Z') || (ch >= '0' && ch <= '9')
                 || ch == '.' || ch == '-' || ch == '_';
        out[i] = ok ? ch : '_';
    }
    out[i] = 0;
}
static id nfx_store_path(id nsName) {
    char dir[600]; nfx_store_dir(dir, sizeof dir);
    const char *c = ((const char *(*)(id, SEL))objc_msgSend)(nsName, SL("UTF8String"));
    char safe[300]; nfx_safe_id(c, safe, sizeof safe);
    char full[1000]; snprintf(full, sizeof full, "%s/%s", dir, safe);
    return nsstr(full);
}
static id nfx_store_read(id nsName) {   /* NSData or nil */
    return ((id(*)(id, SEL, id))objc_msgSend)(CLS("NSData"), SL("dataWithContentsOfFile:"), nfx_store_path(nsName));
}
static int nfx_store_write(id nsName, id nsData) {   /* 1/0 */
    nfx_ensure_store();
    return (int)((char(*)(id, SEL, id, char))objc_msgSend)(nsData, SL("writeToFile:atomically:"), nfx_store_path(nsName), 1);
}
static void nfx_store_delete(id nsName) {
    id fm = msg0(CLS("NSFileManager"), "defaultManager");
    ((char(*)(id, SEL, id, id))objc_msgSend)(fm, SL("removeItemAtPath:error:"), nfx_store_path(nsName), (id)0);
}
static id nfx_store_list(void) {   /* NSArray<NSString*>* of stored ids */
    nfx_ensure_store();
    char dir[600]; nfx_store_dir(dir, sizeof dir);
    id fm = msg0(CLS("NSFileManager"), "defaultManager");
    id arr = ((id(*)(id, SEL, id, id))objc_msgSend)(fm, SL("contentsOfDirectoryAtPath:error:"), nsstr(dir), (id)0);
    return arr ? arr : msg0(CLS("NSArray"), "array");
}

#endif /* NFX_OBJC_COMMON_H */
