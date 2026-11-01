"""Synthetic SDK fixtures; no proprietary APK contents required."""
import tempfile
import unittest
from pathlib import Path

from netflix_patcher.android.gen2_nobridge import patch_gen2_nobridge
from netflix_patcher.android.gen2_profiles import patch_current_profile
from netflix_patcher.android.smali import OFFLINE_PLAYER_ID, emit_offline_profile
from netflix_patcher.android.unity_gen2 import patch_current_profile as unity_profile

ROOT = "com/netflix/games/"
RES = "Lcom/netflix/games/NetflixResult;"
CB = "Lcom/netflix/games/Callback;"


def report():
    return {k: [] for k in ("patched", "already", "not_found", "skipped")}


def method(sig, body="return-void"):
    return f".method public {sig}\n    .locals 1\n    {body}\n.end method\n"


class Gen2Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dec = Path(self.tmp.name)
        self.put(ROOT + "Callback", ".class public interface abstract L" + ROOT + "Callback;\n")
        self.put(ROOT + "player/profiles/ProfilesApi", ".class public interface abstract L" + ROOT + "player/profiles/ProfilesApi;\n")
        self.cp = self.put(ROOT + "player/profiles/CurrentProfile",
            ".class public abstract L" + ROOT + "player/profiles/CurrentProfile;\n"
            ".super Ljava/lang/Object;\n" + method("constructor <init>(Ljava/lang/String;Ljava/lang/String;Ljava/lang/String;)V") +
            ".method public abstract getLegacyGamerAccessToken()" + RES + "\n.end method\n" +
            ".method public abstract getLegacyGamerProfileId()" + RES + "\n.end method\n")
        self.profiles = []
        for dex in ("smali", "smali_classes3"):
            self.profiles.append(self.put("obfuscated/Profiles" + dex,
                ".class public Lobfuscated/Profiles" + dex + ";\n.super Ljava/lang/Object;\n"
                ".implements L" + ROOT + "player/profiles/ProfilesApi;\n" +
                method("getCurrentProfile()" + RES, "const-string v0, \"Platform not initialized\"\n    return-object v0") +
                method("registerEventHandler(Ljava/lang/Object;)V"), dex))
        self.access = self.put("obfuscated/Access", ".class public Lobfuscated/Access;\n"
            ".super Ljava/lang/Object;\n.implements L" + ROOT + "player/access/AccessApi;\n" +
            method("requestPlayerAccess(" + CB + ")V"))
        self.blobs = self.put("obfuscated/Blobs", ".class public Lobfuscated/Blobs;\n"
            ".super Ljava/lang/Object;\n.implements L" + ROOT + "storage/blobs/BlobStoreApi;\n" +
            method("readPlayerBlob(Ljava/lang/String;" + CB + ")V") +
            method("writePlayerBlob(Ljava/lang/String;Lcom/netflix/games/storage/blobs/BlobContainer;" + CB + ")V") +
            method("getPlayerBlobs(" + CB + ")V") +
            method("deletePlayerBlob(Ljava/lang/String;" + CB + ")V"))

    def put(self, cls, text, dex="smali"):
        p = self.dec / dex / (cls + ".smali")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        return p

    def test_direct_sdk_supplies_profile_before_reentrant_access_callback(self):
        init = self.put(ROOT + "NetflixGames", method("init()V", "# real startup kept\n    return-void"))
        before = init.read_text()
        r = report()
        patch_gen2_nobridge(self.dec, r)
        self.assertEqual(r["patched"].count("grant-offline-profile"), 2)
        for p in self.profiles:
            text = p.read_text()
            self.assertNotIn("Platform not initialized", text)
            self.assertIn("NfxOfflineProfile;-><init>()V", text)
            self.assertIn("registerEventHandler(Ljava/lang/Object;)V", text)
            self.assertIn("return-object v0", text)
        self.assertEqual(init.read_text(), before)
        self.assertIn(OFFLINE_PLAYER_ID, self.access.read_text())
        profile = self.cp.parent.joinpath("NfxOfflineProfile.smali").read_text()
        self.assertIn(OFFLINE_PLAYER_ID, profile)
        self.assertIn("getLegacyGamerProfileId()" + RES, profile)
        self.assertIn("getLegacyGamerAccessToken()" + RES, profile)

    def test_callbacks_are_queued_once_and_not_invoked_inline(self):
        patch_gen2_nobridge(self.dec, report())
        for p, count in ((self.access, 1), (self.blobs, 4)):
            text = p.read_text()
            self.assertEqual(text.count("NfxOfflineResult;->post("), count)
            self.assertNotIn("->onResult(", text)
        helper = self.dec.joinpath("smali", ROOT + "NfxOfflineResult.smali").read_text()
        self.assertIn("Looper;->getMainLooper()", helper)
        self.assertIn("Handler;->post(Ljava/lang/Runnable;)Z", helper)
        self.assertEqual(helper.count("->onResult("), 1)
        self.assertIn("Offline SDK callback rejected", helper)

    def test_blob_callbacks_use_their_own_request_parameter(self):
        patch_gen2_nobridge(self.dec, report())
        text = self.blobs.read_text()
        for name, callback in (("readPlayerBlob", "p2"), ("writePlayerBlob", "p3"),
                               ("getPlayerBlobs", "p1"), ("deletePlayerBlob", "p2")):
            body = text.split(".method public " + name, 1)[1].split(".end method", 1)[0]
            self.assertIn("invoke-static {" + callback + ", ", body)
            self.assertEqual(body.count("NfxOfflineResult;->post("), 1)

    def test_profile_annotations_and_unrelated_overload_survive(self):
        p = self.profiles[0]
        text = p.read_text().replace(".locals 1", ".locals 1\n    .annotation runtime Lexample/Annotation;\n    .end annotation", 1)
        overload = method("getCurrentProfile(I)" + RES, "const/4 v0, 0x0\n    return-object v0")
        p.write_text(text + overload)
        patch_current_profile(self.dec, report())
        self.assertIn(".annotation runtime Lexample/Annotation;", p.read_text())
        self.assertIn(overload, p.read_text())

    def test_second_run_is_idempotent(self):
        patch_gen2_nobridge(self.dec, report())
        before = {p: p.read_bytes() for p in self.dec.rglob("*.smali")}
        r = report()
        patch_gen2_nobridge(self.dec, r)
        self.assertEqual(before, {p: p.read_bytes() for p in self.dec.rglob("*.smali")})
        self.assertEqual(r["already"].count("grant-offline-profile"), 2)

    def test_unity_uses_the_same_profile_patch(self):
        self.assertIs(unity_profile, patch_current_profile)
        r = report()
        unity_profile(self.dec, r)
        self.assertEqual(r["patched"].count("grant-offline-profile"), 2)

    def test_membership_constructor_keeps_identity_consistent(self):
        self.put(ROOT + "player/access/PlayerAccessInfo",
            method("constructor <init>(Ljava/lang/String;Lcom/netflix/games/player/access/MembershipStatus;)V"))
        patch_gen2_nobridge(self.dec, report())
        self.assertIn("->MEMBER:", self.access.read_text())
        self.assertIn(OFFLINE_PLAYER_ID, self.access.read_text())

    def test_unknown_abstract_profile_contract_fails_before_grant(self):
        with self.cp.open("a") as f:
            f.write(".method public abstract futureMethod()V\n.end method\n")
        before = self.access.read_text()
        with self.assertRaisesRegex(SystemExit, "unsupported gen-2 CurrentProfile"):
            patch_gen2_nobridge(self.dec, report())
        self.assertEqual(before, self.access.read_text())
        self.assertFalse(self.cp.with_name("NfxOfflineProfile.smali").exists())

    def test_final_or_unknown_constructor_is_rejected(self):
        original = self.cp.read_text()
        for text in (original.replace("public abstract", "public final", 1),
                     original.replace("<init>(Ljava/lang/String;", "<init>(I", 1)):
            self.cp.write_text(text)
            self.assertIsNone(emit_offline_profile(self.dec))

    def test_advertised_profiles_without_impl_fails(self):
        for p in self.profiles:
            p.unlink()
        with self.assertRaisesRegex(SystemExit, "no implementation"):
            patch_current_profile(self.dec, report())

    def test_sdk_without_profiles_is_supported(self):
        for p in self.profiles:
            p.unlink()
        self.dec.joinpath("smali", ROOT + "player/profiles/ProfilesApi.smali").unlink()
        r = report()
        patch_gen2_nobridge(self.dec, r)
        self.assertIn("grant-offline-profile (no ProfilesApi)", r["skipped"])
        self.assertIn("grant-player-access", r["patched"])

    def test_wrong_profile_return_signature_is_not_rewritten(self):
        self.profiles[0].write_text(self.profiles[0].read_text().replace("getCurrentProfile()" + RES, "getCurrentProfile()Ljava/lang/Object;"))
        with self.assertRaisesRegex(SystemExit, "profile method missing"):
            patch_current_profile(self.dec, report())
