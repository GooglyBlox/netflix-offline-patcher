# Building the gen-0 Obj-C iOS shim (`NetflixOffline.framework`)

Prebuilt arm64 dylib for **gen-0 NGP titles that reach the SDK through its Objective-C facade**
(`NGP.framework`, class `NetflixSDK`) rather than the `ngp_*` C ABI - i.e. Unreal Engine titles.
See the header of `netflix_offline_gen0_objc.c` for how it works (load-time swizzle of the
`NetflixSDK` class methods: deliver a synthetic signed-in `NetflixSDKState` to the game's event
receiver, plus a local slot store). Only rebuild if you change the source.

Unlike the C-ABI shims this one does not reference any SDK symbol at link time - it looks the SDK
classes up by name at runtime with `objc_getClass`, so it needs **no** framework to link against.
It uses the Obj-C runtime, libc, and libdispatch, all resolved from libSystem's re-exports via
`-undefined dynamic_lookup`.

```sh
clang -target arm64-apple-ios10.0 -O2 -fno-stack-protector -fno-exceptions -ffreestanding \
      -Wno-incompatible-sysroot -c netflix_offline_gen0_objc.c -o gen0_objc.o

ld64.lld -arch arm64 -dylib -o NetflixOffline.framework/NetflixOffline gen0_objc.o \
      -install_name "@rpath/NetflixOffline.framework/NetflixOffline" \
      -platform_version ios 10.0 17.0 -undefined dynamic_lookup
```

Then add the `libSystem` load command (dyld requires every image to link it; ld64.lld does not add
it for a `dynamic_lookup` dylib). With LIEF:

```python
import lief
b = lief.MachO.parse("NetflixOffline.framework/NetflixOffline").at(0)
if not any("libSystem" in (c.name or "") for c in b.commands):
    b.add_library("/usr/lib/libSystem.B.dylib"); b.write("NetflixOffline.framework/NetflixOffline")
```

The Swift.org toolchain's `clang` + `ld64.lld` build this on Windows/Linux (no Xcode). `Info.plist`
is a minimal framework plist (`CFBundleExecutable = NetflixOffline`); the sideloader re-signs it.
The shim targets classes/selectors by name and guards every lookup, so one binary fits any gen-0
Obj-C title; the tool matches its min-OS to the app at patch time.
