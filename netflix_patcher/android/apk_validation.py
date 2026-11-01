"""Checks for accidental payload loss when merging split APKs."""
import zipfile


def validate_merged_payload(inputs, output):
    """Assets and native libraries must survive unchanged at their original paths.

    Originally stored entries must remain uncompressed for AssetManager.openFd
    and native mmap users; equal uncompressed bytes alone are insufficient.

    Resource table entries may legitimately be renamed by APKEditor, so only
    require the table itself here. ZIP CRC/size checks detect accidental loss;
    they are not a cryptographic authenticity check.
    """
    expected = {}
    has_table = False
    for apk in inputs:
        with zipfile.ZipFile(apk) as z:
            has_table |= "resources.arsc" in z.namelist()
            for entry in z.infolist():
                if entry.is_dir() or not entry.filename.startswith(("assets/", "lib/")):
                    continue
                value = (entry.file_size, entry.CRC, entry.compress_type == zipfile.ZIP_STORED)
                previous = expected.get(entry.filename)
                if previous is not None and previous[:2] != value[:2]:
                    raise RuntimeError(f"Conflicting split payload: {entry.filename} ({apk})")
                if previous is not None:
                    value = (*value[:2], previous[2] or value[2])
                expected[entry.filename] = value
    with zipfile.ZipFile(output) as z:
        actual = {e.filename: (e.file_size, e.CRC, e.compress_type) for e in z.infolist()}
    missing = []
    for name, (size, crc, must_store) in expected.items():
        value = actual.get(name)
        if (value is None or value[:2] != (size, crc)
                or (must_store and value[2] != zipfile.ZIP_STORED)):
            missing.append(name)
    if has_table and "resources.arsc" not in actual:
        missing.append("resources.arsc")
    if missing:
        raise RuntimeError("Split merge lost or changed payload: " + ", ".join(missing[:10]) +
                           "; inspect --keep-work before installing")
    print(f"      merge verified: {len(expected)} asset/library entries preserved")
