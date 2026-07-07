"""GameMaker (libyoyo) titles: no Unity bridge, so the game-authored GameMaker extension
class is the glue. Handles all three shapes seen: the oldest-SDK Nfxa* glue, the
oldest-SDK NetflixWrapper glue, and the gen-2 com.netflix.games glue."""
import re
import sys
from .smali import patch_method, find_smali_file, _PAI, _RES, _ERR
from .gen0 import _g0_slot_store_smali, _g0_authed_state
from .. import MARKER
from .gen0 import _G0_STATE, _G0_CSS, _G0_CONF


_GM_EVH = "Lcom/netflix/android/api/events/NetflixSdkEventHandler;"

_GM_SLOTINFO = "Lcom/netflix/android/api/cloudsave/SlotInfo;"

_GM_READRES = "Lcom/netflix/android/api/cloudsave/CloudSave$ReadSlotResult;"

_GM_SAVERES = "Lcom/netflix/android/api/cloudsave/CloudSave$SaveSlotResult;"

_GM_IDSRES = "Lcom/netflix/android/api/cloudsave/CloudSave$GetSlotIdsResult;"

_GM_DELRES = "Lcom/netflix/android/api/cloudsave/CloudSave$DeleteSlotResult;"

_ONE_D = "0x3ff0000000000000L"  # double 1.0, the Nfxa* success-request return

def _gm_find_glue(dec):
    """The GameMaker<->Netflix glue class file (has the Nfxa* extension methods GML calls)."""
    for d in sorted(dec.glob("smali*")):
        for f in d.rglob("*.smali"):
            try:
                t = f.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            if "NfxaCheckUserAuth(" in t and "NfxaCloudRead(" in t:
                return f
    return None

def is_gamemaker_sdk(dec):
    """GameMaker (RunnerJNILib) game on the oldest SDK with no Unity bridge - the glue is a game
    class exposing Nfxa* methods and implementing NetflixSdkEventHandler."""
    return (find_smali_file(dec, "com/yoyogames/runner/RunnerJNILib") is not None
            and find_smali_file(dec, "com/netflix/android/api/netflixsdk/NetflixSdk") is not None
            and find_smali_file(dec, "com/netflix/android/api/cloudsave/CloudSave") is not None
            and _gm_find_glue(dec) is not None)

def _full_method(header, locals_, body):
    return "\n".join([header, f"    .locals {locals_}", f"    # {MARKER}"]
                     + ["    " + b if b else "" for b in body] + [".end method"])

def _gm_replace_method(text, sig, new_method):
    lines = text.split("\n")
    start = next((i for i, ln in enumerate(lines)
                  if ln.strip().startswith(".method") and sig in ln), None)
    if start is None:
        return text, "not_found"
    end = next(j for j in range(start + 1, len(lines)) if lines[j].strip() == ".end method")
    return "\n".join(lines[:start] + new_method.split("\n") + lines[end + 1:]), "patched"

def _gm_callback(method_text):
    """(callback_class_desc, takes_slot) for the inner-class result callback a cloud method builds."""
    ni = re.findall(r"new-instance [vp]\d+, (L[\w/$]+\$\d+;)", method_text)
    if not ni:
        return None, False
    cb = ni[-1]
    m = re.search(re.escape(cb) + r"-><init>\(([^)]*)\)V", method_text)
    return cb, ("Ljava/lang/String;" in (m.group(1) if m else ""))

def _gm_cb_new(cb, glue, takes_slot):
    ctor = f"{glue}Ljava/lang/String;" if takes_slot else glue
    args = "{v5, p0, p1}" if takes_slot else "{v5, p0}"
    return [f"new-instance v5, {cb}", f"invoke-direct {args}, {cb}-><init>({ctor})V"]

