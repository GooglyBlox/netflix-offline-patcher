# Building the iOS shim (`NetflixOffline.framework`)

`NetflixOffline.framework/NetflixOffline` (here, next to this file) is a prebuilt arm64
dylib. The tool ships it prebuilt so you don't need an Apple toolchain to patch an IPA. You
only need to rebuild if you change `netflix_offline_gen2.c`.

It's a tiny dyld-interpose shim: it overrides the Netflix SDK's `ngp_*` C-ABI functions
with local ones that hand back a "granted / offline" result. See the header comment in
`netflix_offline_gen2.c` for how it works and the exact JSON wire formats.

## Requirements

- **clang** that can target `arm64-apple-ios` (the Swift.org toolchain's clang works on
  Windows/Linux; Xcode's clang works on macOS).
- **ld64.lld** (ships with the Swift toolchain / LLVM) or Apple's `ld` on macOS.
- A copy of any title's `NetflixGames` binary to link against (only its export table is
  read; the result is title-independent because the SDK's install name and `ngp_*`
  symbols are identical across every gen-2 title).

## Build

```sh
clang -target arm64-apple-ios12.0 -O2 -fno-stack-protector -fno-exceptions -ffreestanding \
      -c netflix_offline_gen2.c -o netflix_offline_gen2.o

ld64.lld -arch arm64 -dylib -o NetflixOffline.framework/NetflixOffline \
      netflix_offline_gen2.o \
      /path/to/AnyTitle.app/Frameworks/NetflixGames.framework/NetflixGames \
      -install_name "@rpath/NetflixOffline.framework/NetflixOffline" \
      -platform_version ios 12.0 17.0
```

Then make sure the dylib links `libSystem` (dyld requires every image to). If your linker
didn't add it, add it after the fact, e.g. with LIEF:

```python
import lief
b = lief.MachO.parse("NetflixOffline.framework/NetflixOffline").at(0)
if not any("libSystem" in (c.name or "") for c in b.commands):
    b.add_library("/usr/lib/libSystem.B.dylib"); b.write("NetflixOffline.framework/NetflixOffline")
```

The `ngp_*` symbols are `weak_import`, so linking against one title's `NetflixGames` gives
a binary that still loads against any other title (a symbol a given game lacks binds to
NULL and its interpose becomes a no-op).

## Info.plist

`NetflixOffline.framework/Info.plist` is a minimal framework plist. It only needs
`CFBundleExecutable = NetflixOffline` and an identifier; the sideloader re-signs it.
