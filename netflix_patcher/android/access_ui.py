"""Older (~2024) SDK with no doRequestPlayerAccess: the gate is an access-UI + event
model. No-op the error screen, hand back a synthetic offline profile, and fire the
access-granted / UI-hidden events so the boot handshake completes."""
import sys
from .. import MARKER
from .smali import (find_smali_file, patch_method, _decl_file, _class_of,
                    SDK_MARKER_CLASS, _PAI, _RES)


_PROFILES_API = "com/netflix/games/player/profiles/ProfilesApi"

_CURRENT_PROFILE = "com/netflix/games/player/profiles/CurrentProfile"

_COMP = "Lcom/netflix/games/NetflixResult$Companion;"

def is_legacy_sdk(dec):
    """Older access-UI SDK = NfUnitySdkInternal has no doRequestPlayerAccess but does the UI call."""
    nf = find_smali_file(dec, SDK_MARKER_CLASS)
    t = nf.read_text(encoding="utf-8") if nf else ""
    return ("doRequestPlayerAccess(" not in t) and ("ShowNetflixAccessUIIfNecessary(" in t.replace("show", "Show"))

def _offline_stub(ret):
    """Method body returning an offline value for the given return descriptor."""
    if ret == _RES:
        return [f"sget-object v0, {_RES}->Companion:{_COMP}", "const/4 v1, -0x1",
                'const-string v2, "offline"',
                f"invoke-virtual {{v0, v1, v2}}, {_COMP}->withError(ILjava/lang/String;)Lcom/netflix/games/NetflixResult;",
                "move-result-object v0", "return-object v0"]
    if ret == "V":
        return ["return-void"]
    if ret[0] in "L[":
        return ["const/4 v0, 0x0", "return-object v0"]
    if ret in ("J", "D"):
        return ["const-wide/16 v0, 0x0", "return-wide v0"]
    return ["const/4 v0, 0x0", "return v0"]

