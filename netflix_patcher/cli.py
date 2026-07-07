"""Command-line entry: parse args and dispatch to the Android or iOS pipeline."""
import argparse
import sys
from pathlib import Path

from .android.pipeline import run_android
from .ios.pipeline import is_ipa, run_ios


def main():
    ap = argparse.ArgumentParser(
        description="Remove the Netflix Games SDK login gate from a Netflix game (Android APK or iOS IPA).")
    ap.add_argument("input", help="input .apk / .apkm / .xapk (Android) or .ipa (iOS)")
    ap.add_argument("-o", "--output", help="output path (default: <input>-offline.apk or .ipa)")
    ap.add_argument("--keep-cloud-save", action="store_true", help="leave the Netflix cloud-save APIs alone (Android)")
    ap.add_argument("--no-sign", action="store_true", help="leave the output unsigned (Android)")
    ap.add_argument("--keep-work", action="store_true", help="keep the working directory")
    for k in ("java", "apktool", "apkeditor", "zipalign", "apksigner",
              "keystore", "ks-pass", "ks-alias", "key-pass"):
        ap.add_argument("--" + k, dest=k.replace("-", "_"))
    args = ap.parse_args()

    in_path = Path(args.input).resolve()
    if not in_path.exists():
        sys.exit(f"! input not found: {in_path}")

    if is_ipa(in_path):
        out = Path(args.output).resolve() if args.output else in_path.with_name(in_path.stem + "-offline.ipa")
        print(f"[*] iOS app detected: {in_path.name}")
        run_ios(in_path, out, args)
    else:
        out = Path(args.output).resolve() if args.output else in_path.with_name(in_path.stem + "-offline.apk")
        run_android(in_path, out, args)
