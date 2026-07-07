"""Android toolchain resolution (JDK, apktool, APKEditor, build-tools) and a command runner."""
import json
import os
import shutil
import subprocess
from pathlib import Path
from .. import REPO_ROOT


def resolve_tools(args):
    cfg = {}
    local = REPO_ROOT / "config.local.json"
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
