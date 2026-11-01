# Direct gen-2 SDK startup: Into the Breach

## Root cause and evidence

The supplied `com.netflix.NGP.IntoTheBreach` XAPK contains Netflix Games SDK
`1.11.3+17.8a5a540e`. These identifiers describe the inspected artifact; the patch
selects public SDK types/signatures rather than package names or version strings.
No APK contents, keys or game-specific fixtures are committed.

The original generic recipe changed `AccessApi.requestPlayerAccess(Callback)` to
return PlayerAccessInfo immediately, suppressed the SDK error UI and redirected blob
storage. It did not run the current-profile patch already used on the Unity path.
That made two public SDK surfaces disagree about the session:

1. The game's `MainActivity.requestNetflixPlayerAccess` sets a request-in-flight flag
   and calls AccessApi. Its success callback calls `tryToGetCurrentUser` immediately.
2. `ProfilesApi.getCurrentProfile` delegates to a provider which checks the internal
   NetflixPlatform readiness flag. When false it returns error -2,
   `Platform not initialized`; a missing signed-in user is a separate subsequent check.
3. The game's profile error branch logs the error and returns. It does not store an
   active profile or call `nativeOnProfileChanged`, which occurs on the success path
   after a changed player ID. Thus the native engine never gets that startup signal.

This is an incomplete offline session emulation, not evidence that a delay or a fabricated
profile ID will initialize the real platform. The supplied log and traced branches establish
this startup blocker. They do not prove it is the only possible blocker in native gameplay.

The original SDK access implementation launches a coroutine on IO and dispatches its
result through `Dispatchers.Main`. The previous patch invoked the callback inline, which
also changed reentrancy/thread behavior. Unity's bridge patch has its own existing result
path; applying a generic scheduler to that bridge without equivalent evidence would expand
the change unnecessarily.

## Implementation and compatibility

- `gen2_profiles.py` supplies a local current-profile result for both Unity gen-2 and
  direct SDK calls. It patches every direct ProfilesApi implementation across dex files
  and matches the complete return signature. If ProfilesApi is absent, access-only
  SDKs remain supported. If it is present but unsupported, patching stops with a useful
  diagnostic. The generated CurrentProfile subclass is checked for the known constructor,
  final-class restrictions and unknown abstract methods before emission.
- `smali.py` retains the existing shared offline identity for access/profile/legacy
  profile fields, preserving save identity. This is a synthetic local identity, never an
  assumption about a real Netflix account. The existing display name/language defaults
  remain unchanged. One- and two-argument PlayerAccessInfo constructors stay supported.
- `gen2_callbacks.py` queues each patched direct access/blob response once on the main
  looper. It uses only Android APIs and keeps callback exceptions visible. A rejected
  queue post fails explicitly rather than silently losing a completion.
- `gen2_nobridge.py` applies both surfaces and logs local access/profile activity under
  `NfxOffline`. Suppressed SDK error screens leave a warning, so suppression no longer
  hides all evidence of a startup failure.
- Real SDK startup, dependency-graph construction, platform flags, event registration,
  game Java/JNI and native code are untouched. No synthetic profile/access-change events
  are broadcast: the inspected game already queries the profile from its access callback.
  Other ProfilesApi operations (profile lists, language updates) are not emulated by this
  change. Titles requiring those operations or event-only startup need separate evidence.