def patch_legacy(dec, report):
    """Apply the older-SDK recipe. Appends outcomes to `report`."""
    def note(status, name):
        report[status].append(name)

    # 1. error screen (match by name -> any arity, incl. the older 2-arg)
    ef = find_smali_file(dec, "com/netflix/mediaclient/ui/errors/SdkErrorActivity$Companion")
    if ef is None:
        sys.exit("! critical class missing: SdkErrorActivity$Companion (SDK layout changed?)")
    new, st = patch_method(ef.read_text(encoding="utf-8"), "startSdkErrorActivity(", 0, ["return-void"])
    if st == "patched":
        ef.write_text(new, encoding="utf-8")
    elif st == "not_found":
        sys.exit("! critical method missing: startSdkErrorActivity")
    note(st, "suppress-error-screen")

    # 2. synthetic offline profile
    impl = _decl_file(dec, _PROFILES_API, "implements")
    sub = _decl_file(dec, _CURRENT_PROFILE, "super")
    if impl is None or sub is None:
        sys.exit("! ProfilesApi impl / CurrentProfile subclass not found (SDK version drift?)")
    sub_cls = _class_of(sub)
    ctor = next((ln for ln in sub.read_text(encoding="utf-8").split("\n")
                 if ".method public constructor <init>(Ljava/lang/String;Ljava/lang/String;Ljava/lang/String;L" in ln), None)
    if ctor is None:
        sys.exit("! CurrentProfile ctor(String,String,String,LegacyProfileFields) not found.")
    legacy_iface = "L" + ctor.split("Ljava/lang/String;Ljava/lang/String;Ljava/lang/String;L", 1)[1].split(";", 1)[0] + ";"
    ctor_sig = f"<init>(Ljava/lang/String;Ljava/lang/String;Ljava/lang/String;{legacy_iface})V"

    # generate a dummy LegacyProfileFields impl beside the ProfilesApi impl (same dex)
    iface_file = find_smali_file(dec, legacy_iface[1:-1])
    if iface_file is None:
        sys.exit(f"! LegacyProfileFields interface not found: {legacy_iface}")
    dummy_cls = f"Lcom/netflix/games/player/profiles/NfxLegacyFields;"
    dummy_file = impl.parent / "NfxLegacyFields.smali"
    out = [f".class public {dummy_cls}", ".super Ljava/lang/Object;",
           f".implements {legacy_iface}", "",
           f"# {MARKER}: dummy LegacyProfileFields for the synthetic offline profile", "",
           ".method public constructor <init>()V", "    .locals 0",
           "    invoke-direct {p0}, Ljava/lang/Object;-><init>()V", "    return-void", ".end method"]
    for ln in iface_file.read_text(encoding="utf-8").split("\n"):
        s = ln.strip()
        if s.startswith(".method public abstract "):
            decl = s[len(".method public abstract "):]
            ret = decl.rsplit(")", 1)[1]
            out += ["", f".method public {decl}", "    .locals 3"] + ["    " + b for b in _offline_stub(ret)] + [".end method"]
    dummy_file.write_text("\n".join(out) + "\n", encoding="utf-8")

    body = [
        f"new-instance v0, {sub_cls}",
        'const-string v1, "offline-player"',
        'const-string v2, "Offline"',
        'const-string v3, "en"',
        f"new-instance v4, {dummy_cls}",
        f"invoke-direct {{v4}}, {dummy_cls}-><init>()V",
        f"invoke-direct {{v0, v1, v2, v3, v4}}, {sub_cls}->{ctor_sig}",
        f"sget-object v1, {_RES}->Companion:{_COMP}",
        f"invoke-virtual {{v1, v0}}, {_COMP}->withData(Ljava/lang/Object;)Lcom/netflix/games/NetflixResult;",
        "move-result-object v0", "return-object v0",
    ]
    new, st = patch_method(impl.read_text(encoding="utf-8"), "getCurrentProfile()", 5, body)
    if st == "patched":
        impl.write_text(new, encoding="utf-8")
    elif st == "not_found":
        sys.exit("! getCurrentProfile() not found in the ProfilesApi impl.")
    note(st, "grant-offline-profile")

    # 3. fire access-granted + access-UI-hidden once on the SDK's access-UI call
    nf = find_smali_file(dec, SDK_MARKER_CLASS)
    t = nf.read_text(encoding="utf-8")
    if "nfxFired:Z" not in t:
        lines = t.split("\n")
        idx = next(i for i, l in enumerate(lines) if l.startswith(".method") or l.startswith(".field"))
        lines.insert(idx, ".field private static nfxFired:Z\n")
        t = "\n".join(lines)
    NF, EVS, SCTX = f"L{SDK_MARKER_CLASS};", "Lcom/netflix/unity/impl/EventSenderImpl;", "Lcom/netflix/unity/impl/SdkContext;"
    body = [
        f"iget-object v0, p0, {NF}->netflixGames:Lcom/netflix/games/NetflixGames;",
        "invoke-virtual {v0}, Lcom/netflix/games/NetflixGames;->getAccessApi()Lcom/netflix/games/player/access/AccessApi;",
        "move-result-object v0",
        "invoke-interface {v0}, Lcom/netflix/games/player/access/AccessApi;->showNetflixAccessUIIfNecessary()V",
        f"sget-boolean v0, {NF}->nfxFired:Z",
        "if-nez v0, :nfx_done",
        "const/4 v0, 0x1",
        f"sput-boolean v0, {NF}->nfxFired:Z",
        f"new-instance v0, {EVS}",
        f"invoke-direct {{p0}}, {NF}->getSdkContext()L{SCTX[1:]}",
        "move-result-object v1",
        f"invoke-direct {{v0, v1}}, {EVS}-><init>({SCTX})V",
        f"new-instance v1, {_PAI}",
        'const-string v2, "offline-player"',
        f"invoke-direct {{v1, v2}}, {_PAI}-><init>(Ljava/lang/String;)V",
        "new-instance v2, Lcom/netflix/games/player/access/PlayerAccessEvent;",
        "const/4 v3, 0x0",
        f"invoke-direct {{v2, v1, v3}}, Lcom/netflix/games/player/access/PlayerAccessEvent;-><init>({_PAI}{_PAI})V",
        f"invoke-virtual {{v0, v2}}, {EVS}->onPlayerAccessChanged(Lcom/netflix/games/player/access/PlayerAccessEvent;)V",
        f"invoke-virtual {{v0}}, {EVS}->onNetflixUiShown()V",
        f"invoke-virtual {{v0}}, {EVS}->onNetflixUiHidden()V",
        ":nfx_done",
        "return-void",
    ]
    new, st = patch_method(t, "doShowNetflixAccessUIIfNecessary()", 4, body)
    if st == "not_found":
        sys.exit("! doShowNetflixAccessUIIfNecessary() not found (SDK version drift?)")
    nf.write_text(new if st == "patched" else t, encoding="utf-8")
    note(st, "grant-access-and-dismiss-ui")
