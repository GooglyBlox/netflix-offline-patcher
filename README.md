# Netflix Offline Patcher

Built as an aide for the [Netflix Games Recovery Project](https://netflix.notaspider.dev).

Removes the Netflix Games SDK login gate from a Netflix game APK and produces a signed APK that runs offline with no Netflix account.

Accepts a `.apk`, `.apkm`, or `.xapk`. Split bundles are merged into a single APK.

## How it works

The login check lives in the Netflix Games SDK, not the game. Every Netflix title bundles the same SDK in `classes.dex` and calls into it over JNI, so one set of smali patches works regardless of engine (Unity, native, etc.).

The tool decodes the base APK with apktool and rewrites five SDK methods:

- `doRequestPlayerAccess` returns a granted result locally instead of calling Netflix.
- `startSdkErrorActivity` becomes a no-op, so the "Something went wrong" screen never opens.
- `readBlob`, `getBlobs`, and `writeBlob` return an offline "no cloud save" result instead of hanging on the dead server.

Netflix shipped three generations of this SDK, and the tool detects which one it is facing.

- **Newer (2025)** titles use the request-and-grant model above (`doRequestPlayerAccess`).
- **Older (2024)** titles have no `doRequestPlayerAccess` and instead gate on an access UI that has to be dismissed before boot continues. For those, the tool no-ops the error screen, returns a synthetic offline profile from `getCurrentProfile`, and fires the access-granted and access-UI-dismissed events once so the boot handshake completes. Some class names in that path are obfuscated, so it discovers them by their SDK supertypes rather than by name.
- **Oldest (2022-2023)** titles use the original `com.netflix.android.api` SDK, with none of the above. Auth is a `checkUserAuth` call whose result comes back as an `onUserStateChange` event, and identity is a `getCurrentPlayer` call. The tool returns a dummy player, fires a synthetic signed-in state, and forces every user-state event to read as authenticated. These titles also store progress *only* in the Netflix cloud-save slots (no local save), so the tool additionally replaces the dead slot API with a real local file store keyed to a stable offline identity, so saves persist across launches.

It then rebuilds the APK, merges any config splits with APKEditor, zipaligns, and signs with your keystore. It aborts if the SDK layout has drifted enough that a critical method can't be found.

## Usage

```
python unlock_netflix.py INPUT [-o OUTPUT]
adb install -t OUTPUT
```

Uninstall the store version first. The output is signed with a different key, so it won't install over the original.

### Options

```
-o PATH            output APK (default: <input>-offline.apk)
--keep-cloud-save  leave the cloud-save APIs unpatched
--no-sign          skip signing
--keep-work        keep the temp working directory
```

### Requirements

- **JDK 11+.** Install from [Adoptium](https://adoptium.net). Point `--java` at it, set `JAVA_HOME`, or leave it to `java` on `PATH`.
- **apktool.** Download `apktool.jar` from [apktool.org](https://apktool.org/docs/install) and point `--apktool` at the jar.
- **APKEditor.** Download `APKEditor.jar` from its [releases](https://github.com/REAndroid/APKEditor/releases) and point `--apkeditor` at the jar. Only used to merge split bundles and sign.
- **Android build-tools** (`zipalign`, `apksigner`). Install with Android Studio's SDK Manager or the standalone [command-line tools](https://developer.android.com/tools/releases/cmdline-tools) (`sdkmanager "build-tools;35.0.0"`). They land in `$ANDROID_HOME/build-tools/<version>/` and are picked up automatically when `ANDROID_HOME` is set.
- **A keystore.** Any keystore works. If you have Android Studio there is already one at `~/.android/debug.keystore` (alias `androiddebugkey`, store and key password `android`). To make your own:

  ```
  keytool -genkeypair -v -keystore debug.keystore -storepass android -keypass android -alias androiddebugkey -keyalg RSA -keysize 2048 -validity 10000 -dname "CN=Android Debug, O=Android, C=US"
  ```

Tool paths are resolved in this order: CLI flags, `config.local.json` next to the script, environment variables, then autodetection from `ANDROID_HOME`/`JAVA_HOME`/`PATH`. A config file next to the script sets them once so you can just run `python unlock_netflix.py INPUT`:

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

This only removes the Netflix login gate. A game with its own server dependency can still fail after the gate is gone: an economy backed by Firebase Remote Config (which also breaks on re-signing), a dead game server, or streamed content. Some titles also have profile or cloud-save logic that needs game-specific patching beyond what this does. Self-contained single-player games generally work. Run the output to check.

## Disclaimer

For preservation and personal offline use of games you own. It removes a dead authentication gate and nothing else.