def patch_gamemaker(dec, report):
    """Patch a GameMaker Netflix game's glue class: offline auth + a local cloud-save slot store."""
    def note(status, name):
        report[status].append(name)

    glue_f = _gm_find_glue(dec)
    smdir = next(d for d in dec.glob("smali*") if glue_f.is_relative_to(d))
    glue = "L" + str(glue_f.relative_to(smdir).with_suffix("")).replace("\\", "/") + ";"
    glue_pkg = glue[1:-1].rsplit("/", 1)[0]
    store = f"L{glue_pkg}/NfxSlotStore;"
    t = glue_f.read_text(encoding="utf-8")

    # event-receiver field on the glue (type NetflixSdkEventHandler)
    m = re.search(r"\.field[^\n]*?\b(\w+):" + re.escape(_GM_EVH), t)
    if m is None:
        sys.exit("! GameMaker glue has no NetflixSdkEventHandler field (layout changed?)")
    evfield = f"->{m.group(1)}:{_GM_EVH}"

    # 1. auth: deliver a synthetic authenticated state to the glue's own event receiver
    auth = _g0_authed_state() + [
        f"iget-object v0, p0, {glue}{evfield}",
        f"invoke-interface {{v0, v1}}, {_GM_EVH}->onUserStateChange({_G0_STATE})V",
        "return-void"]
    t, st = _gm_replace_method(t, "NfxaCheckUserAuth()",
                               _full_method(".method public NfxaCheckUserAuth()V", 8, auth))
    note(st, "grant-offline-auth")

    # 2. cloud slot methods -> local store + synthesized OK result -> the game's own callbacks
    def cloud(sig, kind):
        nonlocal t
        orig = t[t.index(".method public " + sig):]
        orig = orig[:orig.index(".end method")]
        cb, takes = _gm_callback(orig)
        if cb is None:
            note("not_found", f"cloud:{kind}"); return
        n = _gm_cb_new(cb, glue, takes)
        if kind == "read":
            body = [
                f"invoke-static {{p1}}, {store}->read(Ljava/lang/String;)Ljava/lang/String;",
                "move-result-object v0", "if-eqz v0, :nfx_miss",
                "invoke-virtual {v0}, Ljava/lang/String;->getBytes()[B", "move-result-object v0",
                f"new-instance v1, {_GM_SLOTINFO}", f"invoke-direct {{v1, v0}}, {_GM_SLOTINFO}-><init>([B)V",
                f"sget-object v2, {_G0_CSS}->OK:{_G0_CSS}", "goto :nfx_build",
                ":nfx_miss", "const/4 v0, 0x0",
                f"new-instance v1, {_GM_SLOTINFO}", f"invoke-direct {{v1, v0}}, {_GM_SLOTINFO}-><init>([B)V",
                f"sget-object v2, {_G0_CSS}->ERROR_UNKNOWN_SLOT_ID:{_G0_CSS}",
                ":nfx_build", "const/4 v3, 0x0", f"new-instance v4, {_GM_READRES}",
                f"invoke-direct {{v4, v1, v2, v3, v3}}, {_GM_READRES}-><init>({_GM_SLOTINFO}{_G0_CSS}{_G0_CONF}Ljava/lang/String;)V",
            ] + n + [f"invoke-virtual {{v5, v4}}, {cb}->onResult({_GM_READRES})V",
                     f"const-wide/high16 v0, {_ONE_D}", "return-wide v0"]
            hdr = ".method public NfxaCloudRead(Ljava/lang/String;)D"
        elif kind == "write":
            body = [
                f"invoke-static {{p1, p2}}, {store}->write(Ljava/lang/String;Ljava/lang/String;)V",
                f"sget-object v2, {_G0_CSS}->OK:{_G0_CSS}", "const/4 v3, 0x0",
                f"new-instance v4, {_GM_SAVERES}",
                f"invoke-direct {{v4, v2, v3, v3}}, {_GM_SAVERES}-><init>({_G0_CSS}{_G0_CONF}Ljava/lang/String;)V",
            ] + n + [f"invoke-virtual {{v5, v4}}, {cb}->onResult({_GM_SAVERES})V",
                     f"const-wide/high16 v0, {_ONE_D}", "return-wide v0"]
            hdr = ".method public NfxaCloudWrite(Ljava/lang/String;Ljava/lang/String;)D"
        elif kind == "ids":
            body = [
                f"invoke-static {{}}, {store}->list()Ljava/util/ArrayList;", "move-result-object v0",
                f"sget-object v2, {_G0_CSS}->OK:{_G0_CSS}", "const/4 v3, 0x0",
                f"new-instance v4, {_GM_IDSRES}",
                f"invoke-direct {{v4, v0, v2, v3}}, {_GM_IDSRES}-><init>(Ljava/util/List;{_G0_CSS}Ljava/lang/String;)V",
            ] + n + [f"invoke-virtual {{v5, v4}}, {cb}->onResult({_GM_IDSRES})V",
                     f"const-wide/high16 v0, {_ONE_D}", "return-wide v0"]
            hdr = ".method public NfxaCloudGetSlotIDs()D"
        else:  # delete
            body = [
                f"invoke-static {{p1}}, {store}->delete(Ljava/lang/String;)V",
                f"sget-object v2, {_G0_CSS}->OK:{_G0_CSS}", "const/4 v3, 0x0",
                f"new-instance v4, {_GM_DELRES}",
                f"invoke-direct {{v4, v2, v3, v3}}, {_GM_DELRES}-><init>({_G0_CSS}{_G0_CONF}Ljava/lang/String;)V",
            ] + n + [f"invoke-virtual {{v5, v4}}, {cb}->onResult({_GM_DELRES})V",
                     f"const-wide/high16 v0, {_ONE_D}", "return-wide v0"]
            hdr = ".method public NfxaCloudDelete(Ljava/lang/String;)D"
        t, s = _gm_replace_method(t, sig, _full_method(hdr, 6, body))
        note(s, f"cloud:{kind}")

    cloud("NfxaCloudRead(", "read")
    cloud("NfxaCloudWrite(", "write")
    cloud("NfxaCloudGetSlotIDs(", "ids")
    cloud("NfxaCloudDelete(", "delete")
    glue_f.write_text(t, encoding="utf-8")

    store_internal = f"{glue_pkg}/NfxSlotStore"
    (glue_f.parent / "NfxSlotStore.smali").write_text(
        _g0_slot_store_smali(None).replace("com/netflix/unity/impl/NfxSlotStore", store_internal),
        encoding="utf-8")
    note("patched", "local-slot-store")

    # 3. suppress the error screen (belt-and-suspenders)
    ef = find_smali_file(dec, "com/netflix/mediaclient/ui/errors/SdkErrorActivity$Companion")
    if ef is not None:
        new, st = patch_method(ef.read_text(encoding="utf-8"), "startSdkErrorActivity(", 0, ["return-void"])
        if st == "patched":
            ef.write_text(new, encoding="utf-8")
        note(st, "suppress-error-screen")

