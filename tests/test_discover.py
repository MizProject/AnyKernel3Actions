from __future__ import annotations

import gzip
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from ak3_actions.discover import (
    discover_kernel_artifacts,
    find_kernel_source_dirs,
    is_linux_kernel_source_dir,
)
from ak3_actions.kernel import inspect_kernel_binary, process_compiled_kernel

REPO_ROOT = Path(__file__).resolve().parent.parent


def _make_arm64_image_bytes(version: str = "6.1.99-test-g1234", extra: bytes = b"") -> bytes:
    header = bytearray(128)
    header[0x38:0x3C] = b"ARM\x64"
    body = (
        f"Linux version {version} (builder@ci) #1 SMP PREEMPT\x00"
        "Kernel command line: console=ttyMSM0\x00"
        "Kernel panic - not syncing: Attempted to kill init!\x00"
    ).encode("utf-8")
    return bytes(header) + body + extra


def _lz4_legacy_wrap_uncompressed_literals(payload: bytes) -> bytes:
    """Build a valid LZ4 legacy stream containing uncompressed literal block(s) for testing."""
    # Single LZ4 sequence with token (0xF0 => lit_len>=15, match_len=0 => end of block)
    out = bytearray(b"\x02\x21\x4c\x18")  # LZ4 legacy magic 0x184C2102 LE
    block = bytearray()
    n = len(payload)
    if n < 15:
        block.append(n << 4)
    else:
        block.append(0xF0)
        rem = n - 15
        while rem >= 255:
            block.append(255)
            rem -= 255
        block.append(rem)
    block.extend(payload)
    out.extend(struct.pack("<I", len(block)))
    out.extend(block)
    return bytes(out)


def _make_kernel_source_tree(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "Makefile").write_text(
        "VERSION = 6\nPATCHLEVEL = 1\nSUBLEVEL = 99\nNAME = Hurr durr\n",
        encoding="utf-8",
    )
    (root / "arch" / "arm64" / "boot").mkdir(parents=True, exist_ok=True)
    (root / "kernel").mkdir(parents=True, exist_ok=True)
    return root


