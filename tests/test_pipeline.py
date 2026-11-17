from __future__ import annotations

import gzip
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

from ak3_actions.config import parse_properties_block

REPO_ROOT = Path(__file__).resolve().parent.parent

UPSTREAM_ANYKERNEL_SH = """### AnyKernel3 Ramdisk Mod Script
## osm0sis @ xda-developers

### AnyKernel setup
# global properties
properties() { '
kernel.string=ExampleKernel by osm0sis @ xda-developers
do.devicecheck=1
do.modules=0
do.systemless=1
do.cleanup=1
do.cleanuponabort=0
device.name1=maguro
device.name2=toro
device.name3=toroplus
device.name4=tuna
device.name5=
supported.versions=
supported.patchlevels=
supported.vendorpatchlevels=
'; } # end properties


### AnyKernel install
## boot files attributes
boot_attributes() {
set_perm_recursive 0 0 755 644 $RAMDISK/*;
set_perm_recursive 0 0 750 750 $RAMDISK/init* $RAMDISK/sbin;
} # end attributes

# boot shell variables
BLOCK=/dev/block/platform/omap/omap_hsmmc.0/by-name/boot;
IS_SLOT_DEVICE=0;
RAMDISK_COMPRESSION=auto;
PATCH_VBMETA_FLAG=auto;

# import functions/variables and setup patching - see for reference (DO NOT REMOVE)
. tools/ak3-core.sh;

# boot install
dump_boot;

# init.tuna.rc
backup_file init.tuna.rc;

write_boot;
## end boot install
"""


def _build_mock_upstream(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / ".github").mkdir()
    (root / ".github" / "FUNDING.yml").write_text("github: osm0sis\n", encoding="utf-8")
    (root / "LICENSE").write_text("GPLv3\n", encoding="utf-8")
    (root / "README.md").write_text("# AnyKernel3\n", encoding="utf-8")
    (root / "anykernel.sh").write_text(UPSTREAM_ANYKERNEL_SH, encoding="utf-8")
    meta = root / "META-INF" / "com" / "google" / "android"
    meta.mkdir(parents=True)
    (meta / "update-binary").write_text("#!/sbin/sh\n", encoding="utf-8")
    (meta / "updater-script").write_text("# AK_BASE_VERSION=20261027\n", encoding="utf-8")
    tools = root / "tools"
    tools.mkdir()
    (tools / "ak3-core.sh").write_text("# core\n", encoding="utf-8")
    (tools / "busybox").write_bytes(b"\x7fELF")
    (root / "modules" / "system" / "lib" / "modules").mkdir(parents=True)
    (root / "modules" / "system" / "lib" / "modules" / "placeholder").write_text("", encoding="utf-8")

    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
    }
    subprocess.run(["git", "init", "-b", "master"], cwd=root, check=True, capture_output=True, env=env)
    subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True, env=env)
    subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True, capture_output=True, env=env)
    subprocess.run(["git", "tag", "ak3-v1"], cwd=root, check=True, capture_output=True, env=env)
    return root


