"""Gen-2 com.netflix.games SDK with no engine glue at all: patch the SDK's own AccessApi
and BlobStoreApi implementations directly (grant player access, offline blob store)."""
import sys
from .smali import (find_smali_file, _decl_file, _decl_files_all, patch_method,
                    _PAI, _RES, _ERR, _CB, _RES_CTOR, _RESCOMP, _ACCESS_API, _BLOB_API)
from .gen0 import _g0_slot_store_smali
from .gamemaker import _GM2_READRES, _GM2_WRITERES, _GM2_BLOBC, _GM2_CONF, _GM2_DELRES


def is_gen2_nobridge(dec):
    """gen-2 com.netflix.games SDK whose AccessApi impl the game calls directly (no engine glue)."""
    return (find_smali_file(dec, _ACCESS_API) is not None
            and find_smali_file(dec, _BLOB_API) is not None
            and _decl_file(dec, _ACCESS_API, "implements") is not None
            and _decl_file(dec, _BLOB_API, "implements") is not None)

def patch_gen2_nobridge(dec, report):
    """Patch the SDK's obfuscated AccessApi + BlobStoreApi impls in place (engine-agnostic gen-2)."""
    def note(status, name):
        report[status].append(name)

    # 1. suppress the SDK error dialog (3-arg gen-2 variant)
    ef = find_smali_file(dec, "com/netflix/mediaclient/ui/errors/SdkErrorActivity$Companion")
    if ef is not None:
        new, st = patch_method(ef.read_text(encoding="utf-8"), "startSdkErrorActivity(", 0, ["return-void"])
        if st == "patched":
            ef.write_text(new, encoding="utf-8")
        note(st, "suppress-error-screen")

    # 2. grant player access on every AccessApi impl
    grant = [
        f"new-instance v0, {_PAI}", 'const-string v1, "offline-player"',
        f"invoke-direct {{v0, v1}}, {_PAI}-><init>(Ljava/lang/String;)V",
        f"new-instance v1, {_RES}", "const/4 v2, 0x0",
        f"invoke-direct {{v1, v0, v2}}, {_RES_CTOR}",
        f"invoke-interface {{p1, v1}}, {_CB}->onResult({_RES})V", "return-void"]
    hit = False
    for f in _decl_files_all(dec, _ACCESS_API):
        new, st = patch_method(f.read_text(encoding="utf-8"),
                               "requestPlayerAccess(Lcom/netflix/games/Callback;)V", 3, grant)
        if st in ("patched", "already"):
            hit = True
            if st == "patched":
                f.write_text(new, encoding="utf-8")
            note(st, "grant-player-access")
    if not hit:
        sys.exit("! gen-2 no-bridge: no AccessApi impl with requestPlayerAccess(Callback) found")

    # 3. local blob store on every BlobStoreApi impl
    def bodies(store):
        read = [
            f"invoke-static {{p1}}, {store}->read(Ljava/lang/String;)Ljava/lang/String;", "move-result-object v0",
            "if-eqz v0, :nfx_miss",
            "const/4 v1, 0x2",  # Base64.NO_WRAP
            "invoke-static {v0, v1}, Landroid/util/Base64;->decode(Ljava/lang/String;I)[B", "move-result-object v1",
            f"new-instance v0, {_GM2_BLOBC}", f"invoke-direct {{v0, v1}}, {_GM2_BLOBC}-><init>([B)V",
            f"new-instance v1, {_GM2_READRES}", "const/4 v2, 0x0",
            f"invoke-direct {{v1, v0, v2}}, {_GM2_READRES}-><init>({_GM2_BLOBC}{_GM2_CONF})V",
            f"new-instance v0, {_RES}", f"invoke-direct {{v0, v1, v2}}, {_RES_CTOR}", "goto :nfx_deliver",
            ":nfx_miss",
            f"new-instance v1, {_ERR}", "const/16 v2, -0x3e9", 'const-string v3, "not found"',
            f"invoke-direct {{v1, v2, v3}}, {_ERR}-><init>(ILjava/lang/String;)V",
            "const/4 v2, 0x0", f"new-instance v0, {_RES}", f"invoke-direct {{v0, v2, v1}}, {_RES_CTOR}",
            ":nfx_deliver", f"invoke-interface {{p2, v0}}, {_CB}->onResult({_RES})V", "return-void"]
        write = [
            f"invoke-virtual {{p2}}, {_GM2_BLOBC}->getBlob()[B", "move-result-object v0",
            "const/4 v1, 0x2",  # Base64.NO_WRAP
            "invoke-static {v0, v1}, Landroid/util/Base64;->encodeToString([BI)Ljava/lang/String;", "move-result-object v0",
            f"invoke-static {{p1, v0}}, {store}->write(Ljava/lang/String;Ljava/lang/String;)V",
            f"new-instance v0, {_GM2_WRITERES}", "const/4 v1, 0x0",
            f"invoke-direct {{v0, v1}}, {_GM2_WRITERES}-><init>({_GM2_CONF})V",
            f"new-instance v2, {_RES}", f"invoke-direct {{v2, v0, v1}}, {_RES_CTOR}",
            f"invoke-interface {{p3, v2}}, {_CB}->onResult({_RES})V", "return-void"]
        getids = [
            f"invoke-static {{}}, {store}->list()Ljava/util/ArrayList;", "move-result-object v0",
            f"sget-object v1, {_RES}->Companion:{_RESCOMP}",
            f"invoke-virtual {{v1, v0}}, {_RESCOMP}->withData(Ljava/lang/Object;){_RES}", "move-result-object v0",
            f"invoke-interface {{p1, v0}}, {_CB}->onResult({_RES})V", "return-void"]
        delete = [
            f"invoke-static {{p1}}, {store}->delete(Ljava/lang/String;)V",
            f"new-instance v0, {_GM2_DELRES}", "const/4 v1, 0x0",
            f"invoke-direct {{v0, v1}}, {_GM2_DELRES}-><init>({_GM2_CONF})V",
            f"new-instance v2, {_RES}", f"invoke-direct {{v2, v0, v1}}, {_RES_CTOR}",
            f"invoke-interface {{p2, v2}}, {_CB}->onResult({_RES})V", "return-void"]
        return read, write, getids, delete

    for f in _decl_files_all(dec, _BLOB_API):
        smdir = next(d for d in dec.glob("smali*") if f.is_relative_to(d))
        pkg = str(f.relative_to(smdir).parent).replace("\\", "/")
        store = f"L{pkg}/NfxBlobStore;"
        read, write, getids, delete = bodies(store)
        t = f.read_text(encoding="utf-8")
        t, s1 = patch_method(t, "readPlayerBlob(Ljava/lang/String;Lcom/netflix/games/Callback;)V", 4, read)
        t, s2 = patch_method(t, "writePlayerBlob(Ljava/lang/String;Lcom/netflix/games/storage/blobs/BlobContainer;Lcom/netflix/games/Callback;)V", 3, write)
        t, s3 = patch_method(t, "getPlayerBlobs(Lcom/netflix/games/Callback;)V", 2, getids)
        t, s4 = patch_method(t, "deletePlayerBlob(Ljava/lang/String;Lcom/netflix/games/Callback;)V", 3, delete)
        if "patched" in (s1, s2, s3, s4):
            f.write_text(t, encoding="utf-8")
            (f.parent / "NfxBlobStore.smali").write_text(
                _g0_slot_store_smali(None).replace("com/netflix/unity/impl/NfxSlotStore", store[1:-1]),
                encoding="utf-8")
            note("patched", "local-blob-store")
        elif "already" in (s1, s2, s3, s4):
            note("already", "local-blob-store")
