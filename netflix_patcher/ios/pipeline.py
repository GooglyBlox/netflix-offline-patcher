"""iOS pipeline: unpack the IPA, pick the matching SDK handler, apply it, repackage."""
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

from .handlers import HANDLERS, find_app_bundle


def is_ipa(path):
    """An .ipa, or any zip that carries a Payload/*.app bundle."""
    path = Path(path)
    if path.suffix.lower() == ".ipa":
        return True
    if zipfile.is_zipfile(path) and path.suffix.lower() not in (".apk", ".apkm", ".xapk"):
        try:
            with zipfile.ZipFile(path) as z:
                return any(n.startswith("Payload/") and ".app/" in n for n in z.namelist())
        except Exception:
            return False
    return False


def run_ios(in_path, out_path, args):
    work = Path(tempfile.mkdtemp(prefix="nfxpatch_ios_"))
    print(f"[*] work dir: {work}")
    try:
        print("[1/3] unpacking IPA")
        with zipfile.ZipFile(in_path) as z:
            z.extractall(work)
        payload = work / "Payload"
        if not payload.is_dir():
            sys.exit("! IPA has no Payload/ (is this really an .ipa?)")
        app = find_app_bundle(payload)
        print(f"      app: {app.name}")

        handler = next((h for h in HANDLERS if h.detect(app)), None)
        if handler is None:
            if (app / "Frameworks" / "NetflixGames.framework").exists():
                sys.exit("! NetflixGames.framework is present but no handler matched its SDK "
                         "generation yet (only the gen-2 ngp_ C ABI is supported so far).")
            sys.exit("! no NetflixGames.framework in the bundle - this doesn't look like a Netflix iOS game.")
        print(f"[2/3] {handler.summary}")
        handler.apply(app)
        if args.keep_cloud_save:
            print("      note: --keep-cloud-save has no effect on iOS (the shim's cloud-save "
                  "stubs are offline-safe and always active)")

        print("[3/3] repackaging IPA")
        if out_path.exists():
            out_path.unlink()
        with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED, compresslevel=1) as z:
            for f in sorted(payload.rglob("*")):
                if f.is_file() or f.is_symlink():
                    z.write(f, arcname=str(f.relative_to(work).as_posix()))
        print(f"\n[done] offline IPA (unsigned): {out_path}")
        print("       Netflix gate removed. Sideload it (AltStore / Sideloadly) to re-sign and install.")
        print("       heads-up: a game with its own dead backend may still stall after the Netflix screen.")
    finally:
        if args.keep_work:
            print(f"[i] work dir kept: {work}")
        else:
            shutil.rmtree(work, ignore_errors=True)
