"""Oldest (2022-2023) com.netflix.android.api SDK: auth is a checkUserAuth call whose
result arrives as an onUserStateChange event. Return a dummy player, force a signed-in
state, and swap the dead cloud slot API for a local file store so saves persist."""
import re
import sys
from .smali import find_smali_file, patch_method, SDK_MARKER_CLASS
from .. import MARKER


_G0_LOCALE = "Lcom/netflix/android/api/netflixsdk/Locale;"

_G0_PROFILE = "Lcom/netflix/android/api/netflixsdk/NetflixProfile;"

_G0_STATE = "Lcom/netflix/android/api/netflixsdk/NetflixSdkState;"

_G0_PID = "Lcom/netflix/android/api/player/PlayerIdentity;"

_G0_EVS = "Lcom/netflix/unity/impl/EventSenderImpl;"

_G0_SCTX = "Lcom/netflix/unity/impl/SdkContext;"

_G0_NFEVENT = "Lcom/netflix/unity/api/NetflixEvent;"

_G0_NF = "Lcom/netflix/unity/impl/NfUnitySdkInternal;"

_G0_DUMMY = "Lcom/netflix/unity/impl/NfxOfflinePlayer;"

_G0_CSR = "Lcom/netflix/unity/api/cloudsave/CloudSaveResult;"

_G0_CSS = "Lcom/netflix/android/api/cloudsave/CloudSaveStatus;"

_G0_ESI = "Lcom/netflix/android/api/cloudsave/ExtendedSlotInfo;"

_G0_CB = "Lcom/netflix/unity/api/cloudsave/CloudSaveCallback;"

_G0_GB = "Lcom/netflix/unity/api/cloudsave/GetSlotIdsCallback;"

_G0_CONF = "Lcom/netflix/android/api/cloudsave/ConflictResolution;"

_G0_STORE = "Lcom/netflix/unity/impl/NfxSlotStore;"

_G0_LOCALE_CTOR = f"{_G0_LOCALE}-><init>(Ljava/lang/String;Ljava/lang/String;Ljava/lang/String;)V"

_G0_PROFILE_CTOR = f"{_G0_PROFILE}-><init>(Ljava/lang/String;Ljava/lang/String;Ljava/lang/String;Ljava/lang/String;{_G0_LOCALE})V"

_G0_STATE_CTOR = f"{_G0_STATE}-><init>({_G0_PROFILE}{_G0_PROFILE})V"

_G0_CSR_CTOR = f"{_G0_CSR}-><init>({_G0_CSS}Ljava/lang/String;{_G0_CONF})V"

