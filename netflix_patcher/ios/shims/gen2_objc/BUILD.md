# Building the gen-2 Obj-C/Swift iOS shim (`NetflixOffline.framework`)

Prebuilt arm64 dylib for **gen-2 `com.netflix.games` titles that reach the SDK through its Swift
`@objc` API** (`NetflixGames.framework`) rather than the `ngp_*` C ABI - i.e. GameMaker titles. See
the header of `netflix_offline_gen2_objc.c` for how it works (load-time swizzle of the SDK's
`AccessProvider`, `NGPProfilesProvider`, and `CloudSavesProvider` methods: grant player access via
whichever model the title uses - completion or event - return an offline profile, and back the blob
store with a local file store). Only rebuild if you change the source.

Like the gen-0 Obj-C shim it references no SDK symbol at link time (classes are looked up by name
at runtime), so it needs **no** framework to link against; the Obj-C runtime, libc, and
libdispatch resolve from libSystem's re-exports via `-undefined dynamic_lookup`. It also creates a
couple of small duck-typed classes at runtime (`objc_allocateClassPair`) for the offline profile
and the granted access event.

```sh
clang -target arm64-apple-ios10.0 -O2 -fno-stack-protector -fno-exceptions -ffreestanding \
      -Wno-incompatible-sysroot -c netflix_offline_gen2_objc.c -o gen2_objc.o

ld64.lld -arch arm64 -dylib -o NetflixOffline.framework/NetflixOffline gen2_objc.o \
      -install_name "@rpath/NetflixOffline.framework/NetflixOffline" \
      -platform_version ios 10.0 17.0 -undefined dynamic_lookup
```

Then add two load commands with LIEF: `libSystem` (dyld requires every image to link it; ld64.lld
doesn't add it for a `dynamic_lookup` dylib) **and a dependency on `NetflixGames.framework`**. The
second is load-bearing: `NetflixGames` is a **Swift** framework, and if this shim's constructor
runs before that framework's Swift initializers the app crashes at launch. Declaring the dependency
forces dyld to initialize `NetflixGames` first (dependency-before-dependent), which is why the
constructor may safely install its swizzles (the actual synthetic-object creation is deferred to
`lazy_init()` on first use as a second line of defense). The path matches the framework's install
name, identical across gen-2 titles.

```python
import lief
p = "NetflixOffline.framework/NetflixOffline"
b = lief.MachO.parse(p).at(0)
names = [ (c.name or "") for c in b.commands ]
if not any("libSystem" in n for n in names):
    b.add_library("/usr/lib/libSystem.B.dylib")
if not any("NetflixGames.framework" in n for n in names):
    b.add_library("@rpath/NetflixGames.framework/NetflixGames")   # init-order dependency (Swift fw)
b.write(p)
```

Build it on Windows/Linux with the Swift.org toolchain's `clang` + `ld64.lld` (no Xcode).
`Info.plist` is a minimal framework plist; the sideloader re-signs it. Every class/selector lookup
is guarded, so the same binary fits both the older (completion-access) and newer (event-access)
gen-2 SDK builds; the tool matches its min-OS to the app at patch time.
