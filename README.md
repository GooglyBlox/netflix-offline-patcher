# Netflix Offline Patcher

Built as an aide for the [Netflix Games Recovery Project](https://netflix.notaspider.dev).

Removes the Netflix Games SDK login gate from a Netflix game so it runs offline with no
Netflix account.

- **Android** (`.apk`, `.apkm`, `.xapk`): rewrites the SDK's smali. Output is a signed APK.
- **iOS** (`.ipa`): injects a dyld-interpose shim. Output is an unsigned IPA you sideload.

## Usage

```
python unlock_netflix.py INPUT [-o OUTPUT]
```

Android, producing a signed offline APK:

```
python unlock_netflix.py game.apkm
adb install -t game-offline.apk
```

Uninstall the store version first; the output is signed with a different key.

iOS, producing an unsigned offline IPA:

```
python unlock_netflix.py game.ipa
```

Install `game-offline.ipa` with AltStore or Sideloadly, which re-signs it with your Apple
ID. The iOS path needs only Python (no JDK, apktool, or keystore). The input must be a
*decrypted* IPA; an App Store download is FairPlay-encrypted and cannot be patched.

### Options

```
-o PATH            output path (default: <input>-offline.apk or .ipa)
--keep-cloud-save  leave the cloud-save APIs unpatched (Android only)
--no-sign          skip signing (Android)
--keep-work        keep the temp working directory
```

## How it works: Android

The login check lives in the Netflix Games SDK, not the game. Every title bundles the same
SDK in `classes.dex` and calls into it over JNI, so one set of smali patches works whatever
the engine is. The tool decodes the base APK with apktool, detects which SDK generation and
engine it is facing, patches, then rebuilds, merges any splits with APKEditor, zipaligns,
and signs. It aborts if a critical method has drifted out of reach.

Netflix shipped three SDK generations. Through the Unity bridge
(`com.netflix.unity.impl.NfUnitySdkInternal`):

- **Newer (2025), `com.netflix.games`:** `doRequestPlayerAccess` returns a granted result
  locally; `startSdkErrorActivity` becomes a no-op; `readBlob`/`getBlobs`/`writeBlob` return
  an offline "no cloud save" result instead of hanging.
- **Older (2024):** no `doRequestPlayerAccess`; the gate is an access UI. No-op the error
  screen, return a synthetic profile from `getCurrentProfile`, and fire the access-granted
  and UI-dismissed events once. Obfuscated classes are found by their SDK supertypes.
- **Oldest (2022-2023), `com.netflix.android.api`:** auth is a `checkUserAuth` call answered
  by an `onUserStateChange` event. Return a dummy player, force a signed-in state, and swap
  the dead cloud slot API for a local file store keyed to a stable offline identity (these
  titles keep progress only in the cloud, so without it saves would not persist).

Non-Unity engines reach the SDK through a game-authored glue class instead of the Unity
bridge; the tool finds it by parsing:

- **GameMaker** (`libyoyo.so`): a GameMaker extension exposes `Nfxa*`/`Netflix*` methods
  that return results to GML as async events. Handled for both the oldest SDK (deliver a
  synthetic signed-in state to the glue's event receiver, redirect cloud *slots* to a local
  store) and the gen-2 SDK (synthesize a granted access, redirect cloud *blobs*). The engine
  is ARM-only, so the engine-less x86/x86_64 libraries are dropped.
- **Unreal** (`libUE4.so`): an Unreal JNI glue (`Thunk_*` in, `native nativeOn*` out) on the
  oldest SDK. The tool rewrites the glue to deliver a synthetic signed-in profile straight to
  the native callback. Auth only; progress lives in the engine's own save.
- **No engine glue:** patch the SDK's own AccessApi and BlobStoreApi implementations directly.

> **Note (GameMaker and Unreal):** resources are kept raw, so the binary manifest is not
> edited. If a game declares the `com.netflix.nfgsdk.permission.ngpstore` permission,
> installing it alongside another patched Netflix game fails with
> `INSTALL_FAILED_DUPLICATE_PERMISSION`. Removing that one `<permission>` is a per-game step;
> standalone installs are unaffected.

## How it works: iOS

On iOS the SDK ships as a native framework, not smali. A Mach-O cannot be reassembled like
smali, so the tool ships prebuilt arm64 shim dylibs: it copies one into `Frameworks/` and adds
one `LC_LOAD_DYLIB` to the main executable (and any engine framework), editing only the Mach-O
header padding so every framework's code stays byte-for-byte the same. The output is unsigned;
the sideloader re-signs the bundle, shim included. Each shim is rebuildable from source (see its
`BUILD.md`). How the shim reaches the gate depends on how the game's engine reaches the SDK -
there are two bindings, and the tool auto-detects which by what the game's own binaries import.

**C-ABI binding (Unity).** The Unity plugin reaches the SDK over a flat C ABI: `extern "C"`
`ngp_*` functions where every result comes back as a JSON string. The shim overrides those C
functions by dyld interposing (a `__DATA,__interpose` table). The engine stores its pending task
before the native call, so the shim answers with the exact JSON the engine expects. The `ngp_*`
symbols are `weak_import`, so one prebuilt binary fits every title of that generation.

- **gen-2 `com.netflix.games`** (`NetflixGames.framework`): `ngp_request_player_access` hands
  back a granted `PlayerAccessInfo`; the access-UI call is a no-op; `ngp_blob_store_*` return an
  offline "no cloud save" result. (shim `gen2_unity`)
- **gen-0 NGP** (`NGP.framework`): auth is event-driven, so the shim captures the event
  dispatcher and, on `ngp_check_user_authentication`, fires a synthetic `onUserStateChange`
  signed-in event, plus a local slot store (read-miss = `ErrorUnknownSlotId`). (shim `gen0_unity`)

**Obj-C/Swift binding (Unreal, GameMaker).** These engines import no `ngp_*` symbol - they reach
the SDK through its Objective-C / Swift `@objc` API (they import `_OBJC_CLASS_$_NetflixSDK`, or the
Swift `NetflixGamesSDK` class + `NetflixBlobContainer`). There is nothing to interpose, so the shim
instead **swizzles the SDK's Obj-C methods** in a load-time constructor (using the Obj-C runtime C
API, no Apple SDK needed). Swizzling patches the shared class objects process-wide, so wiring the
main executable is enough. Where the SDK returns model objects, the shim hands back the real class
with its ivars set, or a small duck-typed stand-in built at runtime.

