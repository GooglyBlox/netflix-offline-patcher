"""iOS SDK handlers, one per (SDK generation x engine binding).

To support a new generation or binding, add a handler with detect()/apply() and register it
in HANDLERS. run_ios() applies the first handler whose detect() matches.

So far every handler neutralises the gate by dyld-interposing the SDK's flat `ngp_*` C ABI,
which is the bridge Unity (and other C-P/Invoke engines) call. Native engines that talk to
the SDK through its Obj-C/Swift API instead (UE4, GameMaker) do not import that C ABI, so
these handlers correctly decline them.
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


class CAbiHandler:
    """Interpose the SDK's flat C ABI with a prebuilt shim. Subclasses set the SDK framework,
    the marker symbol the engine imports, and which shim to inject."""

    shim_framework = "NetflixOffline.framework"
    shim_binary = "NetflixOffline"
    shim_load_path = "@rpath/NetflixOffline.framework/NetflixOffline"

    def _sdk_present(self, app):
        b = app / "Frameworks" / self.sdk_framework / self.sdk_binary
        return b.exists() and self.sdk_marker in b.read_bytes()

    def _importers(self, app):
        """Framework binaries that call the SDK's C ABI (they import the marker) - the engine
        glue. The main executable is always wired too, so it is not included here."""
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

    def _main_calls_abi(self, app):
        b = _bundle_binary(app)
        try:
            return b.exists() and self.sdk_marker in b.read_bytes()
        except Exception:
            return False

    def detect(self, app):
        # SDK present AND some binary actually calls its C ABI. A title that reaches the SDK
        # through its Obj-C/Swift API (UE4, GameMaker) imports no ngp_ symbol, so it declines.
        return self._sdk_present(app) and (self._importers(app) or self._main_calls_abi(app))

    def apply(self, app):
        src = SHIMS / self.shim_name / self.shim_framework
        if not (src / self.shim_binary).exists():
            sys.exit(f"! prebuilt shim missing at {src}")
        dst = app / "Frameworks" / self.shim_framework
        shutil.rmtree(dst, ignore_errors=True)
        shutil.copytree(src, dst)
        print(f"      installed {self.shim_framework} ({self.shim_name})")

        main_bin = _bundle_binary(app)
        # main executable = interpose registered in the launch closure; engine frameworks =
        # wired in the same dlopen batch that binds their ngp_ calls.
        targets = [main_bin] + [b for b in self._importers(app) if b != main_bin]
        wired = 0
        for b in targets:
            res = macho_add_load_dylib(b, self.shim_load_path)
            if "added" in res or "already" in res:
                print(f"      wired -> {b.name}")
                wired += 1
            else:
                print(f"      (skipped {b.name}: {res})")
        if not wired:
            sys.exit("! could not wire the shim into any binary (unexpected Mach-O layout).")


class Gen2UnityHandler(CAbiHandler):
    """gen-2 com.netflix.games SDK (NetflixGames.framework). Request-and-grant player access."""
    key = "gen2-unity"
    summary = "gen-2 com.netflix.games SDK, ngp_* C ABI (Unity IL2CPP bridge)"
    sdk_framework = "NetflixGames.framework"
    sdk_binary = "NetflixGames"
    sdk_marker = b"ngp_request_player_access"
    shim_name = "gen2_unity"


class Gen0UnityHandler(CAbiHandler):
    """gen-0 NGP SDK (NGP.framework). Event-driven auth: fire a synthetic onUserStateChange
    signed-in event, plus a local slot store so cloud-only saves persist."""
    key = "gen0-unity"
    summary = "gen-0 NGP SDK, ngp_* C ABI, event-driven auth (Unity IL2CPP bridge)"
    sdk_framework = "NGP.framework"
    sdk_binary = "NGP"
    sdk_marker = b"ngp_check_user_authentication"
    shim_name = "gen0_unity"


HANDLERS = [Gen2UnityHandler(), Gen0UnityHandler()]
