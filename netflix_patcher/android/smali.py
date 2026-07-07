"""Shared smali primitives: method-body rewriting, class lookup, and the SDK type
names the patched bodies reference across every generation."""
import shutil
from .. import MARKER


_PAI = "Lcom/netflix/games/player/access/PlayerAccessInfo;"

_RES = "Lcom/netflix/games/NetflixResult;"

_ERR = "Lcom/netflix/games/Error;"

_CB  = "Lcom/netflix/games/Callback;"

_RES_CTOR = f"{_RES}-><init>(Ljava/lang/Object;{_ERR})V"

_ONRESULT = f"{_CB}->onResult({_RES})V"

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