class TestEndToEndPipeline(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="ak3_test_pipe_"))
        self.upstream = _build_mock_upstream(self.tmp / "upstream_ak3")

        self.build_out = self.tmp / "kernel_out"
        boot_dir = self.build_out / "arch" / "arm64" / "boot"
        boot_dir.mkdir(parents=True)
        raw = b"Linux version 5.10.210-stellar-gabc1234 (ci@github)\x00"
        (boot_dir / "Image.gz-dtb").write_bytes(gzip.compress(raw) + b"\xd0\x0d\xfe\xedDTB")
        (boot_dir / "Image").write_bytes(raw)
        (boot_dir / "dtbo.img").write_bytes(b"DTBO")

        mods = self.tmp / "mods_out" / "lib" / "modules" / "5.10.210-stellar"
        mods.mkdir(parents=True)
        (mods / "test_drv.ko").write_bytes(b"ELF_KO")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run_cli(self, *args: str, env_extra: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        env = dict(os.environ)
        env["PYTHONPATH"] = str(REPO_ROOT)
        if env_extra:
            env.update(env_extra)
        return subprocess.run(
            [sys.executable, str(REPO_ROOT / "main.py"), *args],
            cwd=str(self.tmp),
            env=env,
            capture_output=True,
            text=True,
        )

    def test_four_step_cli_workflow_matches_action_yaml(self) -> None:
        staging = self.tmp / "staging"
        gh_out = self.tmp / "github_output.txt"
        gh_sum = self.tmp / "github_summary.md"
        cfg_file = self.tmp / "ak3.json"
        cfg_file.write_text(
            json.dumps({
                "kernel.string": "Stellar {kernel_version}",
                "do.devicecheck": 1,
                "device.names": ["alioth", "aliothin"],
                "supported.versions": "13 - 15",
                "block": "/dev/block/bootdevice/by-name/boot",
                "is-slot-device": "1",
            }),
            encoding="utf-8",
        )

        common_env = {
            "GITHUB_ACTIONS": "true",
            "GITHUB_OUTPUT": str(gh_out),
            "GITHUB_STEP_SUMMARY": str(gh_sum),
            "GITHUB_SHA": "abcdef1234567890",
        }

        p1 = self._run_cli(
            "prepare",
            env_extra={
                **common_env,
                "INPUT_ANYKERNEL-REPO": self.upstream.as_uri(),
                "INPUT_ANYKERNEL-REF": "master",
                "INPUT_STAGING-DIR": str(staging),
            },
        )
        self.assertEqual(p1.returncode, 0, p1.stderr)
        self.assertFalse((staging / ".git").exists())

        p2 = self._run_cli(
            "process-kernel",
            env_extra={
                **common_env,
                "INPUT_STAGING-DIR": str(staging),
                "INPUT_KERNEL-DIR": str(self.build_out),
                "INPUT_DTBO-PATH": str(self.build_out / "arch" / "arm64" / "boot" / "dtbo.img"),
                "INPUT_MODULES-PATH": str(self.tmp / "mods_out"),
                "INPUT_MODULES-DEST": "vendor/lib/modules",
                "INPUT_BANNER": "{kernel_string} on {device}",
                "INPUT_VERSION": "{kernel_version}-{short_sha}",
                "INPUT_DEVICE-NAMES": "alioth,aliothin",
                "INPUT_KERNEL-STRING": "Stellar {kernel_version}",
            },
        )
        self.assertEqual(p2.returncode, 0, p2.stderr)

        p3 = self._run_cli(
            "configure",
            env_extra={
                **common_env,
                "INPUT_STAGING-DIR": str(staging),
                "INPUT_CONFIG-FILE": str(cfg_file),
                # Override device-names via env input
                "INPUT_DEVICE-NAMES": "munch,alioth",
                "INPUT_DEEPSLEEP-FIX": "true",
                "INPUT_STRICT": "true",
            },
        )
        self.assertEqual(p3.returncode, 0, p3.stderr)

        p4 = self._run_cli(
            "package",
            env_extra={
                **common_env,
                "INPUT_STAGING-DIR": str(staging),
                "INPUT_OUTPUT-DIR": str(self.tmp / "dist"),
                "INPUT_ZIP-NAME": "Stellar-{device}-{kernel_version}-{short_sha}.zip",
                "INPUT_WRITE-CHECKSUM-FILE": "true",
            },
        )
        self.assertEqual(p4.returncode, 0, p4.stderr)

        expected_zip = self.tmp / "dist" / "Stellar-munch-5.10.210-stellar-gabc1234-abcdef1.zip"
        self.assertTrue(expected_zip.is_file())
        self.assertTrue((self.tmp / "dist" / f"{expected_zip.name}.sha256").is_file())

        with zipfile.ZipFile(expected_zip, "r") as zf:
            names = set(zf.namelist())
            self.assertIn("Image.gz-dtb", names)
            self.assertIn("dtbo.img", names)
            self.assertIn("modules/vendor/lib/modules/test_drv.ko", names)
            self.assertIn("banner", names)
            self.assertIn("version", names)
            self.assertNotIn("README.md", names)
            self.assertFalse(any(".git" in n for n in names))
            self.assertFalse(any(n.endswith("placeholder") for n in names))

            ak_text = zf.read("anykernel.sh").decode("utf-8")
            props = parse_properties_block(ak_text)
            self.assertEqual(props["kernel.string"], "Stellar 5.10.210-stellar-gabc1234")
            self.assertEqual(props["do.modules"], "1")  # auto-enabled when modules staged
            self.assertEqual(props["device.name1"], "munch")
            self.assertEqual(props["device.name2"], "alioth")
            self.assertNotIn("device.name3", props)
            self.assertIn("BLOCK=/dev/block/bootdevice/by-name/boot;", ak_text)
            self.assertIn("IS_SLOT_DEVICE=1;", ak_text)
            self.assertIn("# Deepsleep fix (@Chainfire)", ak_text)
            self.assertNotIn("init.tuna.rc", ak_text)

            version_text = zf.read("version").decode("utf-8")
            self.assertEqual(version_text, "5.10.210-stellar-gabc1234-abcdef1\n")

        out_text = gh_out.read_text(encoding="utf-8")
        self.assertIn(f"zip-path={expected_zip}", out_text)
        self.assertIn("kernel-version=5.10.210-stellar-gabc1234", out_text)
        self.assertIn("### AnyKernel3 Package Summary", gh_sum.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()