_G0_SLOT_STORE_TEMPLATE = r'''.class public final Lcom/netflix/unity/impl/NfxSlotStore;
.super Ljava/lang/Object;

# netflix-offline-patcher: local slot store (the game's only progress save is the Netflix
# cloud-save slot api, dead offline; back it with real local files so progress persists)

.method private static dir()Ljava/lang/String;
    .locals 3
    :try_start_0
    invoke-static {}, Landroid/app/ActivityThread;->currentApplication()Landroid/app/Application;
    move-result-object v0
    invoke-virtual {v0}, Landroid/app/Application;->getFilesDir()Ljava/io/File;
    move-result-object v0
    new-instance v1, Ljava/io/File;
    const-string v2, "nfxslots"
    invoke-direct {v1, v0, v2}, Ljava/io/File;-><init>(Ljava/io/File;Ljava/lang/String;)V
    invoke-virtual {v1}, Ljava/io/File;->getAbsolutePath()Ljava/lang/String;
    move-result-object v0
    :try_end_0
    .catch Ljava/lang/Throwable; {:try_start_0 .. :try_end_0} :catch_0
    return-object v0
    :catch_0
    move-exception v0
    const-string v0, "__NFXDIR__"
    return-object v0
.end method

.method public static read(Ljava/lang/String;)Ljava/lang/String;
    .locals 6
    const/4 v5, 0x0
    :try_start_0
    new-instance v0, Ljava/io/File;
    invoke-static {}, Lcom/netflix/unity/impl/NfxSlotStore;->dir()Ljava/lang/String;
    move-result-object v1
    new-instance v2, Ljava/lang/StringBuilder;
    invoke-direct {v2}, Ljava/lang/StringBuilder;-><init>()V
    const-string v3, "slot_"
    invoke-virtual {v2, v3}, Ljava/lang/StringBuilder;->append(Ljava/lang/String;)Ljava/lang/StringBuilder;
    invoke-virtual {v2, p0}, Ljava/lang/StringBuilder;->append(Ljava/lang/String;)Ljava/lang/StringBuilder;
    invoke-virtual {v2}, Ljava/lang/StringBuilder;->toString()Ljava/lang/String;
    move-result-object v2
    invoke-direct {v0, v1, v2}, Ljava/io/File;-><init>(Ljava/lang/String;Ljava/lang/String;)V
    invoke-virtual {v0}, Ljava/io/File;->exists()Z
    move-result v1
    if-nez v1, :cond_0
    goto :goto_0
    :cond_0
    invoke-virtual {v0}, Ljava/io/File;->length()J
    move-result-wide v1
    long-to-int v1, v1
    new-array v2, v1, [B
    new-instance v3, Ljava/io/FileInputStream;
    invoke-direct {v3, v0}, Ljava/io/FileInputStream;-><init>(Ljava/io/File;)V
    new-instance v0, Ljava/io/DataInputStream;
    invoke-direct {v0, v3}, Ljava/io/DataInputStream;-><init>(Ljava/io/InputStream;)V
    invoke-virtual {v0, v2}, Ljava/io/DataInputStream;->readFully([B)V
    invoke-virtual {v0}, Ljava/io/DataInputStream;->close()V
    new-instance v0, Ljava/lang/String;
    invoke-direct {v0, v2}, Ljava/lang/String;-><init>([B)V
    move-object v5, v0
    :goto_0
    :try_end_0
    .catch Ljava/lang/Throwable; {:try_start_0 .. :try_end_0} :catch_0
    goto :goto_1
    :catch_0
    move-exception v0
    const/4 v5, 0x0
    :goto_1
    return-object v5
.end method

.method public static write(Ljava/lang/String;Ljava/lang/String;)V
    .locals 4
    :try_start_0
    new-instance v0, Ljava/io/File;
    invoke-static {}, Lcom/netflix/unity/impl/NfxSlotStore;->dir()Ljava/lang/String;
    move-result-object v1
    invoke-direct {v0, v1}, Ljava/io/File;-><init>(Ljava/lang/String;)V
    invoke-virtual {v0}, Ljava/io/File;->mkdirs()Z
    new-instance v1, Ljava/io/File;
    new-instance v2, Ljava/lang/StringBuilder;
    invoke-direct {v2}, Ljava/lang/StringBuilder;-><init>()V
    const-string v3, "slot_"
    invoke-virtual {v2, v3}, Ljava/lang/StringBuilder;->append(Ljava/lang/String;)Ljava/lang/StringBuilder;
    invoke-virtual {v2, p0}, Ljava/lang/StringBuilder;->append(Ljava/lang/String;)Ljava/lang/StringBuilder;
    invoke-virtual {v2}, Ljava/lang/StringBuilder;->toString()Ljava/lang/String;
    move-result-object v2
    invoke-direct {v1, v0, v2}, Ljava/io/File;-><init>(Ljava/io/File;Ljava/lang/String;)V
    new-instance v0, Ljava/io/FileOutputStream;
    invoke-direct {v0, v1}, Ljava/io/FileOutputStream;-><init>(Ljava/io/File;)V
    if-eqz p1, :cond_0
    invoke-virtual {p1}, Ljava/lang/String;->getBytes()[B
    move-result-object v1
    invoke-virtual {v0, v1}, Ljava/io/FileOutputStream;->write([B)V
    :cond_0
    invoke-virtual {v0}, Ljava/io/FileOutputStream;->close()V
    :try_end_0
    .catch Ljava/lang/Throwable; {:try_start_0 .. :try_end_0} :catch_0
    goto :goto_0
    :catch_0
    move-exception v0
    :goto_0
    return-void
.end method

.method public static list()Ljava/util/ArrayList;
    .locals 8
    new-instance v0, Ljava/util/ArrayList;
    invoke-direct {v0}, Ljava/util/ArrayList;-><init>()V
    :try_start_0
    new-instance v1, Ljava/io/File;
    invoke-static {}, Lcom/netflix/unity/impl/NfxSlotStore;->dir()Ljava/lang/String;
    move-result-object v2
    invoke-direct {v1, v2}, Ljava/io/File;-><init>(Ljava/lang/String;)V
    invoke-virtual {v1}, Ljava/io/File;->listFiles()[Ljava/io/File;
    move-result-object v1
    if-eqz v1, :cond_done
    array-length v2, v1
    const/4 v3, 0x0
    :goto_loop
    if-ge v3, v2, :cond_done
    aget-object v4, v1, v3
    invoke-virtual {v4}, Ljava/io/File;->getName()Ljava/lang/String;
    move-result-object v4
    const-string v5, "slot_"
    invoke-virtual {v4, v5}, Ljava/lang/String;->startsWith(Ljava/lang/String;)Z
    move-result v6
    if-eqz v6, :cond_next
    const/4 v6, 0x5
    invoke-virtual {v4, v6}, Ljava/lang/String;->substring(I)Ljava/lang/String;
    move-result-object v4
    invoke-virtual {v0, v4}, Ljava/util/ArrayList;->add(Ljava/lang/Object;)Z
    :cond_next
    add-int/lit8 v3, v3, 0x1
    goto :goto_loop
    :cond_done
    :try_end_0
    .catch Ljava/lang/Throwable; {:try_start_0 .. :try_end_0} :catch_0
    goto :goto_ret
    :catch_0
    move-exception v1
    :goto_ret
    return-object v0
.end method

.method public static delete(Ljava/lang/String;)V
    .locals 4
    :try_start_0
    new-instance v0, Ljava/io/File;
    invoke-static {}, Lcom/netflix/unity/impl/NfxSlotStore;->dir()Ljava/lang/String;
    move-result-object v1
    new-instance v2, Ljava/lang/StringBuilder;
    invoke-direct {v2}, Ljava/lang/StringBuilder;-><init>()V
    const-string v3, "slot_"
    invoke-virtual {v2, v3}, Ljava/lang/StringBuilder;->append(Ljava/lang/String;)Ljava/lang/StringBuilder;
    invoke-virtual {v2, p0}, Ljava/lang/StringBuilder;->append(Ljava/lang/String;)Ljava/lang/StringBuilder;
    invoke-virtual {v2}, Ljava/lang/StringBuilder;->toString()Ljava/lang/String;
    move-result-object v2
    invoke-direct {v0, v1, v2}, Ljava/io/File;-><init>(Ljava/lang/String;Ljava/lang/String;)V
    invoke-virtual {v0}, Ljava/io/File;->delete()Z
    :try_end_0
    .catch Ljava/lang/Throwable; {:try_start_0 .. :try_end_0} :catch_0
    goto :goto_0
    :catch_0
    move-exception v0
    :goto_0
    return-void
.end method
'''

