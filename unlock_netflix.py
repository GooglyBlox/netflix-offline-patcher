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
        if not find_smali_file(dec, SDK_MARKER_CLASS):
            sys.exit("! no NfUnitySdkInternal found. this doesn't look like a Netflix game.")

        print("[3/5] patching Netflix Games SDK")
        report = {"patched": [], "already": [], "not_found": [], "skipped": []}
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
