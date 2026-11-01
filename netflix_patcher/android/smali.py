"""Shared smali primitives: method-body rewriting, class lookup, and the SDK type
names the patched bodies reference across every generation."""
import shutil
import re
from .. import MARKER


_PAI = "Lcom/netflix/games/player/access/PlayerAccessInfo;"

_RES = "Lcom/netflix/games/NetflixResult;"

_ERR = "Lcom/netflix/games/Error;"

_CB  = "Lcom/netflix/games/Callback;"

_RES_CTOR = f"{_RES}-><init>(Ljava/lang/Object;{_ERR})V"

_ONRESULT = f"{_CB}->onResult({_RES})V"

_MEMBERSHIP = "Lcom/netflix/games/player/access/MembershipStatus;"

def pai_ctor(dec):
    """PlayerAccessInfo ctor sig. SDK 1.9.x adds a MembershipStatus arg."""
    f = find_smali_file(dec, "com/netflix/games/player/access/PlayerAccessInfo")
    two = f"<init>(Ljava/lang/String;{_MEMBERSHIP})V"
    if f is not None and two in f.read_text(encoding="utf-8", errors="ignore"):
        return f"(Ljava/lang/String;{_MEMBERSHIP})V"
    return "(Ljava/lang/String;)V"

def emit_player_access_info(dec, dst, r_id, r_extra, player_id=None):
    """Build a PlayerAccessInfo in dst. r_extra only used by the 2-arg ctor (MEMBER)."""
    sig = pai_ctor(dec)
    out = [f"new-instance {dst}, {_PAI}",
           f'const-string {r_id}, "{player_id or OFFLINE_PLAYER_ID}"']
    if _MEMBERSHIP in sig:
        out.append(f"sget-object {r_extra}, {_MEMBERSHIP}->MEMBER:{_MEMBERSHIP}")
        out.append(f"invoke-direct {{{dst}, {r_id}, {r_extra}}}, {_PAI}-><init>{sig}")
    else:
        out.append(f"invoke-direct {{{dst}, {r_id}}}, {_PAI}-><init>{sig}")
    return out

_ACCESS_API = "com/netflix/games/player/access/AccessApi"

_BLOB_API = "com/netflix/games/storage/blobs/BlobStoreApi"

_RESCOMP = "Lcom/netflix/games/NetflixResult$Companion;"

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

def _decl_files_all(dec, typ):
    """Every smali file whose header declares `.implements L{typ};` (there is usually one)."""
    needle = f".implements L{typ};"
    out = []
    for d in sorted(dec.glob("smali*")):
        for f in d.rglob("*.smali"):
            try:
                with open(f, encoding="utf-8", errors="ignore") as fh:
                    head = "".join(fh.readline() for _ in range(15))
                if needle in head:
                    out.append(f)
            except OSError:
                pass
    return out

_CURRENT_PROFILE = "com/netflix/games/player/profiles/CurrentProfile"

_OFFLINE_PROFILE = "Lcom/netflix/games/player/profiles/NfxOfflineProfile;"

OFFLINE_PLAYER_ID = "offline-player"

def emit_offline_profile(dec):
    """Write a concrete CurrentProfile subclass with a stable offline identity; returns its
    descriptor, or None. Subclassed directly: the SDK's own needs an R8-renamed collaborator."""
    cp = find_smali_file(dec, _CURRENT_PROFILE)
    if cp is None:
        return None
    ctor = "<init>(Ljava/lang/String;Ljava/lang/String;Ljava/lang/String;)V"
    text = cp.read_text(encoding="utf-8")
    abstract = set(re.findall(r"^\.method[^\n]*\babstract\s+(\S+)", text, re.M))
    supported = {"getLegacyGamerAccessToken()" + _RES, "getLegacyGamerProfileId()" + _RES}
    # Refuse unknown abstract contracts: assembling dex alone would not catch
    # an AbstractMethodError when the game first uses the profile.
    if (f".method public constructor {ctor}" not in text
            or re.search(r"^\.class[^\n]*\bfinal\b", text, re.M)
            or not abstract.issubset(supported)):
        return None

    def _with_data(value):
        return [f"    sget-object v0, {_RES}->Companion:{_RESCOMP}",
                f'    const-string v1, "{value}"',
                f"    invoke-virtual {{v0, v1}}, {_RESCOMP}->withData(Ljava/lang/Object;){_RES}",
                "    move-result-object v0", "    return-object v0"]

    out = [f".class public final {_OFFLINE_PROFILE}",
           f".super L{_CURRENT_PROFILE};", "",
           f"# {MARKER}: synthetic signed-in profile for offline play", "",
           ".method public constructor <init>()V", "    .locals 3",
           f'    const-string v0, "{OFFLINE_PLAYER_ID}"',
           '    const-string v1, "Offline"',
           '    const-string v2, "en"',
           f"    invoke-direct {{p0, v0, v1, v2}}, L{_CURRENT_PROFILE};->{ctor}",
           "    return-void", ".end method", "",
           ".method public getLegacyGamerAccessToken()" + _RES, "    .locals 2"]
    out += _with_data("offline")
    out += [".end method", "",
            ".method public getLegacyGamerProfileId()" + _RES, "    .locals 2"]
    out += _with_data(OFFLINE_PLAYER_ID)
    out += [".end method"]
    (cp.parent / "NfxOfflineProfile.smali").write_text("\n".join(out) + "\n", encoding="utf-8")
    return _OFFLINE_PROFILE

def offline_profile_body(cls):
    """Method body handing back NetflixResult.withData(<the offline profile>)."""
    return ['const-string v0, "NfxOffline"',
            'const-string v1, "getCurrentProfile: local profile; platform readiness unchanged"',
            "invoke-static {v0, v1}, Landroid/util/Log;->d(Ljava/lang/String;Ljava/lang/String;)I",
            f"new-instance v0, {cls}",
            f"invoke-direct {{v0}}, {cls}-><init>()V",
            f"sget-object v1, {_RES}->Companion:{_RESCOMP}",
            f"invoke-virtual {{v1, v0}}, {_RESCOMP}->withData(Ljava/lang/Object;){_RES}",
            "move-result-object v0", "return-object v0"]

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