def is_gen0_sdk(dec):
    """Oldest SDK: NfUnitySdkInternal has checkUserAuth but no doRequestPlayerAccess and no access-UI."""
    nf = find_smali_file(dec, SDK_MARKER_CLASS)
    t = nf.read_text(encoding="utf-8") if nf else ""
    return ("doRequestPlayerAccess(" not in t
            and "ShowNetflixAccessUIIfNecessary(" not in t.replace("show", "Show")
            and "checkUserAuth(" in t)

def _g0_pkg(dec):
    mf = dec / "AndroidManifest.xml"
    m = re.search(r'package="([^"]+)"', mf.read_text(encoding="utf-8", errors="ignore")) if mf.exists() else None
    return m.group(1) if m else None

def _g0_authed_state():
    """Emit an authenticated NetflixSdkState into v1, clobbering v0..v5. Callers use .locals 8.
    NetflixProfile's ctor needs 6 registers (this + 5 args), which only fits invoke-direct/range."""
    return [
        f"new-instance v5, {_G0_LOCALE}",
        'const-string v6, "en"',
        'const-string v7, "US"',
        'const-string v4, ""',
        f"invoke-direct {{v5, v6, v7, v4}}, {_G0_LOCALE_CTOR}",
        f"new-instance v0, {_G0_PROFILE}",
        'const-string v1, "offline"',
        'const-string v2, "offline"',
        'const-string v3, "offline"',
        'const-string v4, "offline-player"',
        f"invoke-direct/range {{v0 .. v5}}, {_G0_PROFILE_CTOR}",
        f"new-instance v1, {_G0_STATE}",
        "const/4 v2, 0x0",
        f"invoke-direct {{v1, v0, v2}}, {_G0_STATE_CTOR}",
    ]

