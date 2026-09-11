"""Newer (2025) com.netflix.games SDK reached through the Unity bridge
(com.netflix.unity.impl.NfUnitySdkInternal): request-and-grant player access, plus
cloud-save read/list/write offline stubs."""
from .smali import (_RES, _ERR, _RES_CTOR, _ONRESULT, _decl_file, patch_method,
                    emit_offline_profile, offline_profile_body, emit_player_access_info)


_PROFILES_API = "com/netflix/games/player/profiles/ProfilesApi"


def patch_current_profile(dec, report):
    """Synthetic profile from getCurrentProfile(), patched in the ProfilesApi impl. A title can
    gate boot on the profile rather than on access, and spin forever on the offline error."""
    cls = emit_offline_profile(dec)
    impl = _decl_file(dec, _PROFILES_API, "implements")
    if cls is None or impl is None:
        report["not_found"].append("grant-offline-profile")
        return
    new, st = patch_method(impl.read_text(encoding="utf-8"), "getCurrentProfile()", 3,
                           offline_profile_body(cls))
    if st == "patched":
        impl.write_text(new, encoding="utf-8")
    report[st].append("grant-offline-profile")


def build_patches(dec):
    """Gen-2 patches. Per-APK: the PlayerAccessInfo ctor varies."""
    grant = ["# pretend the player is a signed-in member"]
    grant += emit_player_access_info(dec, "v0", "v1", "v2")
    grant += [
        f"new-instance v1, {_RES}",
        "const/4 v2, 0x0",
        f"invoke-direct {{v1, v0, v2}}, {_RES_CTOR}",
        f"invoke-interface {{p1, v1}}, {_ONRESULT}",
        "return-void",
    ]
    out = []
    for p in PATCHES:
        p = dict(p)
        if p["name"] == "grant-player-access":
            p["body"] = grant
        out.append(p)
    return out


PATCHES = [
    {   # the login wall. hand back a granted result and skip the dead handshake.
        # body set by build_patches()
        "name": "grant-player-access",
        "class": "com/netflix/unity/impl/NfUnitySdkInternal",
        "sig": "doRequestPlayerAccess(Lcom/netflix/games/Callback;)V",
        "critical": True, "locals": 3,
        "body": [
            f"new-instance v1, {_RES}",
            "const/4 v2, 0x0",
            f"invoke-direct {{v1, v0, v2}}, {_RES_CTOR}",
            f"invoke-interface {{p1, v1}}, {_ONRESULT}",
            "return-void",
        ],
    },
    {   # the "Something went wrong" dead-end. every fatal path ends here, so kill it.
        "name": "suppress-error-screen",
        "class": "com/netflix/mediaclient/ui/errors/SdkErrorActivity$Companion",
        "sig": "startSdkErrorActivity(Landroid/content/Context;Landroid/os/Bundle;Ljava/lang/String;)V",
        "critical": True, "locals": 0,
        "body": ["return-void"],
    },
    {   # cloud-save read. no server, so say "no blob" right away instead of waiting forever.
        "name": "cloud-save-read-offline",
        "class": "com/netflix/unity/impl/NfUnitySdkInternal",
        "sig": "readBlob(Ljava/lang/String;Lcom/netflix/games/Callback;)V",
        "critical": False, "cloud_save": True, "locals": 4,
        "body": [
            "# no cloud blob, so the game starts a fresh local save",
            f"new-instance v0, {_ERR}",
            "const/16 v1, 0x194",
            'const-string v2, "offline"',
            f"invoke-direct {{v0, v1, v2}}, {_ERR}-><init>(ILjava/lang/String;)V",
            f"new-instance v1, {_RES}",
            "const/4 v3, 0x0",
            f"invoke-direct {{v1, v3, v0}}, {_RES_CTOR}",
            f"invoke-interface {{p2, v1}}, {_ONRESULT}",
            "return-void",
        ],
    },
    {   # cloud-save list. return nothing, there are no saved blobs.
        "name": "cloud-save-list-empty",
        "class": "com/netflix/unity/impl/NfUnitySdkInternal",
        "sig": "getBlobs(Lcom/netflix/games/Callback;)V",
        "critical": False, "cloud_save": True, "locals": 3,
        "body": [
            "new-instance v0, Ljava/util/ArrayList;",
            "invoke-direct {v0}, Ljava/util/ArrayList;-><init>()V",
            f"new-instance v1, {_RES}",
            "const/4 v2, 0x0",
            f"invoke-direct {{v1, v0, v2}}, {_RES_CTOR}",
            f"invoke-interface {{p1, v1}}, {_ONRESULT}",
            "return-void",
        ],
    },
    {   # cloud-save write. skip the dead server, it never calls back. the local save is what counts.
        "name": "cloud-save-write-offline",
        "class": "com/netflix/unity/impl/NfUnitySdkInternal",
        "sig": "writeBlob(Ljava/lang/String;Ljava/lang/String;Lcom/netflix/games/Callback;)V",
        "critical": False, "cloud_save": True, "locals": 4,
        "body": [
            f"new-instance v0, {_ERR}",
            "const/16 v1, 0x194",
            'const-string v2, "offline"',
            f"invoke-direct {{v0, v1, v2}}, {_ERR}-><init>(ILjava/lang/String;)V",
            f"new-instance v1, {_RES}",
            "const/4 v3, 0x0",
            f"invoke-direct {{v1, v3, v0}}, {_RES_CTOR}",
            f"invoke-interface {{p3, v1}}, {_ONRESULT}",
            "return-void",
        ],
    },
]
