"""Minimal Mach-O surgery: add an LC_LOAD_DYLIB to every 64-bit slice in place, writing
into the load-command header padding so segments, __LINKEDIT and the (stale) signature
stay byte-for-byte. The sideloader re-signs afterwards."""
from pathlib import Path


FAT_MAGICS = (0xCAFEBABE, 0xBEBAFECA, 0xCAFEBABF, 0xBFBAFECA)

MH_MAGIC_64, MH_CIGAM_64 = 0xFEEDFACF, 0xCFFAEDFE

LC_LOAD_DYLIB = 0x0C

CPU_TYPE_ARM64 = 0x0100000C

def _macho_thin_slices(data):
    """Yield (start_offset, is_le) for each Mach-O in data (thin file or fat container)."""
    magic = int.from_bytes(data[:4], "big")
    if magic in FAT_MAGICS:
        nfat = int.from_bytes(data[4:8], "big")
        for i in range(nfat):
            off = int.from_bytes(data[8 + i * 20 + 8:8 + i * 20 + 12], "big")
            yield off, None
    else:
        yield 0, None

def _inject_load_dylib_slice(data, slice_off, load_name):
    """Surgically add one LC_LOAD_DYLIB to the Mach-O at data[slice_off:]. Writes into the
    header padding after the load commands, so nothing else in the file moves - segments,
    __LINKEDIT and the (now stale, to-be-re-signed) code signature stay byte-for-byte.
    Returns 'added', 'already', 'noroom', or 'skip' (non-arm64/64-bit slice)."""
    if int.from_bytes(data[slice_off:slice_off + 4], "little") != MH_MAGIC_64:
        return "skip"   # 32-bit or big-endian; iOS runs little-endian 64-bit only
    import struct as _s
    ncmds, sizeofcmds = _s.unpack_from("<II", data, slice_off + 16)
    lc_start = slice_off + 0x20
    lc_end = lc_start + sizeofcmds

    # already injected? and find where section data begins (end of usable header pad)
    off = lc_start
    min_file = None
    for _ in range(ncmds):
        cmd, cmdsize = _s.unpack_from("<II", data, off)
        if cmd in (LC_LOAD_DYLIB, 0x0F, 0x18, 0x1C):  # LOAD_DYLIB / ID / LOAD_WEAK / REEXPORT
            name_off = _s.unpack_from("<I", data, off + 8)[0]
            nm = data[off + name_off: off + cmdsize].split(b"\0")[0]
            if nm == load_name.encode():
                return "already"
        if cmd == 0x19:  # LC_SEGMENT_64
            nsects = _s.unpack_from("<I", data, off + 64)[0]
            so = off + 72
            for _s2 in range(nsects):
                s_off = _s.unpack_from("<I", data, so + 48)[0]
                if s_off > 0:
                    # section offsets are relative to the slice; make file-absolute
                    file_s = slice_off + s_off
                    if min_file is None or file_s < min_file:
                        min_file = file_s
                so += 80
        off += cmdsize

    name = load_name.encode() + b"\0"
    cmdsize = (24 + len(name) + 7) // 8 * 8
    pad_limit = (min_file if min_file is not None else lc_end)
    if pad_limit - lc_end < cmdsize:
        return "noroom"
    # header pad must be clear where we write
    if any(data[lc_end:lc_end + cmdsize]):
        return "noroom"
    cmd = bytearray(cmdsize)
    _s.pack_into("<II", cmd, 0, LC_LOAD_DYLIB, cmdsize)
    _s.pack_into("<IIII", cmd, 8, 24, 2, 0x10000, 0x10000)  # name off, timestamp, cur 1.0.0, compat 1.0.0
    cmd[24:24 + len(name)] = name
    data[lc_end:lc_end + cmdsize] = cmd
    _s.pack_into("<II", data, slice_off + 16, ncmds + 1, sizeofcmds + cmdsize)
    return "added"

def macho_add_load_dylib(path, load_name):
    """Add LC_LOAD_DYLIB(load_name) to every 64-bit slice of a Mach-O in place."""
    data = bytearray(Path(path).read_bytes())
    results = [_inject_load_dylib_slice(data, off, load_name) for off, _ in _macho_thin_slices(data)]
    if "added" in results:
        Path(path).write_bytes(data)
    return results


LC_BUILD_VERSION = 0x32
LC_VERSION_MIN_IPHONEOS = 0x25


def _pack_version(version_str):
    """"10.0" / "16.2" / "9" -> the uint32 (major<<16)|(minor<<8)|patch. None if unparseable."""
    try:
        parts = [int(x) for x in (str(version_str).split(".") + ["0", "0"])[:3]]
    except ValueError:
        return None
    return (parts[0] << 16) | (parts[1] << 8) | parts[2]


def macho_set_min_os(path, version_str):
    """Set the minimum-OS field of every slice's LC_BUILD_VERSION / LC_VERSION_MIN_IPHONEOS in
    place (a single uint32, no layout change). iOS refuses to install an app whose embedded
    framework requires a newer OS than the app, so we match the shim to the target."""
    ver = _pack_version(version_str)
    if ver is None:
        return False
    import struct as _s
    data = bytearray(Path(path).read_bytes())
    changed = False
    for slice_off, _ in _macho_thin_slices(data):
        if int.from_bytes(data[slice_off:slice_off + 4], "little") != MH_MAGIC_64:
            continue
        ncmds = _s.unpack_from("<I", data, slice_off + 16)[0]
        off = slice_off + 0x20
        for _ in range(ncmds):
            cmd, cmdsize = _s.unpack_from("<II", data, off)
            if cmd == LC_BUILD_VERSION:
                _s.pack_into("<I", data, off + 12, ver); changed = True   # minos
            elif cmd == LC_VERSION_MIN_IPHONEOS:
                _s.pack_into("<I", data, off + 8, ver); changed = True     # version
            off += cmdsize
    if changed:
        Path(path).write_bytes(data)
    return changed