- `tools.py` selects explicit aapt2 on pre-2.9 Apktool 2.x, and the default compiler on
  2.9+/3.x. An unreadable version fails explicitly; resource build failures are not retried
  with a different compiler. Official references: [2.x CLI](https://apktool.org/docs/2.x/cli-parameters/),
  [3.0 migration](https://apktool.org/blog/apktool-3.0.0/).
- `pipeline.py` requires APKEditor for splits even with `--no-sign`, while single APKs
  no longer require an unused merger. `apk_validation.py` checks assets/native libraries
  for lost/changed paths, size/CRC changes, conflicting split payloads and compression
  changes that could break openFd/mmap. Resource-table presence is checked, but resource
  ID correctness and Play delivery state still require runtime testing.

No new runtime downloads, permissions or network dependencies are introduced.

## Validation performed

- 21 dependency-free unittest checks cover generic/Unity profile sharing, multiple dex
  implementations, stable identity, both access constructors, idempotence, unsupported
  profiles, deferred callback emission, Apktool flag selection/error handling and split
  payload corruption/compression/tool requirements. These are generated-smali/ZIP tests;
  they do not execute Android callbacks or prove ART verification.
- The supplied original XAPK was patched, all eight dex files assembled, the base rebuilt,
  all four splits merged and the candidate signed in the existing Debian/Java 17 Docker
  image, using Apktool 3.0.3 and APKEditor 1.4.9.
- The signed candidate passed `apksigner verify --verbose` (v3 signature),
  `zipalign -c -p 4`, and Python ZIP CRC validation. All 651 expected asset/library entries
  are present unchanged. The eight resource-pack assets total 496,842,978 bytes; the large
  resource and audio files remain uncompressed. The prior broken output also passes the
  payload-preservation check, so losing those assets is not supported as the cause here.
- APKEditor/zipalign emitted ZIP header-mismatch warnings during the merge/alignment
  phase; the final signed ZIP passed the CRC, alignment and signature checks above.
- No device install/launch or gameplay/save round-trip has been performed. Success on a
  device is still unverified. Older Apktool flag branches are unit-tested, not a full
  historical-tool build matrix. Other titles have synthetic regression coverage only.

## Existing Docker wrapper

The surrounding Compose wrapper clones upstream during its image build. To test this
checkout with its already-built image, overlay the source at runtime (run these commands
from the directory containing `compose.yaml`; substitute the actual checkout path):

```sh
# Build the existing wrapper/toolchain if its image is not already present.
# The source overlay below selects this checkout independently of PATCHER_REF.
docker compose build

docker compose run --rm --entrypoint python3 \
  -v "$(pwd)/upstream:/opt/netflix-offline-patcher:ro" \
  patcher -m unittest discover -s tests -v

docker compose run --rm \
  -v "$(pwd)/upstream:/opt/netflix-offline-patcher:ro" \
  patcher /input/into-the-breach.xapk -o /output/into-the-breach-candidate.apk

docker compose run --rm --entrypoint apksigner \
  patcher verify --verbose /output/into-the-breach-candidate.apk
docker compose run --rm --entrypoint zipalign \
  patcher -c -p 4 /output/into-the-breach-candidate.apk
```

Keep the existing `keys` volume. Use a separate output filename. The wrapper's default
runtime network isolation remains appropriate. A source overlay avoids rebuilding the
wrapper's current Dockerfile, which contains a brittle text replacement for `--use-aapt2`.
Remove that replacement before building the wrapper against this branch/fork, and set its
clone URL/ref to the desired reviewed source. Increasing `/tmp` tmpfs and memory may be
necessary for large bundles. `--keep-work` files live inside the container; omit `--rm`
when they must survive for inspection, then remove the stopped container afterwards.

## Remaining device validation

Use an expendable device/profile or back up saves first. An official installation uses a
different signer; do not uninstall it merely to test without accounting for save loss.
An earlier locally signed patch can usually be updated with the same persistent key.

```sh
adb install -r -t into-the-breach-candidate.apk
adb logcat -v threadtime
```

Launch the game offline. Expect local access/profile messages and the game's new profile
ID message, followed by native initialization/main menu. Check for VerifyError,
AbstractMethodError, callback thread exceptions and asset/audio loading failures. Test a
new save, force-stop/relaunch, and confirm progress persists. Repeat on at least one Unity
gen-2 title and one other direct-SDK title before claiming broad device compatibility.
If startup still hangs after a successful profile, collect the surrounding native/asset
logs and inspect resource-table references, AssetManager/openFd usage and Play asset-pack
lookups rather than altering platform-ready flags blindly.
