#!/usr/bin/env python3
"""
Strip the Netflix Games SDK out of a Netflix game APK so it runs offline with no account.

Point it at a .apk, .apkm, or .xapk. You get back a signed APK you can sideload.

The login check lives in the Netflix SDK, not the game. Every title bundles the same SDK
in classes.dex and calls into it over JNI, so one set of smali patches works on any of
them, Unity or native.

It only removes the Netflix gate. If a game has its own dead backend (a game server, a
Firebase Remote Config economy, streamed content), it boots past the Netflix screen and
then stalls. The README covers which games come back clean.

Usage:
    python unlock_netflix.py INPUT [-o OUT] [--keep-cloud-save] [--no-sign] [--keep-work]

Tool paths come from CLI flags, then config.local.json, then env vars, then autodetect off
ANDROID_HOME/ANDROID_SDK_ROOT, JAVA_HOME, and PATH.
"""
import argparse, json, os, re, shutil, subprocess, sys, tempfile, zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
MARKER = "netflix-offline-patcher"

# Netflix SDK types the patched bodies reference. These names hold across SDK versions.
_PAI = "Lcom/netflix/games/player/access/PlayerAccessInfo;"
_RES = "Lcom/netflix/games/NetflixResult;"
_ERR = "Lcom/netflix/games/Error;"
_CB  = "Lcom/netflix/games/Callback;"
_RES_CTOR = f"{_RES}-><init>(Ljava/lang/Object;{_ERR})V"
_ONRESULT = f"{_CB}->onResult({_RES})V"

# The gate, written as method-body rewrites. We match the .method line loosely, so a
# changed access modifier in a newer SDK still gets caught.
PATCHES = [
    {   # the login wall. hand back a granted result and skip the dead handshake.
        "name": "grant-player-access",
        "class": "com/netflix/unity/impl/NfUnitySdkInternal",
        "sig": "doRequestPlayerAccess(Lcom/netflix/games/Callback;)V",
        "critical": True, "locals": 3,
        "body": [
            "# pretend the player is a signed-in member",
            f"new-instance v0, {_PAI}",
            'const-string v1, "offline-player"',
            f"invoke-direct {{v0, v1}}, {_PAI}-><init>(Ljava/lang/String;)V",
            f"new-instance v1, {_RES}",
            "const/4 v2, 0x0",
            f"invoke-direct {{v1, v0, v2}}, {_RES_CTOR}",
            f"invoke-interface {{p1, v1}}, {_ONRESULT}",
            "return-void",
        ],
    },
    {   # the "Something went wrong" dead-end. every fatal path ends here, so kill it.
        "name": "suppress-error-screen",
        "class": "com/netflix/mediaclient/ui/errors/SdkErrorActivity$Companion",
        "sig": "startSdkErrorActivity(Landroid/content/Context;Landroid/os/Bundle;Ljava/lang/String;)V",
        "critical": True, "locals": 0,
        "body": ["return-void"],
    },
    {   # cloud-save read. no server, so say "no blob" right away instead of waiting forever.
        "name": "cloud-save-read-offline",
        "class": "com/netflix/unity/impl/NfUnitySdkInternal",
        "sig": "readBlob(Ljava/lang/String;Lcom/netflix/games/Callback;)V",
        "critical": False, "cloud_save": True, "locals": 4,
        "body": [
            "# no cloud blob, so the game starts a fresh local save",
            f"new-instance v0, {_ERR}",
            "const/16 v1, 0x194",
            'const-string v2, "offline"',
            f"invoke-direct {{v0, v1, v2}}, {_ERR}-><init>(ILjava/lang/String;)V",
            f"new-instance v1, {_RES}",
            "const/4 v3, 0x0",
            f"invoke-direct {{v1, v3, v0}}, {_RES_CTOR}",
            f"invoke-interface {{p2, v1}}, {_ONRESULT}",
            "return-void",
        ],
    },
    {   # cloud-save list. return nothing, there are no saved blobs.
        "name": "cloud-save-list-empty",
        "class": "com/netflix/unity/impl/NfUnitySdkInternal",
        "sig": "getBlobs(Lcom/netflix/games/Callback;)V",
        "critical": False, "cloud_save": True, "locals": 3,
        "body": [
            "new-instance v0, Ljava/util/ArrayList;",
            "invoke-direct {v0}, Ljava/util/ArrayList;-><init>()V",
            f"new-instance v1, {_RES}",
            "const/4 v2, 0x0",
            f"invoke-direct {{v1, v0, v2}}, {_RES_CTOR}",
            f"invoke-interface {{p1, v1}}, {_ONRESULT}",
            "return-void",
        ],
    },
    {   # cloud-save write. skip the dead server, it never calls back. the local save is what counts.
        "name": "cloud-save-write-offline",
        "class": "com/netflix/unity/impl/NfUnitySdkInternal",
        "sig": "writeBlob(Ljava/lang/String;Ljava/lang/String;Lcom/netflix/games/Callback;)V",
        "critical": False, "cloud_save": True, "locals": 4,
        "body": [
            f"new-instance v0, {_ERR}",
            "const/16 v1, 0x194",
            'const-string v2, "offline"',
            f"invoke-direct {{v0, v1, v2}}, {_ERR}-><init>(ILjava/lang/String;)V",
            f"new-instance v1, {_RES}",
            "const/4 v3, 0x0",
            f"invoke-direct {{v1, v3, v0}}, {_RES_CTOR}",
            f"invoke-interface {{p3, v1}}, {_ONRESULT}",
            "return-void",
        ],
    },
]