def _gm0w_find_glue(dec):
    """GameMaker gen-0 glue of the 'NetflixWrapper' shape: builds the user-state as a DsMap async
    event and exposes NetflixCheckUserAuth (Netflix* methods)."""
    for d in sorted(dec.glob("smali*")):
        for f in d.rglob("*.smali"):
            try:
                t = f.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            if ("handleUserStateChange(" in t and "CreateAsynEventWithDSMap(" in t
                    and "NetflixCheckUserAuth(" in t):
                return f
    return None

def is_gamemaker_gen0_wrapper(dec):
    """GameMaker + gen-0 SDK, 'NetflixWrapper' shape - auth-only (no Nfxa*/cloud-save glue)."""
    return (find_smali_file(dec, "com/yoyogames/runner/RunnerJNILib") is not None
            and find_smali_file(dec, "com/netflix/android/api/netflixsdk/NetflixSdk") is not None
            and _gm_find_glue(dec) is None
            and _gm0w_find_glue(dec) is not None)

def patch_gamemaker_gen0_wrapper(dec, report):
    """Force a synthetic signed-in DsMap user-state event, skip the network. No cloud-save."""
    def note(status, name):
        report[status].append(name)

    jni = "Lcom/yoyogames/runner/RunnerJNILib;"
    glue_f = _gm0w_find_glue(dec)
    smdir = next(d for d in dec.glob("smali*") if glue_f.is_relative_to(d))
    glue = "L" + str(glue_f.relative_to(smdir).with_suffix("")).replace("\\", "/") + ";"
    t = glue_f.read_text(encoding="utf-8")

    m = re.search(r"handleUserStateChange\((L[\w/$]+;)\)V", t)
    if m is None:
        sys.exit("! GameMaker gen-0 wrapper: handleUserStateChange signature not found")
    state = m.group(1)

    def add(k, v):
        return [f'const-string v1, "{k}"', f'const-string v2, "{v}"',
                f"invoke-static {{v0, v1, v2}}, {jni}->DsMapAddString(ILjava/lang/String;Ljava/lang/String;)V"]

    signed_in = [
        "const/4 v0, 0x0",
        f"invoke-static {{v0, v0, v0}}, {jni}->jCreateDsMap([Ljava/lang/String;[Ljava/lang/String;[D)I",
        "move-result v0",
        *add("id", "netflixUserStateChange"),
        *add("userChanged", "true"),
        *add("userLoginId", "offline-player"),
        *add("userAccessToken", "offline"),
        *add("language", "en"),
        *add("country", "US"),
        *add("variant", ""),
        *add("userGamerName", ""),
        "const/16 v1, 0x46",
        f"invoke-static {{v0, v1}}, {jni}->CreateAsynEventWithDSMap(II)V",
        "return-void",
    ]
    check_auth = [
        "const/4 v0, 0x0",
        f"invoke-direct {{p0, v0}}, {glue}->handleUserStateChange({state})V",
        "const-wide/16 v0, 0x0",
        "return-wide v0",
    ]
    t, s1 = patch_method(t, f"handleUserStateChange({state})V", 3, signed_in)
    t, s2 = patch_method(t, "NetflixCheckUserAuth()D", 2, check_auth)
    if "patched" in (s1, s2):
        glue_f.write_text(t, encoding="utf-8")
    note(s1, "force-signed-in")
    note(s2, "grant-auth")

    ef = find_smali_file(dec, "com/netflix/mediaclient/ui/errors/SdkErrorActivity$Companion")
    if ef is not None:
        new, st = patch_method(ef.read_text(encoding="utf-8"), "startSdkErrorActivity(", 0, ["return-void"])
        if st == "patched":
            ef.write_text(new, encoding="utf-8")
        note(st, "suppress-error-screen")

