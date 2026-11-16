from __future__ import annotations

import gzip
import shutil
import tempfile
import unittest
from pathlib import Path

from ak3_actions.kernel import detect_kernel_version, process_compiled_kernel, resolve_kernel_image


def _make_staging(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "anykernel.sh").write_text(". tools/ak3-core.sh;\n", encoding="utf-8")
    (root / "modules" / "system" / "lib" / "modules").mkdir(parents=True, exist_ok=True)
    (root / "modules" / "system" / "lib" / "modules" / "placeholder").write_text("", encoding="utf-8")
    (root / "ramdisk").mkdir(parents=True, exist_ok=True)
    (root / "ramdisk" / "placeholder").write_text("", encoding="utf-8")
    (root / "patch").mkdir(parents=True, exist_ok=True)
    (root / "patch" / "placeholder").write_text("", encoding="utf-8")
    return root


class TestKernelComponent(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="ak3_test_kernel_"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_kernel_discovery_priority_prefers_compressed_dtb(self) -> None:
        boot_dir = self.tmp / "out" / "arch" / "arm64" / "boot"
        boot_dir.mkdir(parents=True)
        (boot_dir / "Image").write_bytes(b"RAWIMAGE")
        (boot_dir / "Image.gz").write_bytes(b"GZIMAGE")
        (boot_dir / "Image.gz-dtb").write_bytes(b"GZDTBIMAGE")

        src, dest_name = resolve_kernel_image(kernel_dir=self.tmp / "out")
        self.assertEqual(dest_name, "Image.gz-dtb")
        self.assertEqual(src, (boot_dir / "Image.gz-dtb").resolve())

    def test_detect_kernel_version_from_gzip_and_zimage_payload(self) -> None:
        raw_kernel = b"\x00" * 64 + b"Linux version 5.10.198-perf-g123abc (builder@ci) #1 SMP PREEMPT\x00"
        gz_payload = gzip.compress(raw_kernel)

        img_gz_dtb = self.tmp / "Image.gz-dtb"
        img_gz_dtb.write_bytes(gz_payload + b"\xd0\x0d\xfe\xedDTBSTUB")
        ver, banner = detect_kernel_version(img_gz_dtb)
        self.assertEqual(ver, "5.10.198-perf-g123abc")
        self.assertIn("Linux version 5.10.198-perf-g123abc", banner)

        zimage = self.tmp / "zImage"
        zimage.write_bytes(b"ARM_STUB_DECOMPRESSOR" + gz_payload)
        ver2, _ = detect_kernel_version(zimage)
        self.assertEqual(ver2, "5.10.198-perf-g123abc")

    def test_detect_kernel_version_from_kernel_release(self) -> None:
        out_dir = self.tmp / "out"
        rel = out_dir / "include" / "config" / "kernel.release"
        rel.parent.mkdir(parents=True)
        rel.write_text("6.1.75-android14-11-gdeadbeef\n", encoding="utf-8")
        boot = out_dir / "arch" / "arm64" / "boot"
        boot.mkdir(parents=True)
        img = boot / "Image"
        img.write_bytes(b"NO_BANNER_BYTES")

        ver, _ = detect_kernel_version(img, out_dir)
        self.assertEqual(ver, "6.1.75-android14-11-gdeadbeef")

    def test_process_dtb_concat_modules_overlays_and_templates(self) -> None:
        staging = _make_staging(self.tmp / "staging")
        (staging / "Image").write_bytes(b"STALE_IMAGE")

        out_dir = self.tmp / "build"
        boot_dir = out_dir / "arch" / "arm64" / "boot"
        boot_dir.mkdir(parents=True)
        raw_kernel = b"Linux version 5.15.140-custom (ci@runner)\x00"
        (boot_dir / "Image.gz").write_bytes(gzip.compress(raw_kernel))

        dts_dir = boot_dir / "dts" / "vendor"
        dts_dir.mkdir(parents=True)
        (dts_dir / "a.dtb").write_bytes(b"\xd0\x0d\xfe\xedAAA")
        (dts_dir / "b.dtb").write_bytes(b"\xd0\x0d\xfe\xedBBB")
        (boot_dir / "dtbo.img").write_bytes(b"DTBOIMG")

        mod_dir = self.tmp / "mods" / "lib" / "modules" / "5.15.140-custom" / "kernel" / "drivers"
        mod_dir.mkdir(parents=True)
        (mod_dir / "wlan.ko").write_bytes(b"KO1")
        (mod_dir.parent.parent / "modules.load").write_text("wlan.ko\n", encoding="utf-8")

        ramdisk_src = self.tmp / "ramdisk_in"
        (ramdisk_src / "overlay.d").mkdir(parents=True)
        (ramdisk_src / "overlay.d" / "init.custom.rc").write_text("on boot\n", encoding="utf-8")

        res = process_compiled_kernel(
            staging,
            kernel_dir=out_dir,
            dtb_path=dts_dir,
            dtbo_path=boot_dir / "dtbo.img",
            modules_path=self.tmp / "mods",
            modules_dest="vendor/lib/modules",
            include_module_metadata=True,
            ramdisk_dir=ramdisk_src,
            banner="MyKernel {kernel_version}",
            version="{kernel_version}-{kernel_image}",
        )

        self.assertEqual(res.kernel_image, "Image.gz")
        self.assertEqual(res.kernel_version, "5.15.140-custom")
        self.assertFalse((staging / "Image").exists())
        self.assertTrue((staging / "Image.gz").is_file())
        self.assertEqual((staging / "dtb").read_bytes(), b"\xd0\x0d\xfe\xedAAA\xd0\x0d\xfe\xedBBB")
        self.assertEqual((staging / "dtbo.img").read_bytes(), b"DTBOIMG")
        self.assertEqual(res.modules_count, 1)
        self.assertTrue((staging / "modules" / "vendor" / "lib" / "modules" / "wlan.ko").is_file())
        self.assertTrue((staging / "modules" / "vendor" / "lib" / "modules" / "modules.load").is_file())
        self.assertFalse((staging / "modules" / "system" / "lib" / "modules" / "placeholder").exists())
        self.assertFalse((staging / "ramdisk" / "placeholder").exists())
        self.assertTrue((staging / "ramdisk" / "overlay.d" / "init.custom.rc").is_file())
        self.assertEqual((staging / "banner").read_text(encoding="utf-8"), "MyKernel 5.15.140-custom\n")
        self.assertEqual((staging / "version").read_text(encoding="utf-8"), "5.15.140-custom-Image.gz\n")

    def test_raw_dtbo_directory_without_image_errors_clearly(self) -> None:
        staging = _make_staging(self.tmp / "staging")
        dtbo_dir = self.tmp / "dtbo_raw"
        dtbo_dir.mkdir()
        (dtbo_dir / "board.dtbo").write_bytes(b"RAWDTBO")
        with self.assertRaises(ValueError):
            process_compiled_kernel(staging, dtbo_path=dtbo_dir)


if __name__ == "__main__":
    unittest.main()