SDK_MARKER_CLASS = "com/netflix/unity/impl/NfUnitySdkInternal"


def patch_method(text, sig, locals_count, body_lines):
    """Replace a smali method body. Keeps .annotation/.param blocks and resets .locals.
    Returns (new_text, status) where status is patched, already, or not_found."""
    lines = text.split("\n")
    start = next((i for i, ln in enumerate(lines)
                  if ln.strip().startswith(".method") and sig in ln), None)
    if start is None:
        return text, "not_found"
    end = next((j for j in range(start + 1, len(lines))
                if lines[j].strip() == ".end method"), None)
    if end is None:
        return text, "not_found"

    inner = lines[start + 1:end]
    if any(MARKER in ln for ln in inner):
        return text, "already"

    # annotation and param blocks sit above the code. keep them as-is.
    preserved, depth = [], 0
    for ln in inner:
        s = ln.strip()
        if s.startswith(".annotation") or s.startswith(".param"):
            depth += 1; preserved.append(ln)
        elif s in (".end annotation", ".end param"):
            preserved.append(ln); depth = max(0, depth - 1)
        elif depth > 0:
            preserved.append(ln)

    rebuilt = [lines[start], f"    .locals {locals_count}"] + preserved
    rebuilt += ["", f"    # {MARKER}"]
    rebuilt += [("    " + b) if b else "" for b in body_lines]
    rebuilt += [".end method"]
    return "\n".join(lines[:start] + rebuilt + lines[end + 1:]), "patched"


def find_smali_file(apktool_dir, class_path):
    """Locate com/.../Foo.smali across smali, smali_classes2..N."""
    rel = class_path + ".smali"
    for d in sorted(apktool_dir.glob("smali*")):
        if (d / rel).exists():
            return d / rel
    return None