_GM2_PROF = "com/netflix/games/player/profiles"

_GM2_READRES = "Lcom/netflix/games/storage/blobs/ReadPlayerBlobResult;"

_GM2_WRITERES = "Lcom/netflix/games/storage/blobs/WritePlayerBlobResult;"

_GM2_BLOBC = "Lcom/netflix/games/storage/blobs/BlobContainer;"

_GM2_CONF = "Lcom/netflix/games/storage/blobs/Conflict;"

_GM2_NOTFOUND = "Lcom/netflix/games/errors/ErrorCodes$BlobStore;->BLOB_NAME_NOT_FOUND:I"

_GM2_DELRES = "Lcom/netflix/games/storage/blobs/DeletePlayerBlobResult;"

_GM2_OFFLINE_PROFILE = """.class public final Lcom/netflix/games/player/profiles/OfflineCurrentProfile;
.super Lcom/netflix/games/player/profiles/CurrentProfile;
.source "OfflineCurrentProfile.java"

# %MARK% offline profile (CurrentProfile is abstract)

.method public constructor <init>()V
    .locals 3
    const-string v0, "offline-player"
    const-string v1, "Offline"
    const-string v2, "en"
    invoke-direct {p0, v0, v1, v2}, Lcom/netflix/games/player/profiles/CurrentProfile;-><init>(Ljava/lang/String;Ljava/lang/String;Ljava/lang/String;)V
    return-void
.end method

.method public getLegacyGamerAccessToken()Lcom/netflix/games/NetflixResult;
    .locals 2
    sget-object v0, Lcom/netflix/games/NetflixResult;->Companion:Lcom/netflix/games/NetflixResult$Companion;
    const-string v1, "offline-player"
    invoke-virtual {v0, v1}, Lcom/netflix/games/NetflixResult$Companion;->withData(Ljava/lang/Object;)Lcom/netflix/games/NetflixResult;
    move-result-object v0
    return-object v0
.end method

.method public getLegacyGamerProfileId()Lcom/netflix/games/NetflixResult;
    .locals 2
    sget-object v0, Lcom/netflix/games/NetflixResult;->Companion:Lcom/netflix/games/NetflixResult$Companion;
    const-string v1, "offline-player"
    invoke-virtual {v0, v1}, Lcom/netflix/games/NetflixResult$Companion;->withData(Ljava/lang/Object;)Lcom/netflix/games/NetflixResult;
    move-result-object v0
    return-object v0
.end method
""".replace("%MARK%", MARKER)

