"""Unreal Engine (libUE4) + oldest com.netflix.android.api SDK: the glue is an Unreal JNI
class (Thunk_* in, native nativeOn* out). Deliver a synthetic signed-in profile straight
to the native callback. Auth only; progress lives in the engine's own on-device save."""
import re
import sys
from .smali import find_smali_file, patch_method, _class_of
from .gen0 import _G0_PID, _G0_LOCALE, _G0_LOCALE_CTOR, _G0_PROFILE, _G0_PROFILE_CTOR
from .. import MARKER


def _ue_find_glue(dec):
    """The UE4<->Netflix glue class file: Unreal JNI, Thunk_checkNetflixUserAuth calls in and
    nativeOnNetflixUserStateChanged calls back into native. Both names are game glue, not SDK."""
    for d in sorted(dec.glob("smali*")):
        for f in d.rglob("*.smali"):
            try:
                t = f.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            if "Thunk_checkNetflixUserAuth(" in t and "nativeOnNetflixUserStateChanged(" in t:
                return f
    return None

def is_unreal_gen0(dec):
    """UE4 + oldest SDK: a glue class with the Unreal JNI Thunk/native user-state bridge."""
    return _ue_find_glue(dec) is not None

def patch_unreal_gen0(dec, report):
    """Deliver a synthetic signed-in gen-0 state through the UE4 native callback."""
    def note(status, name):
        report[status].append(name)

    # 1. suppress the SDK error dialog (2-arg variant)
    ef = find_smali_file(dec, "com/netflix/mediaclient/ui/errors/SdkErrorActivity$Companion")
    if ef is None:
        sys.exit("! critical class missing: SdkErrorActivity$Companion (SDK layout changed?)")
    new, st = patch_method(ef.read_text(encoding="utf-8"), "startSdkErrorActivity(", 0, ["return-void"])
    if st == "patched":
        ef.write_text(new, encoding="utf-8")
    note(st, "suppress-error-screen")

    glue_f = _ue_find_glue(dec)
    if glue_f is None:
        sys.exit("! UE4 Netflix glue not found (Thunk_checkNetflixUserAuth)")
    glue = _class_of(glue_f)                 # Lcom/netflix/NGP/NetflixSDK;
    pkg = glue[1:glue.rfind("/")]            # com/netflix/NGP
    gt = glue_f.read_text(encoding="utf-8")

    # the native user-state callback's exact descriptor (J, profile, profile, PlayerIdentity)
    m = re.search(r"nativeOnNetflixUserStateChanged(\([^)]*\)V)", gt)
    if not m:
        sys.exit("! nativeOnNetflixUserStateChanged signature not found in the glue")
    native_cb = f"{glue}->nativeOnNetflixUserStateChanged{m.group(1)}"

    # the long field holding the native pointer (set in Thunk_checkNetflixUserAuth via iput-wide);
    # flip it to public so the inner classes can read it without a fragile access$NNN accessor.
    thunk = re.search(r"\.method[^\n]*Thunk_checkNetflixUserAuth\([^)]*\)V.*?\.end method", gt, re.DOTALL)
    fm = re.search(r"iput-wide [pv]\d+, [pv]\d+, " + re.escape(glue) + r"->(\w+):J",
                   thunk.group(0) if thunk else "")
    if not fm:
        sys.exit("! native-pointer field not found in Thunk_checkNetflixUserAuth")
    nf_field = fm.group(1)
    gt = re.sub(r"\.field private (?:final )?" + re.escape(nf_field) + r":J",
                f".field public {nf_field}:J", gt)
    glue_f.write_text(gt, encoding="utf-8")

    # 2. dummy offline PlayerIdentity in the glue's package (never null)
    dummy = f"L{pkg}/NfxOfflinePlayer;"
    acc = '    .locals 1\n    const-string v0, "offline-player"\n    return-object v0\n'
    (glue_f.parent / "NfxOfflinePlayer.smali").write_text(
        f".class public {dummy}\n.super Ljava/lang/Object;\n.implements {_G0_PID}\n\n"
        f"# {MARKER}: dummy offline PlayerIdentity (never null)\n\n"
        ".method public constructor <init>()V\n    .locals 0\n"
        "    invoke-direct {p0}, Ljava/lang/Object;-><init>()V\n    return-void\n.end method\n\n"
        f".method public getHandle()Ljava/lang/String;\n{acc}.end method\n\n"
        f".method public getPlayerId()Ljava/lang/String;\n{acc}.end method\n\n"
        f".method public handle()Ljava/lang/String;\n{acc}.end method\n\n"
        f".method public playerId()Ljava/lang/String;\n{acc}.end method\n", encoding="utf-8")
    note("patched", "grant-offline-player")

    # the synthetic-delivery body, shared by the Runnable and the event handler. `inner` is the
    # inner class descriptor, `outer` its synthetic outer-instance field. Builds an offline
    # NetflixProfile and calls nativeOnNetflixUserStateChanged(nativeObj, null, profile, player).
    def deliver_body(inner, outer):
        return [
            f"iget-object v8, p0, {inner}->{outer}:{glue}",
            f"iget-wide v9, v8, {glue}->{nf_field}:J",
            f"new-instance v5, {_G0_LOCALE}",
            'const-string v0, "en"', 'const-string v1, "US"', 'const-string v2, ""',
            f"invoke-direct {{v5, v0, v1, v2}}, {_G0_LOCALE_CTOR}",
            f"new-instance v0, {_G0_PROFILE}",
            'const-string v1, "offline"', 'const-string v2, "offline"',
            'const-string v3, "offline"', 'const-string v4, "offline-player"',
            f"invoke-direct/range {{v0 .. v5}}, {_G0_PROFILE_CTOR}",
            "move-object v7, v0",
            f"new-instance v6, {dummy}",
            f"invoke-direct {{v6}}, {dummy}-><init>()V",
            "move-object v0, v8",   # this (glue)
            "move-wide v1, v9",     # nativeObj
            "const/4 v3, 0x0",      # previousProfile = null
            "move-object v4, v7",   # currentProfile = offline profile
            "move-object v5, v6",   # playerIdentity
            f"invoke-virtual/range {{v0 .. v5}}, {native_cb}",
            "return-void",
        ]

    def outer_field(f):
        mm = re.search(r"\.field final synthetic (\w+):" + re.escape(glue),
                       f.read_text(encoding="utf-8"))
        return mm.group(1) if mm else "this$0"

    # the glue's inner classes: the checkUserAuth Runnable and the event handler.
    runnable = handler = None
    for f in sorted(glue_f.parent.glob(glue_f.stem + "$*.smali")):
        t = f.read_text(encoding="utf-8", errors="ignore")
        if "NetflixSdkEventHandler;" in t and "onUserStateChange(" in t:
            handler = f
        elif "Ljava/lang/Runnable;" in t and "checkUserAuth(" in t:
            runnable = f

    # 3. checkUserAuth Runnable's run() -> deliver the authed state (skip the network)
    if runnable is None:
        sys.exit("! checkUserAuth Runnable inner class not found (glue layout changed?)")
    new, st = patch_method(runnable.read_text(encoding="utf-8"), "run()V", 11,
                           deliver_body(_class_of(runnable), outer_field(runnable)))
    if st == "patched":
        runnable.write_text(new, encoding="utf-8")
    note(st, "grant-auth-at-checkUserAuth")

    # 4. event handler's onUserStateChange(state) -> always deliver the authed state (chokepoint)
    if handler is not None:
        new, st = patch_method(handler.read_text(encoding="utf-8"), "onUserStateChange(", 11,
                               deliver_body(_class_of(handler), outer_field(handler)))
        if st == "patched":
            handler.write_text(new, encoding="utf-8")
        note(st, "force-authenticated-event")
