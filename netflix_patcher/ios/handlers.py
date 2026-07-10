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
from .macho import macho_add_load_dylib, macho_set_min_os

SHIMS = PKG_DIR / "ios" / "shims"


def _app_min_os(app):
    info = app / "Info.plist"
    if info.exists():
        import plistlib
        try:
            return plistlib.loads(info.read_bytes()).get("MinimumOSVersion")
        except Exception:
            pass
    return None


def _match_shim_min_os(shim_framework_dir, shim_binary, version):
    """iOS won't install an app whose embedded framework needs a newer OS than the app, so
    set the installed shim's min OS (Mach-O + Info.plist) to the app's."""
    macho_set_min_os(shim_framework_dir / shim_binary, version)
    plist = shim_framework_dir / "Info.plist"
    if plist.exists():
        import plistlib
        try:
            d = plistlib.loads(plist.read_bytes())
            d["MinimumOSVersion"] = version
            plist.write_bytes(plistlib.dumps(d))
        except Exception:
            pass


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


def _install_shim(app, shim_name, shim_framework, shim_binary, shim_load_path, targets):
    """Copy a prebuilt shim framework into the app, match its min-OS to the app, and add an
    LC_LOAD_DYLIB for it to each target binary. Shared by the C-ABI and Obj-C handlers."""
    src = SHIMS / shim_name / shim_framework
    if not (src / shim_binary).exists():
        sys.exit(f"! prebuilt shim missing at {src}")
    dst = app / "Frameworks" / shim_framework
    shutil.rmtree(dst, ignore_errors=True)
    shutil.copytree(src, dst)
    min_os = _app_min_os(app)
    if min_os:
        _match_shim_min_os(dst, shim_binary, min_os)
    print(f"      installed {shim_framework} ({shim_name}, min iOS {min_os or 'default'})")
    wired = 0
    for b in targets:
        res = macho_add_load_dylib(b, shim_load_path)
        if "added" in res or "already" in res:
            print(f"      wired -> {b.name}")
            wired += 1
        else:
            print(f"      (skipped {b.name}: {res})")
    if not wired:
        sys.exit("! could not wire the shim into any binary (unexpected Mach-O layout).")


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
        main_bin = _bundle_binary(app)
        # main executable = interpose registered in the launch closure; engine frameworks =
        # wired in the same dlopen batch that binds their ngp_ calls.
        targets = [main_bin] + [b for b in self._importers(app) if b != main_bin]
        _install_shim(app, self.shim_name, self.shim_framework, self.shim_binary,
                      self.shim_load_path, targets)


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


class ObjcSwizzleHandler:
    """Titles that reach the SDK through its Obj-C / Swift @objc API instead of the ngp_* C ABI
    (UE4, GameMaker). There is nothing to interpose, so the shim swizzles the SDK's Obj-C methods in
    a load-time constructor - a process-wide patch, so wiring it into the main executable is enough.
    detect() keys on the Obj-C class marker being *imported by the game's own binaries* (not merely
    defined in the SDK framework), so Unity titles fall to the C-ABI handlers (matched first)."""

    shim_framework = "NetflixOffline.framework"
    shim_binary = "NetflixOffline"
    shim_load_path = "@rpath/NetflixOffline.framework/NetflixOffline"

    def _sdk_present(self, app):
        return (app / "Frameworks" / self.sdk_framework / self.sdk_binary).exists()

    def _game_binaries(self, app):
        """Main executable + engine framework binaries (not the SDK framework, which defines the marker)."""
        skip = {self.sdk_framework, "NetflixGames-companion.framework", self.shim_framework}
        out = [_bundle_binary(app)]
        for d in sorted((app / "Frameworks").glob("*.framework")):
            if d.name not in skip:
                out.append(_bundle_binary(d))
        return out

    def _marker_importers(self, app):
        out = []
        for b in self._game_binaries(app):
            try:
                data = b.read_bytes() if b.exists() else b""
            except Exception:
                data = b""
            if any(m in data for m in self.objc_markers):
                out.append(b)
        return out

    def detect(self, app):
        return self._sdk_present(app) and bool(self._marker_importers(app))

    def apply(self, app):
        main_bin = _bundle_binary(app)
        targets = [main_bin] + [b for b in self._marker_importers(app) if b != main_bin]
        _install_shim(app, self.shim_name, self.shim_framework, self.shim_binary,
                      self.shim_load_path, targets)


class Gen0ObjcHandler(ObjcSwizzleHandler):
    """gen-0 NGP SDK via its Obj-C facade `NetflixSDK` (Unreal Engine titles). Swizzles
    registerEventReceiver:/checkUserAuth to deliver a synthetic signed-in state, plus a local slot
    store."""
    key = "gen0-objc"
    summary = "gen-0 NGP SDK, Obj-C NetflixSDK facade (Unreal Engine binding)"
    sdk_framework = "NGP.framework"
    sdk_binary = "NGP"
    objc_markers = (b"_OBJC_CLASS_$_NetflixSDK",)
    shim_name = "gen0_objc"


class Gen2ObjcHandler(ObjcSwizzleHandler):
    """gen-2 com.netflix.games SDK via its Swift @objc API (GameMaker titles). Swizzles
    AccessProvider.requestPlayerAccess... to grant offline access and CloudSavesProvider read/write
    to a local blob store (read-miss -> blobNameNotFound)."""
    key = "gen2-objc"
    summary = "gen-2 com.netflix.games SDK, Swift @objc API (GameMaker binding)"
    sdk_framework = "NetflixGames.framework"
    sdk_binary = "NetflixGames"
    objc_markers = (b"_OBJC_CLASS_$_NetflixBlobContainer", b"NetflixGames15NetflixGamesSDK")
    shim_name = "gen2_objc"


# C-ABI (Unity) handlers first - they are the proven, most specific match; the Obj-C handlers
# pick up the engines that drive the SDK through Obj-C/Swift instead (UE4, GameMaker).
HANDLERS = [Gen2UnityHandler(), Gen0UnityHandler(), Gen2ObjcHandler(), Gen0ObjcHandler()]
