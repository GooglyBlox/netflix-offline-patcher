"""Android pipeline: unpack, decode with apktool, detect the SDK/engine, patch, rebuild, sign."""
import os
import re
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

from .tools import resolve_tools, run
from .smali import find_smali_file, patch_method, SDK_MARKER_CLASS, strip_non_arm_libs
from .unity_gen2 import PATCHES
from .access_ui import is_legacy_sdk, patch_legacy
from .gen0 import is_gen0_sdk, patch_gen0
from .gamemaker import (is_gamemaker_sdk, patch_gamemaker, is_gamemaker_gen0_wrapper,
                        patch_gamemaker_gen0_wrapper, is_gamemaker_gen2_sdk, patch_gamemaker_gen2)
from .unreal import is_unreal_gen0, patch_unreal_gen0
from .gen2_nobridge import is_gen2_nobridge, patch_gen2_nobridge


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

        def _has_classes_dex(a):
            try:
                with zipfile.ZipFile(a) as z:
                    return any(n == "classes.dex" for n in z.namelist())
            except Exception:
                return False

        base = (next((a for a in apks if a.name == "base.apk"), None)
                or next((a for a in apks if _has_classes_dex(a)), None)
                or next((a for a in apks if not re.search(r"config|split|install.?time|asset", a.name, re.I)), None)
                or max(apks, key=lambda a: a.stat().st_size))
        return base, [a for a in apks if a != base]
    return path, []


def run_android(in_path, out_path, args):
    T = resolve_tools(args)
    need = ["java", "apktool"] + ([] if args.no_sign else ["apkeditor", "zipalign", "apksigner", "keystore"])
    missing = [k for k in need if not T[k]]
    if missing:
        sys.exit("! missing tool paths: " + ", ".join(missing) +
                 "\n  set them in config.local.json, env vars, or CLI flags (see README).")

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
        gamemaker_gen0 = (not has_unity_bridge) and is_gamemaker_sdk(dec)
        gamemaker_gen2 = (not has_unity_bridge) and (not gamemaker_gen0) and is_gamemaker_gen2_sdk(dec)
        gamemaker = gamemaker_gen0 or gamemaker_gen2
        unreal_gen0 = (not has_unity_bridge) and (not gamemaker) and is_unreal_gen0(dec)
        gen2_nobridge = (not has_unity_bridge) and (not gamemaker) and (not unreal_gen0) and is_gen2_nobridge(dec)
        gamemaker_gen0_wrapper = (not has_unity_bridge) and (not gamemaker) and (not unreal_gen0) \
            and (not gen2_nobridge) and is_gamemaker_gen0_wrapper(dec)
        if not (has_unity_bridge or gamemaker or unreal_gen0 or gen2_nobridge or gamemaker_gen0_wrapper):
            sys.exit("! no Netflix Games glue found (Unity, GameMaker, or Unreal). "
                     "this doesn't look like a Netflix game.")

        print("[3/5] patching Netflix Games SDK")
        report = {"patched": [], "already": [], "not_found": [], "skipped": []}
        if gamemaker_gen2:
            print("      GameMaker + newer com.netflix.games SDK (no Unity bridge; patching the GameMaker glue)")
            patch_gamemaker_gen2(dec, report)
            strip_non_arm_libs(dec)
        elif gamemaker_gen0:
            print("      GameMaker + oldest SDK (no Unity bridge; patching the GameMaker glue class)")
            patch_gamemaker(dec, report)
            strip_non_arm_libs(dec)
        elif unreal_gen0:
            print("      Unreal Engine + oldest SDK (com.netflix.android.api; patching the UE4 JNI glue, auth-only)")
            patch_unreal_gen0(dec, report)
        elif gen2_nobridge:
            print("      newer com.netflix.games SDK, no engine glue (patching the SDK AccessApi + BlobStoreApi impls directly)")
            patch_gen2_nobridge(dec, report)
        elif gamemaker_gen0_wrapper:
            print("      GameMaker + oldest SDK, 'NetflixWrapper' glue (auth-only, DsMap user-state event; local GameMaker save)")
            patch_gamemaker_gen0_wrapper(dec, report)
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
                # libyoyo is arm-only, so drop the engine-less x86 arch splits to run under ARM translation
                if gamemaker and re.search(r"config\.x86(_64)?\.apk$", s.name):
                    print(f"      (skip {s.name}: arm-only engine)")
                    continue
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