_GM2_GETCURRENTPROFILE = """.method public getCurrentProfile()Lcom/netflix/games/NetflixResult;
    .locals 3
    .annotation system Ldalvik/annotation/Signature;
        value = {
            "()",
            "Lcom/netflix/games/NetflixResult<",
            "Lcom/netflix/games/player/profiles/CurrentProfile;",
            ">;"
        }
    .end annotation

    # %MARK% offline profile
    new-instance v0, Lcom/netflix/games/player/profiles/OfflineCurrentProfile;
    invoke-direct {v0}, Lcom/netflix/games/player/profiles/OfflineCurrentProfile;-><init>()V
    new-instance v1, Lcom/netflix/games/NetflixResult;
    const/4 v2, 0x0
    invoke-direct {v1, v0, v2}, Lcom/netflix/games/NetflixResult;-><init>(Ljava/lang/Object;Lcom/netflix/games/Error;)V
    return-object v1
.end method""".replace("%MARK%", MARKER)

def _gm2_find_glue(dec):
    """gen-2 GameMaker glue: exposes NfxaShowUI + NfxaCloudRead (and NOT the gen-0 NfxaCheckUserAuth)."""
    for d in sorted(dec.glob("smali*")):
        for f in d.rglob("*.smali"):
            try:
                t = f.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            if "NfxaShowUI(" in t and "NfxaCloudRead(" in t and "NfxaCheckUserAuth(" not in t:
                return f
    return None

def is_gamemaker_gen2_sdk(dec):
    return (find_smali_file(dec, "com/yoyogames/runner/RunnerJNILib") is not None
            and find_smali_file(dec, f"{_GM2_PROF}/ProfilesApiImpl") is not None
            and find_smali_file(dec, "com/netflix/games/storage/blobs/BlobStoreApi") is not None
            and _gm2_find_glue(dec) is not None)