- **gen-0 NGP Obj-C facade** (Unreal): swizzle `+[NetflixSDK checkUserAuth]` to deliver a synthetic
  signed-in `NetflixSDKState`(`NetflixProfile`) to the registered event receiver;
  `registerEventReceiver:` captures it; UI-present calls are no-ops; the slot methods back a local
  file store. (shim `gen0_objc`)
- **gen-2 Swift `@objc` API** (GameMaker): grant player access through whichever model the title
  uses - the completion form (`requestPlayerAccessWithCompletionHandler:`) or the event form
  (`registerEventHandler:` + `showNetflixAccessUIIfNecessary` -> a synthetic granted
  `onPlayerAccessChangeWithAccessEvent:`); `currentProfileAndReturnError:` returns an offline
  profile; the blob store is backed by a local file store (read-miss = `blobNameNotFound`, -1001).
  (shim `gen2_objc`)

## Project layout

```
unlock_netflix.py              CLI entry point
netflix_patcher/
  cli.py                       argument parsing, Android/iOS dispatch
  android/
    smali.py                   shared smali primitives and SDK type names
    unity_gen2.py              gen-2 SDK, Unity bridge
    access_ui.py               older access-UI SDK
    gen0.py                    oldest com.netflix.android.api SDK
    gamemaker.py               GameMaker glue (all shapes)
    unreal.py                  Unreal JNI glue
    gen2_nobridge.py           gen-2 SDK, no engine glue
    tools.py, pipeline.py      toolchain resolution, orchestration
  ios/
    handlers.py                one handler per (SDK generation x engine binding)
    macho.py                   Mach-O load-command injection
    pipeline.py                orchestration
    shims/nfx_objc_common.h    shared Obj-C-runtime/libc/dispatch preamble (Obj-C shims)
    shims/gen2_unity/          prebuilt dylib + source - C-ABI interpose (gen-2, Unity)
    shims/gen0_unity/          prebuilt dylib + source - C-ABI interpose (gen-0, Unity)
    shims/gen2_objc/           prebuilt dylib + source - Obj-C swizzle (gen-2, GameMaker)
    shims/gen0_objc/           prebuilt dylib + source - Obj-C swizzle (gen-0, Unreal)
```

Each SDK generation and engine is its own module. Adding a new one, on either platform, is a
new module (Android) or a new handler registered in `ios/handlers.py` (iOS).

## Requirements

The iOS path needs only Python 3. The Android path also needs:

- **JDK 11+**, from [Adoptium](https://adoptium.net). Point `--java` at it, set `JAVA_HOME`,
  or leave it to `java` on `PATH`.
- **apktool**, `apktool.jar` from [apktool.org](https://apktool.org/docs/install), via `--apktool`.
- **APKEditor**, `APKEditor.jar` from its [releases](https://github.com/REAndroid/APKEditor/releases),
  via `--apkeditor`. Used to merge split bundles.
- **Android build-tools** (`zipalign`, `apksigner`), from Android Studio's SDK Manager or the
  [command-line tools](https://developer.android.com/tools/releases/cmdline-tools). Picked up
  automatically when `ANDROID_HOME` is set.
- **A keystore.** Any keystore works; Android Studio ships one at `~/.android/debug.keystore`
  (alias `androiddebugkey`, passwords `android`). To make your own:

  ```
  keytool -genkeypair -v -keystore debug.keystore -storepass android -keypass android -alias androiddebugkey -keyalg RSA -keysize 2048 -validity 10000 -dname "CN=Android Debug, O=Android, C=US"
  ```

Tool paths resolve in this order: CLI flags, `config.local.json` next to `unlock_netflix.py`,
environment variables, then autodetection from `ANDROID_HOME`/`JAVA_HOME`/`PATH`. A config
file sets them once:

```jsonc
{
  "java": "/path/to/jdk/",
  "apktool": "/path/to/apktool.jar",
  "apkeditor": "/path/to/APKEditor.jar",
  "android_sdk": "/path/to/Android/Sdk",
  "keystore": "/path/to/debug.keystore",
  "ks_pass": "android", "ks_alias": "androiddebugkey", "key_pass": "android"
}
```

## Limitations

This only removes the Netflix login gate. A game with its own server dependency can still
fail after the gate is gone: a Firebase Remote Config economy (which also breaks on
re-signing), a dead game server, or streamed content. Self-contained single-player games
generally work. Run the output to check.

## Disclaimer

For preservation and personal offline use of games you own. It removes a dead authentication
gate and nothing else.
