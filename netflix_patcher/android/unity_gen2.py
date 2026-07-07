"""Newer (2025) com.netflix.games SDK reached through the Unity bridge
(com.netflix.unity.impl.NfUnitySdkInternal): request-and-grant player access, plus
cloud-save read/list/write offline stubs."""
from .smali import _PAI, _RES, _ERR, _RES_CTOR, _ONRESULT


PATCHES = [
    {   # the login wall. hand back a granted result and skip the dead handshake.
        "name": "grant-player-access",
        "class": "com/netflix/unity/impl/NfUnitySdkInternal",
        "sig": "doRequestPlayerAccess(Lcom/netflix/games/Callback;)V",
        "critical": True, "locals": 3,
        "body": [
            "# pretend the player is a signed-in member",
            f"new-instance v0, {_PAI}",
            'const-string v1, "offline-player"',
            f"invoke-direct {{v0, v1}}, {_PAI}-><init>(Ljava/lang/String;)V",
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