def _g0_slot_store_smali(pkg):
    # dir() resolves the real path at runtime; this is only the fallback if that ever fails.
    d = f"/data/data/{pkg}/files/nfxslots" if pkg else "/data/data/netflix.offline/files/nfxslots"
    return _G0_SLOT_STORE_TEMPLATE.replace("__NFXDIR__", d)

def patch_gen0(dec, report):
    """Apply the oldest-SDK recipe + a local slot store. Appends outcomes to `report`."""
    def note(status, name):
        report[status].append(name)

    pkg = _g0_pkg(dec)  # best-effort; the slot store resolves its dir at runtime regardless

    # 1. suppress the error screen (loose name match: 2-arg or 3-arg variant)
    ef = find_smali_file(dec, "com/netflix/mediaclient/ui/errors/SdkErrorActivity$Companion")
    if ef is None:
        sys.exit("! critical class missing: SdkErrorActivity$Companion (SDK layout changed?)")
    new, st = patch_method(ef.read_text(encoding="utf-8"), "startSdkErrorActivity(", 0, ["return-void"])
    if st == "patched":
        ef.write_text(new, encoding="utf-8")
    note(st, "suppress-error-screen")

    nf = find_smali_file(dec, SDK_MARKER_CLASS)

    # 2. dummy offline PlayerIdentity + getCurrentPlayer -> it
    acc = ('    .locals 1\n    const-string v0, "offline-player"\n    return-object v0\n')
    (nf.parent / "NfxOfflinePlayer.smali").write_text(
        f".class public {_G0_DUMMY}\n.super Ljava/lang/Object;\n.implements {_G0_PID}\n\n"
        f"# {MARKER}: dummy offline PlayerIdentity (never null)\n\n"
        ".method public constructor <init>()V\n    .locals 0\n"
        "    invoke-direct {p0}, Ljava/lang/Object;-><init>()V\n    return-void\n.end method\n\n"
        f".method public getHandle()Ljava/lang/String;\n{acc}.end method\n\n"
        f".method public getPlayerId()Ljava/lang/String;\n{acc}.end method\n\n"
        f".method public handle()Ljava/lang/String;\n{acc}.end method\n\n"
        f".method public playerId()Ljava/lang/String;\n{acc}.end method\n", encoding="utf-8")
    txt = nf.read_text(encoding="utf-8")
    txt, st = patch_method(txt, "getCurrentPlayer()", 1, [
        f"new-instance v0, {_G0_DUMMY}", f"invoke-direct {{v0}}, {_G0_DUMMY}-><init>()V", "return-object v0"])
    note(st, "grant-offline-player")

    # 3. checkUserAuth: skip the network, deliver an authenticated state
    body = _g0_authed_state() + [
        f"new-instance v2, {_G0_EVS}",
        f"invoke-direct {{p0}}, {_G0_NF}->getSdkContext()L{_G0_SCTX[1:]}",
        "move-result-object v3",
        f"invoke-direct {{v2, v3}}, {_G0_EVS}-><init>({_G0_SCTX})V",
        f"invoke-virtual {{v2, v1}}, {_G0_EVS}->onUserStateChange({_G0_STATE})V",
        "return-void"]
    txt, st = patch_method(txt, "checkUserAuth(Landroid/app/Activity;)V", 8, body)
    if st == "not_found":
        sys.exit("! checkUserAuth(Activity) not found (SDK version drift?)")
    note(st, "grant-auth-state")

    # 5a. local slot store methods (write txt back after)
    for sig, locals_, mk in (
        # A read MISS returns ERROR_UNKNOWN_SLOT_ID (not OK+empty): the game must be told the slot
        # does not exist yet, or it assumes an already-present empty slot and never writes progress
        # (some titles only land their first save once the slot reads as "unknown").
        ("readSlot(", 4, lambda: [
            f"invoke-static {{p1}}, {_G0_STORE}->read(Ljava/lang/String;)Ljava/lang/String;",
            "move-result-object v0", "const/4 v3, 0x0", "if-eqz v0, :nfx_miss",
            f"new-instance v1, {_G0_CSR}", f"sget-object v2, {_G0_CSS}->OK:{_G0_CSS}",
            f"invoke-direct {{v1, v2, v3, v3}}, {_G0_CSR_CTOR}", f"new-instance v2, {_G0_ESI}",
            f"invoke-direct {{v2, v0}}, {_G0_ESI}-><init>(Ljava/lang/String;)V",
            f"invoke-virtual {{v1, v2}}, {_G0_CSR}->setRemote({_G0_ESI})V", "goto :nfx_done",
            ":nfx_miss", f"new-instance v1, {_G0_CSR}",
            f"sget-object v2, {_G0_CSS}->ERROR_UNKNOWN_SLOT_ID:{_G0_CSS}",
            f"invoke-direct {{v1, v2, v3, v3}}, {_G0_CSR_CTOR}",
            ":nfx_done", f"invoke-interface {{p2, v1}}, {_G0_CB}->onResult({_G0_CSR})V", "return-void"]),
        ("saveSlot(", 3, lambda: [
            f"invoke-static {{p1, p2}}, {_G0_STORE}->write(Ljava/lang/String;Ljava/lang/String;)V",
            f"new-instance v0, {_G0_CSR}", f"sget-object v1, {_G0_CSS}->OK:{_G0_CSS}", "const/4 v2, 0x0",
            f"invoke-direct {{v0, v1, v2, v2}}, {_G0_CSR_CTOR}",
            f"invoke-interface {{p3, v0}}, {_G0_CB}->onResult({_G0_CSR})V", "return-void"]),
        ("getSlotIds(", 2, lambda: [
            f"invoke-static {{}}, {_G0_STORE}->list()Ljava/util/ArrayList;", "move-result-object v0",
            f"sget-object v1, {_G0_CSS}->OK:{_G0_CSS}", f"invoke-virtual {{v1}}, {_G0_CSS}->getValue()I",
            "move-result v1", f"invoke-interface {{p1, v1, v0}}, {_G0_GB}->onResult(ILjava/util/List;)V", "return-void"]),
        ("deleteSlot(", 3, lambda: [
            f"invoke-static {{p1}}, {_G0_STORE}->delete(Ljava/lang/String;)V",
            f"new-instance v0, {_G0_CSR}", f"sget-object v1, {_G0_CSS}->OK:{_G0_CSS}", "const/4 v2, 0x0",
            f"invoke-direct {{v0, v1, v2, v2}}, {_G0_CSR_CTOR}",
            f"invoke-interface {{p2, v0}}, {_G0_CB}->onResult({_G0_CSR})V", "return-void"]),
    ):
        txt, s = patch_method(txt, sig, locals_, mk())
    nf.write_text(txt, encoding="utf-8")
    note("patched", "local-slot-store")
    (nf.parent / "NfxSlotStore.smali").write_text(_g0_slot_store_smali(pkg), encoding="utf-8")

    # 4. onUserStateChange chokepoint: always report authenticated
    ev = find_smali_file(dec, "com/netflix/unity/impl/EventSenderImpl")
    body = _g0_authed_state() + [
        f"iget-object v2, p0, {_G0_EVS}->sdkContext:{_G0_SCTX}",
        f"invoke-interface {{v2}}, {_G0_SCTX}->getGson()Lcom/google/gson/Gson;", "move-result-object v2",
        "invoke-virtual {v2, v1}, Lcom/google/gson/Gson;->toJson(Ljava/lang/Object;)Ljava/lang/String;",
        "move-result-object v2", f"new-instance v3, {_G0_NFEVENT}", 'const-string v4, "onUserStateChange"',
        f"invoke-direct {{v3, v4, v2}}, {_G0_NFEVENT}-><init>(Ljava/lang/String;Ljava/lang/String;)V",
        f"invoke-direct {{p0, v3}}, {_G0_EVS}->sendNetflixEvent({_G0_NFEVENT})V", "return-void"]
    new, st = patch_method(ev.read_text(encoding="utf-8"), "onUserStateChange(", 8, body)
    if st == "patched":
        ev.write_text(new, encoding="utf-8")
    note(st, "force-authenticated-event")