# -----------------------------------------------------------------------------
# Older ("legacy") Netflix Games SDK (~2024 titles, e.g. NetflixGames-1.1.0-5).
#
# These have NO doRequestPlayerAccess. The gate is an access-UI + event model:
# NetflixPlatform.Init() calls showNetflixAccessUIIfNecessary() and waits on a promise that
# only resolves once the access UI is DISMISSED (an onNetflixUiHidden event); it then reads
# ProfilesApi.getCurrentProfile() synchronously and needs a real profile. Offline the access
# UI is the dead-end error screen, which never dismisses, so the game hangs on its splash.
#
# Three smali patches (impl classes are obfuscated, so we discover them by their SDK super/
# interface types, which are not obfuscated):
#   1. SdkErrorActivity$Companion.startSdkErrorActivity(...) -> return-void  (2-arg variant).
#   2. <ProfilesApi impl>.getCurrentProfile() -> a synthetic offline CurrentProfile. Its
#      concrete subclass ctor wants a non-null LegacyProfileFields, so we generate a dummy.
#   3. NfUnitySdkInternal.doShowNetflixAccessUIIfNecessary() -> real call, then once fire via
#      EventSenderImpl: onPlayerAccessChanged(granted) + onNetflixUiShown() + onNetflixUiHidden().
# -----------------------------------------------------------------------------
_PROFILES_API = "com/netflix/games/player/profiles/ProfilesApi"
_CURRENT_PROFILE = "com/netflix/games/player/profiles/CurrentProfile"
_COMP = "Lcom/netflix/games/NetflixResult$Companion;"


def is_legacy_sdk(dec):
    """Older access-UI SDK = NfUnitySdkInternal has no doRequestPlayerAccess but does the UI call."""
    nf = find_smali_file(dec, SDK_MARKER_CLASS)
    t = nf.read_text(encoding="utf-8") if nf else ""
    return ("doRequestPlayerAccess(" not in t) and ("ShowNetflixAccessUIIfNecessary(" in t.replace("show", "Show"))


def _decl_file(dec, typ, kind):
    """First smali whose header declares `.{kind} L{typ};` (kind = 'implements' or 'super')."""
    needle = f".{kind} L{typ};"
    for d in sorted(dec.glob("smali*")):
        for f in d.rglob("*.smali"):
            try:
                with open(f, encoding="utf-8", errors="ignore") as fh:
                    for _ in range(15):
                        ln = fh.readline()
                        if not ln:
                            break
                        if needle in ln:
                            return f
            except OSError:
                pass
    return None


def _class_of(smali_file):
    for ln in smali_file.read_text(encoding="utf-8").split("\n"):
        if ln.startswith(".class"):
            return ln.split()[-1]  # e.g. Lcom/.../diff;
    return None


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


# -----------------------------------------------------------------------------
# Oldest ("gen-0") Netflix Games SDK (~2022-2023 titles, com.netflix.android.api /
# com.netflix.unity). No doRequestPlayerAccess and no access-UI. Auth is:
#   game calls NfUnitySdkInternal.checkUserAuth(activity) -> network handshake, whose
#   result arrives as EventSenderImpl.onUserStateChange(NetflixSdkState) pushed to Unity
#   (a non-null currentProfile == signed in). getCurrentPlayer() -> PlayerIdentity too
#   (null == signed out). Offline the handshake fails -> SdkErrorActivity / hang.
# Progress is stored ONLY in the Netflix cloud-save SLOT api (saveSlot/readSlot/getSlotIds),
# which is dead offline, so without a local store the game restarts fresh every launch.
#
# Five smali patches + a generated local slot store:
#   1. SdkErrorActivity$Companion.startSdkErrorActivity(...) -> return-void
#   2. getCurrentPlayer() -> a dummy offline PlayerIdentity (never null)
#   3. checkUserAuth(Activity) -> skip the network, deliver a synthetic authenticated state
#   4. EventSenderImpl.onUserStateChange(...) -> always report authenticated (the chokepoint)
#   5. saveSlot/readSlot/getSlotIds/deleteSlot -> a real local file store (progress persists)
# -----------------------------------------------------------------------------
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

