"""iOS SDK handlers, one per (SDK generation x engine framework).

To support a new generation or framework, add a handler with detect()/apply() and register
it in HANDLERS. run_ios() applies the first handler whose detect() matches.
"""
import shutil
import sys

from .. import PKG_DIR
from .macho import macho_add_load_dylib

SHIMS = PKG_DIR / "ios" / "shims"


def _bundle_binary(bundle_dir):
    """The Mach-O inside a .app/.framework: CFBundleExecutable, else the same-named file."""
    exe = None
    info = bundle_dir / "Info.plist"
    if info.exists():
        import plistlib
        try:
            exe = plistlib.loads(info.read_bytes()).get("CFBundleExecutable")
        except Exception:
            exe = None
    cand = bundle_dir / (exe or bundle_dir.stem)
    return cand if cand.exists() else bundle_dir / bundle_dir.stem


def find_app_bundle(payload_dir):
    apps = sorted(payload_dir.glob("*.app"))
    if not apps:
        sys.exit("! no .app bundle inside Payload/")
    return apps[0]


class Gen2UnityHandler:
    """gen-2 com.netflix.games SDK exposed as the flat ngp_* C ABI (the Unity plugin bridge,
    and any other engine that calls the SDK through it). The gate is neutralised by a prebuilt
    dyld-interpose dylib that answers the ngp_* calls with a granted / offline result."""

    key = "gen2-unity"
    summary = "gen-2 com.netflix.games SDK, ngp_* C ABI (Unity IL2CPP bridge)"

    sdk_framework = "NetflixGames.framework"
    sdk_binary = "NetflixGames"
    sdk_marker = b"ngp_request_player_access"

    shim_name = "gen2_unity"
    shim_framework = "NetflixOffline.framework"
    shim_binary = "NetflixOffline"
    shim_load_path = "@rpath/NetflixOffline.framework/NetflixOffline"

    def detect(self, app):
        b = app / "Frameworks" / self.sdk_framework / self.sdk_binary
        return b.exists() and self.sdk_marker in b.read_bytes()

    def _engine_binaries(self, app):
        # Frameworks that import the SDK's C ABI (the engine glue). Wire the shim into these
        # too so the interpose is registered in the same dlopen batch that binds their calls.
        skip = {self.sdk_framework, "NetflixGames-companion.framework", self.shim_framework}
        out = []
        for d in sorted((app / "Frameworks").glob("*.framework")):
            if d.name in skip:
                continue
            b = _bundle_binary(d)
            try:
                if b.exists() and self.sdk_marker in b.read_bytes():
                    out.append(b)
            except Exception:
                pass
        return out

    def apply(self, app):
        src = SHIMS / self.shim_name / self.shim_framework
        if not (src / self.shim_binary).exists():
            sys.exit(f"! prebuilt shim missing at {src}")
        dst = app / "Frameworks" / self.shim_framework
        shutil.rmtree(dst, ignore_errors=True)
        shutil.copytree(src, dst)
        print(f"      installed {self.shim_framework}")

        main_bin = _bundle_binary(app)
        if not main_bin.exists():
            sys.exit(f"! could not find the app's main executable ({main_bin.name})")
        wired = 0
        for b in [main_bin] + [e for e in self._engine_binaries(app) if e != main_bin]:
            res = macho_add_load_dylib(b, self.shim_load_path)
            if "added" in res or "already" in res:
                print(f"      wired -> {b.name}")
                wired += 1
            else:
                print(f"      (skipped {b.name}: {res})")
        if not wired:
            sys.exit("! could not wire the shim into any binary (unexpected Mach-O layout).")


HANDLERS = [Gen2UnityHandler()]
