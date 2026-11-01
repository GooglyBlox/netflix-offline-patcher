import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from netflix_patcher.android.apk_validation import validate_merged_payload
from netflix_patcher.android.tools import apktool_build_flags
from netflix_patcher.android.pipeline import run_android


class ApktoolTests(unittest.TestCase):
    def test_version_flags(self):
        for version, expected in (("2.6.1", ["--use-aapt2"]),
                                  ("2.8.2", ["--use-aapt2"]), ("2.9.0", []),
                                  ("2.12.1", []), ("3.0.3", []),
                                  ("3.0.0-SNAPSHOT", [])):
            with self.subTest(version=version), patch("netflix_patcher.android.tools.run", return_value=SimpleNamespace(stdout=version)) as run:
                self.assertEqual(apktool_build_flags("java", "apktool.jar", {"A": "B"}), expected)
                self.assertEqual(run.call_args.args[0], ["java", "-jar", "apktool.jar", "--version"])
                self.assertEqual(run.call_args.kwargs["env"], {"A": "B"})

    def test_unknown_version_is_diagnostic(self):
        with patch("netflix_patcher.android.tools.run", return_value=SimpleNamespace(stdout="unknown")):
            with self.assertRaisesRegex(RuntimeError, "Unrecognized|unrecognized"):
                apktool_build_flags("java", "tool.jar", {})

    def test_tool_failure_is_not_retried_as_a_resource_error(self):
        with patch("netflix_patcher.android.tools.run", side_effect=subprocess.CalledProcessError(1, "java")) as run:
            with self.assertRaises(subprocess.CalledProcessError):
                apktool_build_flags("java", "tool.jar", {})
            self.assertEqual(run.call_count, 1)


class MergeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.base = self.apk("base.apk", {"classes.dex": b"dex", "assets/config": b"config", "resources.arsc": b"table"})
        self.pack = self.apk("split_resourcespack.apk", {"assets/resources/music.bank": b"bank", "assets/resources/resource.dat": b"data"})
        self.abi = self.apk("split_config.arm64_v8a.apk", {"lib/arm64-v8a/libgame.so": b"ELF"})
        self.entries = {"assets/config": b"config", "assets/resources/music.bank": b"bank", "assets/resources/resource.dat": b"data", "lib/arm64-v8a/libgame.so": b"ELF", "resources.arsc": b"merged table"}

    def apk(self, name, entries, compression=zipfile.ZIP_STORED):
        p = self.root / name
        with zipfile.ZipFile(p, "w", compression=compression) as z:
            for key, value in entries.items():
                z.writestr(key, value)
        return p

    def test_all_asset_packs_and_abi_libraries_survive(self):
        output = self.apk("merged.apk", self.entries)
        validate_merged_payload([self.base, self.pack, self.abi], output)

    def test_missing_changed_or_compressed_asset_and_library_are_detected(self):
        for key in ("assets/resources/music.bank", "lib/arm64-v8a/libgame.so", "resources.arsc"):
            for action in ("missing", "changed") if key != "resources.arsc" else ("missing",):
                with self.subTest(key=key, action=action):
                    entries = dict(self.entries)
                    if action == "missing":
                        del entries[key]
                    else:
                        entries[key] = b"oops"
                    output = self.apk("merged.apk", entries)
                    with self.assertRaisesRegex(RuntimeError, "Split merge lost or changed payload"):
                        validate_merged_payload([self.base, self.pack, self.abi], output)
        output = self.apk("compressed.apk", self.entries, zipfile.ZIP_DEFLATED)
        with self.assertRaisesRegex(RuntimeError, "music.bank"):
            validate_merged_payload([self.base, self.pack, self.abi], output)

    def test_decompression_by_merger_is_safe(self):
        pack = self.apk("compressed-pack.apk", {"assets/config": b"config"}, zipfile.ZIP_DEFLATED)
        validate_merged_payload([pack], self.apk("out.apk", {"assets/config": b"config"}))

    def test_identical_duplicates_allow_different_compression_but_keep_storage(self):
        compressed = self.apk("compressed.apk", {"assets/config": b"config"}, zipfile.ZIP_DEFLATED)
        output = self.apk("out.apk", self.entries)
        for inputs in ([self.base, compressed], [compressed, self.base]):
            validate_merged_payload(inputs, output)
            with self.assertRaisesRegex(RuntimeError, "assets/config"):
                validate_merged_payload(inputs, self.apk("bad.apk", self.entries, zipfile.ZIP_DEFLATED))

    def test_conflicting_payload_does_not_silently_overwrite(self):
        conflict = self.apk("conflict.apk", {"assets/config": b"different"})
        with self.assertRaisesRegex(RuntimeError, "Conflicting split payload"):
            validate_merged_payload([self.base, conflict], self.apk("out.apk", self.entries))

    def test_unsigned_split_still_requires_apkeditor(self):
        args = SimpleNamespace(no_sign=True, keep_work=False)
        tools = dict(java="java", apktool="tool.jar", apkeditor=None)
        with patch("netflix_patcher.android.pipeline.resolve_tools", return_value=tools), patch("netflix_patcher.android.pipeline.unpack_input", return_value=(self.base, [self.pack])):
            with self.assertRaisesRegex(SystemExit, "APKEditor is required"):
                run_android(self.base, self.root / "out.apk", args)
