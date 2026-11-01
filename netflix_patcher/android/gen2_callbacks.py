"""Deliver direct SDK callbacks through Android's main queue, without coroutines."""
from .. import MARKER
from .smali import _CB, _RES, find_smali_file


_DELIVERY = "Lcom/netflix/games/NfxOfflineResult;"


def emit_result_delivery(dec):
    target = find_smali_file(dec, "com/netflix/games/Callback")
    if target is None:
        raise RuntimeError("gen-2 Callback class missing (SDK layout changed?)")
    code = f""".class public final {_DELIVERY}
.super Ljava/lang/Object;
.implements Ljava/lang/Runnable;

# {MARKER}: one queued delivery per offline SDK request
.field private final callback:{_CB}
.field private final result:{_RES}

.method private constructor <init>({_CB}{_RES})V
    .locals 0
    invoke-direct {{p0}}, Ljava/lang/Object;-><init>()V
    iput-object p1, p0, {_DELIVERY}->callback:{_CB}
    iput-object p2, p0, {_DELIVERY}->result:{_RES}
    return-void
.end method

.method public static post({_CB}{_RES})V
    .locals 3
    invoke-static {{}}, Landroid/os/Looper;->getMainLooper()Landroid/os/Looper;
    move-result-object v0
    new-instance v1, Landroid/os/Handler;
    invoke-direct {{v1, v0}}, Landroid/os/Handler;-><init>(Landroid/os/Looper;)V
    new-instance v2, {_DELIVERY}
    invoke-direct {{v2, p0, p1}}, {_DELIVERY}-><init>({_CB}{_RES})V
    invoke-virtual {{v1, v2}}, Landroid/os/Handler;->post(Ljava/lang/Runnable;)Z
    move-result v0
    if-nez v0, :nfx_posted
    new-instance v0, Ljava/lang/IllegalStateException;
    const-string v1, "Offline SDK callback rejected by main queue"
    invoke-direct {{v0, v1}}, Ljava/lang/IllegalStateException;-><init>(Ljava/lang/String;)V
    throw v0
    :nfx_posted
    return-void
.end method

.method public run()V
    .locals 2
    iget-object v0, p0, {_DELIVERY}->callback:{_CB}
    iget-object v1, p0, {_DELIVERY}->result:{_RES}
    invoke-interface {{v0, v1}}, {_CB}->onResult({_RES})V
    return-void
.end method
"""
    (target.parent / "NfxOfflineResult.smali").write_text(code, encoding="utf-8")


def deliver(callback, result):
    return f"invoke-static {{{callback}, {result}}}, {_DELIVERY}->post({_CB}{_RES})V"