def patch_gamemaker_gen2(dec, report):
    def note(status, name):
        report[status].append(name)

    glue_f = _gm2_find_glue(dec)
    smdir = next(d for d in dec.glob("smali*") if glue_f.is_relative_to(d))
    glue = "L" + str(glue_f.relative_to(smdir).with_suffix("")).replace("\\", "/") + ";"
    store = f"L{glue[1:-1].rsplit('/', 1)[0]}/NfxBlobStore;"
    t = glue_f.read_text(encoding="utf-8")

    def orig(sig):
        s = t[t.index(".method public " + sig):]
        return s[:s.index(".end method")]

    # error screen
    ef = find_smali_file(dec, "com/netflix/mediaclient/ui/errors/SdkErrorActivity$Companion")
    if ef is not None:
        new, st = patch_method(ef.read_text(encoding="utf-8"), "startSdkErrorActivity(", 0, ["return-void"])
        if st == "patched":
            ef.write_text(new, encoding="utf-8")
        note(st, "suppress-error-screen")

    # profile -> synthetic offline CurrentProfile
    impl = find_smali_file(dec, f"{_GM2_PROF}/ProfilesApiImpl")
    (impl.parent / "OfflineCurrentProfile.smali").write_text(_GM2_OFFLINE_PROFILE, encoding="utf-8")
    itxt, _ = _gm_replace_method(impl.read_text(encoding="utf-8"), "getCurrentProfile()", _GM2_GETCURRENTPROFILE)
    impl.write_text(itxt, encoding="utf-8")
    note("patched", "offline-profile")

    # access grant: NfxaShowUI -> synthesize a granted result, call the requestPlayerAccess callback
    cb2, _ = _gm_callback(orig("NfxaShowUI()"))
    show = [
        f"new-instance v0, {_PAI}", 'const-string v1, "offline-player"',
        f"invoke-direct {{v0, v1}}, {_PAI}-><init>(Ljava/lang/String;)V",
        f"new-instance v1, {_RES}", "const/4 v2, 0x0",
        f"invoke-direct {{v1, v0, v2}}, {_RES}-><init>(Ljava/lang/Object;{_ERR})V",
        f"new-instance v0, {cb2}", f"invoke-direct {{v0, p0}}, {cb2}-><init>({glue})V",
        f"invoke-virtual {{v0, v1}}, {cb2}->onResult({_RES})V", "return-void"]
    t = _gm_replace_method(t, "NfxaShowUI()", _full_method(".method public NfxaShowUI()V", 3, show))[0]

    # blob cloud-save: local store + synthesized result -> the game's own callbacks
    cbr, _ = _gm_callback(orig("NfxaCloudRead("))
    read = [
        f"invoke-static {{p1}}, {store}->read(Ljava/lang/String;)Ljava/lang/String;", "move-result-object v0",
        "if-eqz v0, :nfx_miss", "invoke-virtual {v0}, Ljava/lang/String;->getBytes()[B", "move-result-object v0",
        f"new-instance v1, {_GM2_BLOBC}", f"invoke-direct {{v1, v0}}, {_GM2_BLOBC}-><init>([B)V",
        "const/4 v2, 0x0", f"new-instance v3, {_GM2_READRES}",
        f"invoke-direct {{v3, v1, v2}}, {_GM2_READRES}-><init>({_GM2_BLOBC}{_GM2_CONF})V",
        f"new-instance v0, {_RES}", f"invoke-direct {{v0, v3, v2}}, {_RES}-><init>(Ljava/lang/Object;{_ERR})V",
        "goto :nfx_deliver", ":nfx_miss",
        f"new-instance v1, {_ERR}", f"sget v2, {_GM2_NOTFOUND}", 'const-string v3, "not found"',
        f"invoke-direct {{v1, v2, v3}}, {_ERR}-><init>(ILjava/lang/String;)V",
        f"new-instance v0, {_RES}", "const/4 v2, 0x0",
        f"invoke-direct {{v0, v2, v1}}, {_RES}-><init>(Ljava/lang/Object;{_ERR})V", ":nfx_deliver",
        f"new-instance v1, {cbr}", f"invoke-direct {{v1, p0, p1}}, {cbr}-><init>({glue}Ljava/lang/String;)V",
        f"invoke-virtual {{v1, v0}}, {cbr}->onResult({_RES})V",
        "const-wide/high16 v0, 0x3ff0000000000000L", "return-wide v0"]
    t = _gm_replace_method(t, "NfxaCloudRead(", _full_method(".method public NfxaCloudRead(Ljava/lang/String;)D", 5, read))[0]

    cbw, _ = _gm_callback(orig("NfxaCloudWrite("))
    write = [
        f"invoke-static {{p1, p2}}, {store}->write(Ljava/lang/String;Ljava/lang/String;)V",
        f"new-instance v0, {_GM2_WRITERES}", "const/4 v1, 0x0",
        f"invoke-direct {{v0, v1}}, {_GM2_WRITERES}-><init>({_GM2_CONF})V",
        f"new-instance v2, {_RES}", f"invoke-direct {{v2, v0, v1}}, {_RES}-><init>(Ljava/lang/Object;{_ERR})V",
        f"new-instance v3, {cbw}", f"invoke-direct {{v3, p0, p1}}, {cbw}-><init>({glue}Ljava/lang/String;)V",
        f"invoke-virtual {{v3, v2}}, {cbw}->onResult({_RES})V",
        "const-wide/high16 v0, 0x3ff0000000000000L", "return-wide v0"]
    t = _gm_replace_method(t, "NfxaCloudWrite(", _full_method(".method public NfxaCloudWrite(Ljava/lang/String;Ljava/lang/String;)D", 4, write))[0]

    glue_f.write_text(t, encoding="utf-8")
    note("patched", "grant-offline-auth")
    note("patched", "local-blob-store")

    (glue_f.parent / "NfxBlobStore.smali").write_text(
        _g0_slot_store_smali(None).replace("com/netflix/unity/impl/NfxSlotStore", store[1:-1]),
        encoding="utf-8")