class TestKernelInspectionAndDiscovery(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="ak3_test_disc_"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_inspect_accepts_authentic_raw_gzip_lz4_zimage_bzimage(self) -> None:
        raw_img = self.tmp / "Image"
        raw_img.write_bytes(_make_arm64_image_bytes("6.1.99-raw"))
        res_raw = inspect_kernel_binary(raw_img)
        self.assertTrue(res_raw.valid)
        self.assertEqual(res_raw.arch_hint, "arm64")
        self.assertEqual(res_raw.header_magic, "ARM64_IMAGE")
        self.assertEqual(res_raw.confidence, "high")
        self.assertEqual(res_raw.kernel_version, "6.1.99-raw")

        gz_dtb = self.tmp / "Image.gz-dtb"
        gz_dtb.write_bytes(gzip.compress(_make_arm64_image_bytes("6.1.99-gz")) + b"\xd0\x0d\xfe\xedDTB")
        res_gz = inspect_kernel_binary(gz_dtb)
        self.assertTrue(res_gz.valid)
        self.assertEqual(res_gz.compression, "gzip")
        self.assertTrue(res_gz.has_dtb_appended)
        self.assertEqual(res_gz.kernel_version, "6.1.99-gz")

        lz4_dtb = self.tmp / "Image.lz4-dtb"
        lz4_dtb.write_bytes(
            _lz4_legacy_wrap_uncompressed_literals(_make_arm64_image_bytes("6.1.99-lz4"))
            + b"\xd0\x0d\xfe\xedDTB"
        )
        res_lz4 = inspect_kernel_binary(lz4_dtb)
        self.assertTrue(res_lz4.valid, res_lz4.reason)
        self.assertEqual(res_lz4.compression, "lz4")
        self.assertEqual(res_lz4.arch_hint, "arm64")
        self.assertEqual(res_lz4.kernel_version, "6.1.99-lz4")

        zimg = self.tmp / "zImage"
        zstub = bytearray(64)
        zstub[0x24:0x28] = b"\x18\x28\x6f\x01"
        arm32_inner = b"\x00" * 64 + b"Linux version 5.4.250-arm (ci@arm)\x00Booting Linux on physical CPU 0x0\x00"
        zimg.write_bytes(bytes(zstub) + gzip.compress(arm32_inner))
        res_z = inspect_kernel_binary(zimg)
        self.assertTrue(res_z.valid)
        self.assertEqual(res_z.arch_hint, "arm")
        self.assertEqual(res_z.header_magic, "ARM_ZIMAGE")

    def test_inspect_rejects_fakes_text_elf_png_corrupt_and_header_without_strings(self) -> None:
        # 1. Plain text containing Linux version string
        txt = self.tmp / "Image"
        txt.write_text("Linux version 6.1.0-fake (this is plain text, not a kernel)\n", encoding="utf-8")
        self.assertFalse(inspect_kernel_binary(txt).valid)

        # 2. Spoofed ARM64 header magic with no kernel strings
        spoof = bytearray(256)
        spoof[0x38:0x3C] = b"ARM\x64"
        spoof_p = self.tmp / "Image_spoof"
        spoof_p.write_bytes(bytes(spoof))
        res_spoof = inspect_kernel_binary(spoof_p)
        self.assertFalse(res_spoof.valid)
        self.assertIn("no Linux version banner", res_spoof.reason)

        # 3. Userland ELF binary
        elf = self.tmp / "Image_elf"
        elf.write_bytes(b"\x7fELF" + b"\x00" * 128 + b"Linux version 6.1.0\x00")
        self.assertFalse(inspect_kernel_binary(elf).valid)

        # 4. PNG image / random noise / corrupt gz
        png = self.tmp / "Image_png"
        png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 128)
        self.assertFalse(inspect_kernel_binary(png).valid)

        corrupt_gz = self.tmp / "Image.gz"
        corrupt_gz.write_bytes(b"NOT_REAL_GZIP" * 10)
        self.assertFalse(inspect_kernel_binary(corrupt_gz).valid)

    def test_discovery_stage_order_cwd_then_kernel_source_out_then_arch_then_artifacts(self) -> None:
        ws = self.tmp / "ws"
        ws.mkdir()
        ksrc = _make_kernel_source_tree(ws / "android_kernel")
        self.assertTrue(is_linux_kernel_source_dir(ksrc))
        self.assertEqual(find_kernel_source_dirs(ws), [ksrc.resolve()])

        # Case 1: Only artifacts/ has a verified kernel while out/ has a fake non-kernel Image
        fake_boot = ksrc / "out" / "arch" / "arm64" / "boot"
        fake_boot.mkdir(parents=True)
        (fake_boot / "Image").write_text("fake placeholder not a kernel", encoding="utf-8")

        art_dir = ws / "artifacts" / "build-job"
        art_dir.mkdir(parents=True)
        (art_dir / "Image.gz-dtb").write_bytes(gzip.compress(_make_arm64_image_bytes("6.1.99-art")) + b"\xd0\x0d\xfe\xedD")
        (art_dir / "dtbo.img").write_bytes(b"DTBO_ART")
        mods_art = art_dir / "modules" / "vendor" / "lib" / "modules"
        mods_art.mkdir(parents=True)
        (mods_art / "wlan.ko").write_bytes(b"KO")

        res_art = discover_kernel_artifacts(cwd=ws, workspace=ws)
        self.assertEqual(res_art.source_stage, "artifacts:artifacts")
        self.assertEqual(res_art.kernel_path.name, "Image.gz-dtb")
        self.assertEqual(res_art.verification.kernel_version, "6.1.99-art")
        self.assertEqual(res_art.dtbo_path, (art_dir / "dtbo.img").resolve())
        self.assertIsNotNone(res_art.modules_path)
        self.assertTrue(any(p.name == "Image" for p, _ in res_art.rejected_candidates))

        # Case 2: Remove fake and put real in-tree arch/arm64/boot kernel (when out/ is absent)
        shutil.rmtree(ksrc / "out")
        in_tree_img = ksrc / "arch" / "arm64" / "boot" / "Image"
        in_tree_img.write_bytes(_make_arm64_image_bytes("6.1.99-arch"))
        res_arch = discover_kernel_artifacts(cwd=ws, workspace=ws)
        self.assertEqual(res_arch.source_stage, "kernel_source/arch")
        self.assertEqual(res_arch.kernel_path, in_tree_img.resolve())
        self.assertEqual(res_arch.kernel_source_dir, ksrc.resolve())

        # Case 3: out/ appears and takes precedence over in-tree arch/
        out_img = ksrc / "out" / "arch" / "arm64" / "boot" / "Image.gz"
        out_img.parent.mkdir(parents=True)
        out_img.write_bytes(gzip.compress(_make_arm64_image_bytes("6.1.99-out")))
        res_out = discover_kernel_artifacts(cwd=ws, workspace=ws)
        self.assertEqual(res_out.source_stage, "kernel_source/out")
        self.assertEqual(res_out.kernel_path, out_img.resolve())

        # Case 4: cwd wins first when kernel exists right in cwd
        cwd_dir = self.tmp / "run_here"
        cwd_dir.mkdir()
        cwd_img = cwd_dir / "Image.gz-dtb"
        cwd_img.write_bytes(gzip.compress(_make_arm64_image_bytes("6.1.99-cwd")) + b"\xd0\x0d\xfe\xedD")
        res_cwd = discover_kernel_artifacts(cwd=cwd_dir, workspace=ws)
        self.assertEqual(res_cwd.source_stage, "cwd")
        self.assertEqual(res_cwd.kernel_path, cwd_img.resolve())

    def test_discovery_failure_mentions_manual_override(self) -> None:
        ws = self.tmp / "empty_ws"
        ws.mkdir()
        (ws / "Image").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)
        with self.assertRaises(FileNotFoundError) as ctx:
            discover_kernel_artifacts(cwd=ws, workspace=ws)
        msg = str(ctx.exception)
        self.assertIn("[Beta]", msg)
        self.assertIn("manually", msg)
        self.assertIn("Rejected candidate", msg)

    def test_manual_verify_kernel_flag_rejects_spoofed_file(self) -> None:
        staging = self.tmp / "staging"
        staging.mkdir()
        (staging / "anykernel.sh").write_text(". tools/ak3-core.sh;\n", encoding="utf-8")
        bad_img = self.tmp / "Image"
        bad_img.write_text("Not a kernel binary", encoding="utf-8")
        with self.assertRaises(ValueError):
            process_compiled_kernel(staging, kernel_image=bad_img, verify_kernel=True)

    def test_cli_discover_and_autodetect_build(self) -> None:
        ws = self.tmp / "ci_ws"
        ws.mkdir()
        # Local mock AnyKernel3 base so no network needed
        ak_base = self.tmp / "ak3_base"
        ak_base.mkdir()
        (ak_base / "LICENSE").write_text("GPLv3\n", encoding="utf-8")
        (ak_base / "anykernel.sh").write_text(
            "### AK3\nproperties() { '\nkernel.string=Ex\ndo.devicecheck=0\n'; } # end properties\n"
            "BLOCK=boot;\n. tools/ak3-core.sh;\n",
            encoding="utf-8",
        )
        meta = ak_base / "META-INF" / "com" / "google" / "android"
        meta.mkdir(parents=True)
        (meta / "update-binary").write_text("#!/sbin/sh\n", encoding="utf-8")
        (meta / "updater-script").write_text("# AK_BASE_VERSION=20261027\n", encoding="utf-8")
        tools = ak_base / "tools"
        tools.mkdir()
        (tools / "ak3-core.sh").write_text("# core\n", encoding="utf-8")

        art_dir = ws / "downloaded-artifacts"
        art_dir.mkdir()
        (art_dir / "Image.gz-dtb").write_bytes(
            gzip.compress(_make_arm64_image_bytes("6.6.12-ci-auto")) + b"\xd0\x0d\xfe\xedD"
        )
        gh_out = self.tmp / "gh_out.txt"
        env = {
            **os.environ,
            "PYTHONPATH": str(REPO_ROOT),
            "GITHUB_WORKSPACE": str(ws),
            "GITHUB_OUTPUT": str(gh_out),
        }

        proc_disc = subprocess.run(
            [
                sys.executable,
                str(REPO_ROOT / "main.py"),
                "discover",
                "--workspace",
                str(ws),
                "--artifacts-dir",
                str(art_dir),
            ],
            cwd=str(ws),
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc_disc.returncode, 0, proc_disc.stderr)
        out_txt = gh_out.read_text(encoding="utf-8")
        self.assertIn("kernel-version=6.6.12-ci-auto", out_txt)
        self.assertIn("discovered-from=artifacts-dir", out_txt)

        proc_build = subprocess.run(
            [
                sys.executable,
                str(REPO_ROOT / "main.py"),
                "build",
                "--anykernel-path",
                str(ak_base),
                "--autodetect",
                "true",
                "--artifacts-dir",
                str(art_dir),
                "--kernel-string",
                "AutoKernel {kernel_version}",
                "--block",
                "/dev/block/bootdevice/by-name/boot",
                "--is-slot-device",
                "auto",
                "--output-dir",
                str(self.tmp / "out_zip"),
                "--zip-name",
                "Auto-{kernel_version}.zip",
            ],
            cwd=str(ws),
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc_build.returncode, 0, proc_build.stderr)
        self.assertTrue((self.tmp / "out_zip" / "Auto-6.6.12-ci-auto.zip").is_file())


if __name__ == "__main__":
    unittest.main()
