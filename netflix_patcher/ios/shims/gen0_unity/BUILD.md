# Building the gen-0 iOS shim (`NetflixOffline.framework`)

Prebuilt arm64 dylib for the gen-0 NGP SDK (`NGP.framework`). See the header of
`netflix_offline_gen0.c` for how it works (event-driven auth + a local slot store) and the
JSON formats. Only rebuild if you change the source.

Unlike the gen-2 shim this one uses libc (file I/O for the slot store), so it needs
`-undefined dynamic_lookup` for the libSystem symbols while still linking two-level against
`NGP.framework` for the `ngp_*` targets:

```sh
clang -target arm64-apple-ios12.0 -O2 -fno-stack-protector -fno-exceptions -ffreestanding \
      -c netflix_offline_gen0.c -o netflix_offline_gen0.o

ld64.lld -arch arm64 -dylib -o NetflixOffline.framework/NetflixOffline \
      netflix_offline_gen0.o \
      /path/to/AnyTitle.app/Frameworks/NGP.framework/NGP \
      -install_name "@rpath/NetflixOffline.framework/NetflixOffline" \
      -platform_version ios 12.0 17.0 \
      -undefined dynamic_lookup
```

Then add the `libSystem` load command if the linker did not (see the gen-2 `BUILD.md` for the
LIEF snippet). The `ngp_*` symbols are `weak_import`, so one binary fits any gen-0 NGP title.
