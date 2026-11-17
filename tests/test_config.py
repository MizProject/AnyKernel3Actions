from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from ak3_actions.config import (
    AnyKernelConfig,
    PartitionBlock,
    config_from_mapping,
    configure_anykernel_script,
    parse_properties_block,
)

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
dump_boot; # use split_boot to skip ramdisk unpack, e.g. for devices with init_boot ramdisk

# init.rc
backup_file init.rc;
replace_string init.rc "cpuctl cpu,timer_slack" "mount cgroup none /dev/cpuctl cpu" "mount cgroup none /dev/cpuctl cpu,timer_slack";

# init.tuna.rc
backup_file init.tuna.rc;
insert_line init.tuna.rc "nodiratime barrier=0" after "mount_all /fstab.tuna" "\\tmount ext4 /dev/block/platform/omap/omap_hsmmc.0/by-name/userdata /data remount nosuid nodev noatime nodiratime barrier=0";
append_file init.tuna.rc "bootscript" init.tuna;

# fstab.tuna
backup_file fstab.tuna;
patch_fstab fstab.tuna /system ext4 options "noatime,barrier=1" "noatime,nodiratime,barrier=0";
patch_fstab fstab.tuna /cache ext4 options "barrier=1" "barrier=0,nomblk_io_submit";
patch_fstab fstab.tuna /data ext4 options "data=ordered" "nomblk_io_submit,data=writeback";
append_file fstab.tuna "usbdisk" fstab;

write_boot; # use flash_boot to skip ramdisk repack, e.g. for devices with init_boot ramdisk
## end boot install


## init_boot files attributes
#init_boot_attributes() {
#set_perm_recursive 0 0 755 644 $RAMDISK/*;
#set_perm_recursive 0 0 750 750 $RAMDISK/init* $RAMDISK/sbin;
#} # end attributes