# Generated helper: a file-backed replacement for the dead Netflix cloud-save slot server.
# dir() resolves the app's own internal files dir at runtime (package-agnostic) so saves are
# private and persist; __NFXDIR__ is only a fallback if the runtime lookup ever fails.
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
        ("readSlot(", 4, lambda: [
            f"invoke-static {{p1}}, {_G0_STORE}->read(Ljava/lang/String;)Ljava/lang/String;",
            "move-result-object v0", f"new-instance v1, {_G0_CSR}", f"sget-object v2, {_G0_CSS}->OK:{_G0_CSS}",
            "const/4 v3, 0x0", f"invoke-direct {{v1, v2, v3, v3}}, {_G0_CSR_CTOR}",
            "if-eqz v0, :nfx_miss", f"new-instance v2, {_G0_ESI}",
            f"invoke-direct {{v2, v0}}, {_G0_ESI}-><init>(Ljava/lang/String;)V",
            f"invoke-virtual {{v1, v2}}, {_G0_CSR}->setRemote({_G0_ESI})V",
            ":nfx_miss", f"invoke-interface {{p2, v1}}, {_G0_CB}->onResult({_G0_CSR})V", "return-void"]),
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


# -----------------------------------------------------------------------------
# GameMaker (libyoyo) + oldest SDK, no Unity bridge.
#
# Some Netflix titles are GameMaker Studio games, not Unity/native. They carry the same oldest
# SDK (com.netflix.android.api - NetflixProfile/NetflixSdkState/Locale) but have NO Unity bridge
# (NfUnitySdkInternal), so the checks above all miss and the tool would abort. Instead, the
# GameMaker<->Netflix glue is a single game class (a GameMaker extension) that implements
# NetflixSdkEventHandler and exposes Nfxa* methods GML calls; results go back to GML as RunnerJNILib
# async events. We patch that glue (never the DexGuard-flattened NetflixGameSdk):
#   - auth: deliver a synthetic authenticated NetflixSdkState to the glue's event receiver, so its
#     own onUserStateChange fires the game's "signed in" async event. No network, no hardcoded keys.
#   - saves: the game's only progress store is the Netflix cloud-save SLOT api (raw
#     com.netflix.android.api.cloudsave types, not the unity wrapper). Redirect the four Nfxa cloud
#     methods to a local file store (NfxSlotStore) and invoke the game's OWN result callbacks with a
#     synthesized OK result. A read MISS must return ERROR_UNKNOWN_SLOT_ID (not OK+empty), or the game
#     tries to parse empty savedata and errors.
# -----------------------------------------------------------------------------
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


def strip_non_arm_libs(dec):
    """GameMaker's engine (libyoyo.so) ships arm-only; if the x86/x86_64 lib dirs carry no engine,
    strip them so x86 emulators run the app under ARM translation instead of failing to find it."""
    lib = dec / "lib"
    if not lib.is_dir():
        return
    has_arm = (lib / "arm64-v8a" / "libyoyo.so").exists() or (lib / "armeabi-v7a" / "libyoyo.so").exists()
    has_x86_engine = (lib / "x86_64" / "libyoyo.so").exists() or (lib / "x86" / "libyoyo.so").exists()
    if has_arm and not has_x86_engine:
        for a in ("x86", "x86_64"):
            d = lib / a
            if d.is_dir():
                shutil.rmtree(d)
                print(f"      stripped lib/{a} (arm-only engine -> ARM translation on x86 emulators)")


