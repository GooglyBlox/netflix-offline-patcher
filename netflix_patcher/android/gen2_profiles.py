"""Offline ProfilesApi support shared by gen-2 SDK entry points."""
import sys

from .smali import (_RES, _decl_files_all, emit_offline_profile,
                    find_smali_file, offline_profile_body, patch_method)


_PROFILES_API = "com/netflix/games/player/profiles/ProfilesApi"


def patch_current_profile(dec, report):
    """Patch every direct implementation, including wrappers in other dex files.

    Access success does not initialize NetflixPlatform. Serve the profile locally
    rather than forging internal readiness flags or depending on startup timing.
    """
    impls = _decl_files_all(dec, _PROFILES_API)
    if not impls and find_smali_file(dec, _PROFILES_API) is None:
        report["skipped"].append("grant-offline-profile (no ProfilesApi)")
        return
    if not impls:
        sys.exit("! gen-2 ProfilesApi present but no implementation found (SDK layout changed?)")
    cls = emit_offline_profile(dec)
    if cls is None:
        sys.exit("! unsupported gen-2 CurrentProfile contract; cannot safely supply an offline profile. "
                 "Inspect CurrentProfile constructor/abstract methods with --keep-work.")
    sig = f"getCurrentProfile(){_RES}"
    for impl in impls:
        new, status = patch_method(impl.read_text(encoding="utf-8"), sig, 2,
                                   offline_profile_body(cls))
        if status == "not_found":
            sys.exit(f"! gen-2 profile method missing: {impl}::{sig} (SDK layout changed?)")
        if status == "patched":
            impl.write_text(new, encoding="utf-8")
        print(f"      offline profile: {impl.relative_to(dec)} ({status})")
        report[status].append("grant-offline-profile")
