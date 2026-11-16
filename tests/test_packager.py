from __future__ import annotations

import hashlib
import os
import shutil
import stat
import tempfile
import unittest
import zipfile
from pathlib import Path

from ak3_actions.packager import package_anykernel_zip


class TestPackagerComponent(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="ak3_test_pkg_"))
        self.staging = self.tmp / "staging"
        self.staging.mkdir()
        (self.staging / "LICENSE").write_text("GPLv3\n", encoding="utf-8")
        (self.staging / "README.md").write_text("# Ignore me\n", encoding="utf-8")
        (self.staging / ".git").mkdir()
        (self.staging / ".git" / "HEAD").write_text("ref: refs/heads/master\n", encoding="utf-8")
        (self.staging / ".github").mkdir()
        (self.staging / ".github" / "FUNDING.yml").write_text("github: osm0sis\n", encoding="utf-8")
        (self.staging / "anykernel.sh").write_text(". tools/ak3-core.sh;\n", encoding="utf-8")
        (self.staging / "Image.gz-dtb").write_bytes(b"KERNEL")

        meta = self.staging / "META-INF" / "com" / "google" / "android"
        meta.mkdir(parents=True)
        (meta / "update-binary").write_text("#!/sbin/sh\n", encoding="utf-8")
        (meta / "updater-script").write_text("# AK_BASE_VERSION=20261027\n", encoding="utf-8")

        tools = self.staging / "tools"
        tools.mkdir()
        (tools / "ak3-core.sh").write_text("# core\n", encoding="utf-8")
        (tools / "magiskboot").write_bytes(b"\x7fELF")

        # Placeholders that must be excluded
        (self.staging / "patch").mkdir()
        (self.staging / "patch" / "placeholder").write_text("", encoding="utf-8")
        (self.staging / "ramdisk").mkdir()
        (self.staging / "ramdisk" / "placeholder").write_text("", encoding="utf-8")
        (self.staging / "modules" / "vendor" / "lib" / "modules").mkdir(parents=True)
        (self.staging / "modules" / "vendor" / "lib" / "modules" / "wlan.ko").write_bytes(b"KO")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_package_excludes_git_readme_placeholders_and_preserves_exec_bits(self) -> None:
        out_dir = self.tmp / "dist"
        res = package_anykernel_zip(
            self.staging,
            output_dir=out_dir,
            zip_name="TestKernel--{date}.zip",
            write_checksum_file=True,
        )
        self.assertTrue(res.zip_path.is_file())
        self.assertNotIn("--", res.zip_name)
        self.assertIsNotNone(res.checksum_path)
        self.assertTrue(res.checksum_path.is_file())
        actual_sha256 = hashlib.sha256(res.zip_path.read_bytes()).hexdigest()
        self.assertEqual(res.sha256, actual_sha256)
        self.assertEqual(
            res.checksum_path.read_text(encoding="utf-8"),
            f"{actual_sha256}  {res.zip_name}\n",
        )

        with zipfile.ZipFile(res.zip_path, "r") as zf:
            names = set(zf.namelist())
            self.assertIn("LICENSE", names)
            self.assertIn("anykernel.sh", names)
            self.assertIn("Image.gz-dtb", names)
            self.assertIn("modules/vendor/lib/modules/wlan.ko", names)
            self.assertFalse(any(n.startswith(".git") for n in names))
            self.assertNotIn("README.md", names)
            self.assertFalse(any(n.endswith("placeholder") for n in names))

            ub_mode = (zf.getinfo("META-INF/com/google/android/update-binary").external_attr >> 16) & 0o777
            mb_mode = (zf.getinfo("tools/magiskboot").external_attr >> 16) & 0o777
            self.assertEqual(ub_mode, 0o755)
            self.assertEqual(mb_mode, 0o755)


if __name__ == "__main__":
    unittest.main()