def resolve_tools(args):
    cfg = {}
    local = HERE / "config.local.json"
    if local.exists():
        cfg.update(json.loads(local.read_text()))

    def pick(key, env, default=None):
        return getattr(args, key, None) or os.environ.get(env) or cfg.get(key) or default

    java = pick("java", "JAVA_HOME")
    if java and Path(java).is_dir():  # accept a JAVA_HOME dir or a direct java path
        java = str(Path(java) / "bin" / ("java.exe" if os.name == "nt" else "java"))
    java = java or shutil.which("java")

    sdk = os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT") or cfg.get("android_sdk")
    zipalign = pick("zipalign", "ZIPALIGN")
    apksigner = pick("apksigner", "APKSIGNER")
    if sdk and not (zipalign and apksigner):  # grab the newest build-tools
        bt = Path(sdk) / "build-tools"
        vers = sorted((p for p in bt.iterdir() if p.is_dir()), key=lambda p: p.name) if bt.is_dir() else []
        if vers:
            newest = vers[-1]
            zipalign = zipalign or str(newest / ("zipalign.exe" if os.name == "nt" else "zipalign"))
            apksigner = apksigner or str(newest / ("apksigner.bat" if os.name == "nt" else "apksigner"))

    return {
        "java": java,
        "apktool": pick("apktool", "APKTOOL"),
        "apkeditor": pick("apkeditor", "APKEDITOR"),
        "zipalign": zipalign or shutil.which("zipalign"),
        "apksigner": apksigner or shutil.which("apksigner"),
        "keystore": pick("keystore", "NETFLIX_PATCHER_KS"),
        "ks_pass": pick("ks_pass", "NETFLIX_PATCHER_KS_PASS", "android"),
        "ks_alias": pick("ks_alias", "NETFLIX_PATCHER_KS_ALIAS", "androiddebugkey"),
        "key_pass": pick("key_pass", "NETFLIX_PATCHER_KEY_PASS", "android"),
    }


def run(cmd, **kw):
    print("  $ " + " ".join(str(c) for c in cmd))
    return subprocess.run(cmd, check=True, **kw)


def unpack_input(path, workdir):
    """Return (base_apk, [splits]). Handles a plain .apk or a zip bundle (.apkm/.xapk)."""
    path = Path(path)
    if zipfile.is_zipfile(path) and path.suffix.lower() != ".apk":
        ex = workdir / "extracted"; ex.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(path) as z:
            z.extractall(ex)
        apks = list(ex.rglob("*.apk"))
        if not apks:
            sys.exit("! no .apk inside the bundle")
        base = (next((a for a in apks if a.name == "base.apk"), None)
                or next((a for a in apks if not re.search(r"config|split", a.name, re.I)), None)
                or max(apks, key=lambda a: a.stat().st_size))
        return base, [a for a in apks if a != base]
    return path, []