# init_boot shell variables
#BLOCK=init_boot;
#IS_SLOT_DEVICE=1;
#RAMDISK_COMPRESSION=auto;
#PATCH_VBMETA_FLAG=auto;
"""


class TestConfigComponent(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="ak3_test_config_"))
        (self.tmp / "anykernel.sh").write_text(UPSTREAM_ANYKERNEL_SH, encoding="utf-8")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_configure_properties_devices_and_shell_vars(self) -> None:
        cfg = AnyKernelConfig(
            kernel_string="AwesomeKernel 5.10",
            do_devicecheck="1",
            do_modules="1",
            do_systemless="0",
            device_names=["sweet", "sweetin"],
            supported_versions="13 - 15",
            supported_patchlevels="2024-01 -",
            extra_properties={"custom.maintainer": "dev"},
            block="/dev/block/bootdevice/by-name/boot",
            is_slot_device="1",
            ramdisk_compression="lz4",
            patch_vbmeta_flag="0",
            no_block_display=True,
            no_magisk_check=True,
            custom_dd="bs=4096",
            slot_select="active",
        )
        props = configure_anykernel_script(self.tmp, cfg)
        self.assertEqual(props["kernel.string"], "AwesomeKernel 5.10")
        self.assertEqual(props["device.name1"], "sweet")
        self.assertEqual(props["device.name2"], "sweetin")
        self.assertNotIn("device.name3", props)
        self.assertNotIn("device.name4", props)
        self.assertNotIn("device.name5", props)
        self.assertEqual(props["custom.maintainer"], "dev")

        rendered = (self.tmp / "anykernel.sh").read_text(encoding="utf-8")
        self.assertIn("BLOCK=/dev/block/bootdevice/by-name/boot;", rendered)
        self.assertIn("IS_SLOT_DEVICE=1;", rendered)
        self.assertIn("RAMDISK_COMPRESSION=lz4;", rendered)
        self.assertIn("PATCH_VBMETA_FLAG=0;", rendered)
        self.assertIn("NO_BLOCK_DISPLAY=1;", rendered)
        self.assertIn("NO_MAGISK_CHECK=1;", rendered)
        self.assertIn('CUSTOMDD="bs=4096";', rendered)
        self.assertIn("SLOT_SELECT=active;", rendered)
        self.assertNotIn("init.tuna.rc", rendered)
        self.assertNotIn("fstab.tuna", rendered)
        self.assertIn("dump_boot;\n\nwrite_boot;", rendered)

    def test_split_flash_adds_flash_generic_for_staged_dtbo_and_dlkm(self) -> None:
        (self.tmp / "dtbo.img").write_bytes(b"DTBO")
        (self.tmp / "vendor_dlkm.img").write_bytes(b"VDLKM")
        cfg = AnyKernelConfig(
            kernel_string="GKI Kernel",
            do_devicecheck="0",
            block="/dev/block/bootdevice/by-name/boot",
            is_slot_device="1",
            install_mode="split_flash",
        )
        props = configure_anykernel_script(self.tmp, cfg)
        self.assertNotIn("device.name1", props)
        rendered = (self.tmp / "anykernel.sh").read_text(encoding="utf-8")
        self.assertIn("split_boot;", rendered)
        self.assertIn("flash_boot;", rendered)
        self.assertIn("flash_generic vendor_dlkm;", rendered)
        self.assertIn("flash_generic dtbo;", rendered)

    def test_extra_partitions_keep_primary_block_isolated(self) -> None:
        cfg = config_from_mapping({
            "kernel-string": "MultiPart",
            "do-devicecheck": "0",
            "block": "/dev/block/bootdevice/by-name/boot",
            "is-slot-device": "1",
            "install-mode": "split_flash",
            "extra-partitions": [
                {
                    "name": "vendor_kernel_boot",
                    "block": "/dev/block/by-name/vendor_kernel_boot",
                    "is-slot-device": "1",
                    "install-mode": "split_flash",
                },
                {
                    "name": "init_boot",
                    "block": "/dev/block/by-name/init_boot",
                    "is-slot-device": "1",
                    "install-mode": "dump_write",
                    "commands": "ui_print \"Patched init_boot\";",
                },
            ],
        })
        configure_anykernel_script(self.tmp, cfg)
        rendered = (self.tmp / "anykernel.sh").read_text(encoding="utf-8")
        self.assertIn("BLOCK=/dev/block/bootdevice/by-name/boot;", rendered)
        self.assertIn("BLOCK=/dev/block/by-name/vendor_kernel_boot;", rendered)
        self.assertIn("BLOCK=/dev/block/by-name/init_boot;", rendered)
        self.assertIn("reset_ak;", rendered)
        self.assertIn('ui_print "Patched init_boot";', rendered)
        self.assertNotIn("#init_boot_attributes()", rendered)

    def test_single_quote_in_property_raises(self) -> None:
        cfg = AnyKernelConfig(
            kernel_string="Bob's Kernel",
            do_devicecheck="0",
            block="/dev/block/bootdevice/by-name/boot",
        )
        with self.assertRaises(ValueError):
            configure_anykernel_script(self.tmp, cfg)

    def test_upstream_placeholder_block_always_requires_specification(self) -> None:
        cfg = AnyKernelConfig(kernel_string="Test", do_devicecheck="0")
        with self.assertRaises(ValueError) as ctx:
            configure_anykernel_script(self.tmp, cfg)
        self.assertIn("omap_hsmmc.0", str(ctx.exception))

    def test_strict_mode_rejects_short_block_and_upstream_defaults(self) -> None:
        cfg_upstream = AnyKernelConfig(kernel_string="Test", strict=True)
        with self.assertRaises(ValueError):
            configure_anykernel_script(self.tmp, cfg_upstream)

        cfg_short = AnyKernelConfig(
            kernel_string="Test",
            do_devicecheck="0",
            block="boot",
            is_slot_device="1",
            strict=True,
        )
        with self.assertRaises(ValueError) as ctx:
            configure_anykernel_script(self.tmp, cfg_short)
        self.assertIn("short partition name", str(ctx.exception))

    def test_slot_suffix_with_is_slot_device_raises(self) -> None:
        for slot_mode in ("1", "auto"):
            with self.subTest(slot_mode=slot_mode):
                cfg = AnyKernelConfig(
                    kernel_string="Test",
                    do_devicecheck="0",
                    block="/dev/block/bootdevice/by-name/boot_a",
                    is_slot_device=slot_mode,
                )
                with self.assertRaises(ValueError) as ctx:
                    configure_anykernel_script(self.tmp, cfg)
                self.assertIn("boot_a/boot_b", str(ctx.exception))

    def test_is_slot_device_auto_accepted(self) -> None:
        cfg = AnyKernelConfig(
            kernel_string="Test",
            do_devicecheck="0",
            block="/dev/block/bootdevice/by-name/boot",
            is_slot_device="auto",
        )
        configure_anykernel_script(self.tmp, cfg)
        rendered = (self.tmp / "anykernel.sh").read_text(encoding="utf-8")
        self.assertIn("BLOCK=/dev/block/bootdevice/by-name/boot;", rendered)
        self.assertIn("IS_SLOT_DEVICE=auto;", rendered)

    def test_invalid_patchlevel_raises(self) -> None:
        cfg = AnyKernelConfig(
            kernel_string="Test",
            do_devicecheck="0",
            block="/dev/block/bootdevice/by-name/boot",
            supported_patchlevels="not-a-date",
        )
        with self.assertRaises(ValueError):
            configure_anykernel_script(self.tmp, cfg)

    def test_no_block_display_false_removes_var(self) -> None:
        cfg1 = AnyKernelConfig(
            kernel_string="Test",
            do_devicecheck="0",
            block="/dev/block/bootdevice/by-name/boot",
            no_block_display=True,
        )
        configure_anykernel_script(self.tmp, cfg1)
        self.assertIn("NO_BLOCK_DISPLAY=1;", (self.tmp / "anykernel.sh").read_text(encoding="utf-8"))

        cfg2 = AnyKernelConfig(
            kernel_string="Test",
            do_devicecheck="0",
            block="/dev/block/bootdevice/by-name/boot",
            no_block_display=False,
        )
        configure_anykernel_script(self.tmp, cfg2)
        self.assertNotIn("NO_BLOCK_DISPLAY", (self.tmp / "anykernel.sh").read_text(encoding="utf-8"))

    def test_parse_properties_block_extracts_upstream(self) -> None:
        props = parse_properties_block(UPSTREAM_ANYKERNEL_SH)
        self.assertEqual(props["kernel.string"], "ExampleKernel by osm0sis @ xda-developers")
        self.assertEqual(props["device.name4"], "tuna")

    def test_deepsleep_fix_injection_and_idempotency(self) -> None:
        cfg = config_from_mapping({
            "kernel-string": "SleepFixKernel",
            "do-devicecheck": "0",
            "block": "/dev/block/bootdevice/by-name/boot",
            "is-slot-device": "0",
            "ramdisk-patch-commands": 'ui_print "- Applying ramdisk tweaks";',
            "deepsleep-fix": True,
        })
        configure_anykernel_script(self.tmp, cfg)
        rendered = (self.tmp / "anykernel.sh").read_text(encoding="utf-8")
        self.assertIn("# Deepsleep fix (@Chainfire)", rendered)
        self.assertIn("echo 'temporary none' > /sys/class/scsi_disk/$i/cache_type", rendered)
        self.assertLess(rendered.index("dump_boot;"), rendered.index("# Deepsleep fix (@Chainfire)"))
        self.assertLess(
            rendered.index("# Deepsleep fix (@Chainfire)"),
            rendered.index('ui_print "- Applying ramdisk tweaks";'),
        )
        self.assertLess(
            rendered.index('ui_print "- Applying ramdisk tweaks";'),
            rendered.index("write_boot;"),
        )

        # Re-running in keep mode must not duplicate the deepsleep snippet
        cfg_keep = AnyKernelConfig(
            kernel_string="SleepFixKernel",
            do_devicecheck="0",
            block="/dev/block/bootdevice/by-name/boot",
            is_slot_device="0",
            install_mode="keep",
            strip_example_patches=False,
            deepsleep_fix=True,
        )
        configure_anykernel_script(self.tmp, cfg_keep)
        rendered2 = (self.tmp / "anykernel.sh").read_text(encoding="utf-8")
        self.assertEqual(rendered2.count("# Deepsleep fix (@Chainfire)"), 1)

        # Custom script without `# boot install ... ## end boot install` anchors still places it right after dump_boot;
        custom_ak = self.tmp / "custom_anykernel.sh"
        custom_ak.write_text(
            "### AnyKernel3\n"
            "properties() { '\nkernel.string=Custom\ndo.devicecheck=0\n'; } # end properties\n"
            "BLOCK=/dev/block/by-name/boot;\nIS_SLOT_DEVICE=0;\n"
            ". tools/ak3-core.sh;\n"
            "## AnyKernel install\n"
            "dump_boot;\n\n"
            "ui_print \"- Other tweaks\";\n\n"
            "write_boot;\n"
            "## end install\n",
            encoding="utf-8",
        )
        configure_anykernel_script(
            self.tmp,
            AnyKernelConfig(
                custom_script_path=custom_ak,
                install_mode="keep",
                strip_example_patches=False,
                deepsleep_fix=True,
            ),
        )
        rendered3 = (self.tmp / "anykernel.sh").read_text(encoding="utf-8")
        self.assertLess(rendered3.index("dump_boot;"), rendered3.index("# Deepsleep fix (@Chainfire)"))
        self.assertLess(rendered3.index("# Deepsleep fix (@Chainfire)"), rendered3.index('ui_print "- Other tweaks";'))
        self.assertLess(rendered3.index('ui_print "- Other tweaks";'), rendered3.index("write_boot;"))


if __name__ == "__main__":
    unittest.main()