def main():
    ap = argparse.ArgumentParser(description="Strip the Netflix Games SDK gate from a Netflix game APK.")
    ap.add_argument("input", help="input .apk / .apkm / .xapk")
    ap.add_argument("-o", "--output", help="output APK (default: <input>-offline.apk)")
    ap.add_argument("--keep-cloud-save", action="store_true", help="leave the Netflix cloud-save blob APIs alone")
    ap.add_argument("--no-sign", action="store_true", help="leave the output unsigned")
    ap.add_argument("--keep-work", action="store_true", help="keep the working directory")
    for k in ("java", "apktool", "apkeditor", "zipalign", "apksigner",
              "keystore", "ks-pass", "ks-alias", "key-pass"):
        ap.add_argument("--" + k, dest=k.replace("-", "_"))
    args = ap.parse_args()

    T = resolve_tools(args)
    need = ["java", "apktool"] + ([] if args.no_sign else ["apkeditor", "zipalign", "apksigner", "keystore"])
    missing = [k for k in need if not T[k]]
    if missing:
        sys.exit("! missing tool paths: " + ", ".join(missing) +
                 "\n  set them in config.local.json, env vars, or CLI flags (see README).")

    in_path = Path(args.input).resolve()
    if not in_path.exists():
        sys.exit(f"! input not found: {in_path}")
    out_path = Path(args.output).resolve() if args.output else in_path.with_name(in_path.stem + "-offline.apk")

    work = Path(tempfile.mkdtemp(prefix="nfxpatch_"))
    print(f"[*] work dir: {work}")
    env = dict(os.environ)
    if T["java"] and Path(T["java"]).parent.parent.exists():
        env["JAVA_HOME"] = str(Path(T["java"]).parent.parent)
    try:
        print("[1/5] unpacking input")
        base_apk, splits = unpack_input(in_path, work)
        print(f"      base: {base_apk.name}  splits: {len(splits)}")

        print("[2/5] decoding base (apktool, resources kept raw)")
        dec = work / "apktool_base"
        run([T["java"], "-jar", T["apktool"], "d", "-r", "-f", "-o", str(dec), str(base_apk)], env=env)
        has_unity_bridge = find_smali_file(dec, SDK_MARKER_CLASS) is not None
        gamemaker = (not has_unity_bridge) and is_gamemaker_sdk(dec)
        if not has_unity_bridge and not gamemaker:
            sys.exit("! no NfUnitySdkInternal found. this doesn't look like a Netflix game.")

        print("[3/5] patching Netflix Games SDK")
        report = {"patched": [], "already": [], "not_found": [], "skipped": []}
        if gamemaker:
            print("      GameMaker + oldest SDK (no Unity bridge; patching the GameMaker glue class)")
            patch_gamemaker(dec, report)
            strip_non_arm_libs(dec)
        elif is_gen0_sdk(dec):
            print("      oldest SDK (com.netflix.android.api auth model; local slot store for saves)")
            patch_gen0(dec, report)
        elif is_legacy_sdk(dec):
            print("      older SDK (access-UI model, no doRequestPlayerAccess)")
            patch_legacy(dec, report)
        else:
          for p in PATCHES:
            if p.get("cloud_save") and args.keep_cloud_save:
                report["skipped"].append(p["name"]); continue
            f = find_smali_file(dec, p["class"])
            if f is None:
                report["not_found"].append(p["name"])
                if p["critical"]:
                    sys.exit(f"! critical class missing: {p['class']} (SDK layout changed?)")
                continue
            new, status = patch_method(f.read_text(encoding="utf-8"), p["sig"], p["locals"], p["body"])
            if status == "patched":
                f.write_text(new, encoding="utf-8")
            elif status == "not_found" and p["critical"]:
                sys.exit(f"! critical method missing: {p['class']}::{p['sig']} (SDK version drift?)")
            report[status].append(p["name"])
        for k in ("patched", "already", "not_found", "skipped"):
            if report[k]:
                print(f"      {k:9}: {', '.join(report[k])}")
        if not (report["patched"] or report["already"]):
            sys.exit("! nothing patched, aborting.")

        print("[4/5] rebuilding base")
        base_out = work / "base_patched.apk"
        run([T["java"], "-jar", T["apktool"], "b", "--use-aapt2", "-o", str(base_out), str(dec)], env=env)

        if splits:
            print(f"[5/5] merging base + {len(splits)} split(s), then signing")
            merge_in = work / "merge_in"; merge_in.mkdir()
            shutil.copy(base_out, merge_in / "base.apk")
            for s in splits:
                shutil.copy(s, merge_in / s.name)
            to_sign = work / "merged.apk"
            run([T["java"], "-jar", T["apkeditor"], "m", "-i", str(merge_in), "-o", str(to_sign), "-f"], env=env)
        else:
            print("[5/5] signing (single APK, no splits)")
            to_sign = base_out

        if args.no_sign:
            shutil.copy(to_sign, out_path)
            print(f"\n[done] unsigned output: {out_path}")
        else:
            aligned = work / "aligned.apk"
            run([T["zipalign"], "-p", "-f", "4", str(to_sign), str(aligned)])
            run([T["apksigner"], "sign", "--ks", T["keystore"], "--ks-pass", "pass:" + T["ks_pass"],
                 "--ks-key-alias", T["ks_alias"], "--key-pass", "pass:" + T["key_pass"], str(aligned)], env=env)
            shutil.copy(aligned, out_path)
            print(f"\n[done] signed offline APK: {out_path}")

        print("       Netflix gate removed. install with: adb install -t "
              f'"{out_path.name}"  (uninstall the original first, different signer)')
        print("       heads-up: a game with its own dead backend may still stall after the Netflix screen.")
    finally:
        if args.keep_work:
            print(f"[i] work dir kept: {work}")
        else:
            shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()
