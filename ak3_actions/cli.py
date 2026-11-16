"""Command-line interface for modular AnyKernel3 GitHub Action components."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any, Sequence

from .discover import discover_kernel_artifacts
from .github_env import (
    get_env_input,
    get_env_raw,
    is_truthy,
    set_github_output,
)
from .packager import package_anykernel_zip
from .pipeline import (
    overrides_from_dict,
    resolve_configured_script,
    run_full_pipeline,
    stage_kernel_with_optional_autodetect,
)
from .repo import DEFAULT_ANYKERNEL_REF, DEFAULT_ANYKERNEL_REPO, prepare_anykernel_base


def _opt_path(val: str | None) -> Path | None:
    if val is None or val.strip() == "":
        return None
    return Path(val.strip())


def _pick_str(cli_val: str | None, env_name: str, default: str | None = None) -> str | None:
    if cli_val is not None and cli_val != "":
        return cli_val
    return get_env_input(env_name, default)


def _pick_raw(cli_val: str | None, env_name: str) -> str | None:
    if cli_val is not None and cli_val != "":
        return cli_val
    return get_env_raw(env_name)


def _pick_bool(cli_val: str | None, env_name: str, default: bool) -> bool:
    raw = _pick_str(cli_val, env_name)
    return is_truthy(raw, default=default)


def _add_repo_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--anykernel-repo", default=None, help="AnyKernel3 git repository URL")
    parser.add_argument("--anykernel-ref", default=None, help="AnyKernel3 branch, tag, or commit SHA")
    parser.add_argument("--anykernel-path", default=None, help="Local AnyKernel3 directory path")
    parser.add_argument("--staging-dir", default=None, help="Staging working directory")
    parser.add_argument("--clean-staging", default=None, help="Clean staging directory before preparing (true/false)")
    parser.add_argument("--strip-readme", default=None, help="Remove upstream README.md from staging (true/false)")


def _add_kernel_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--kernel-image", default=None, help="Path or glob to compiled kernel image")
    parser.add_argument("--kernel-image-name", default=None, help="Target filename in AnyKernel3 root")
    parser.add_argument("--kernel-dir", default=None, help="Kernel build output directory")
    parser.add_argument("--autodetect", default=None, help="[Beta] Scour workspace/out/arch/artifacts when kernel-image and kernel-dir are omitted")
    parser.add_argument("--artifacts-dir", default=None, help="[Beta] Optional downloaded artifacts directory for scour")
    parser.add_argument("--search-arch", default=None, help="[Beta] Target kernel architecture hint (arm64, arm, x86, riscv)")
    parser.add_argument("--autodetect-extras", default=None, help="[Beta] Also stage companion dtb/dtbo/modules found during scour")
    parser.add_argument("--verify-kernel", default=None, help="Verify kernel binary magic and strings even for manual paths")
    parser.add_argument("--dtb-path", default=None, help="Path or glob for dtb / dtb.img / *.dtb")
    parser.add_argument("--dt-path", default=None, help="Path to dt / dt.img")
    parser.add_argument("--dtbo-path", default=None, help="Path to dtbo / dtbo.img")
    parser.add_argument("--recovery-dtbo-path", default=None, help="Path to recovery_dtbo / recovery_dtbo.img")
    parser.add_argument("--system-dlkm-path", default=None, help="Path to system_dlkm / system_dlkm.img")
    parser.add_argument("--vendor-dlkm-path", default=None, help="Path to vendor_dlkm / vendor_dlkm.img")
    parser.add_argument("--modules-path", default=None, help="Path or glob for compiled .ko kernel modules")
    parser.add_argument("--modules-dest", default=None, help="Relative module target inside modules/")
    parser.add_argument("--preserve-module-tree", default=None, help="Preserve subdirectory layout for modules")
    parser.add_argument("--include-module-metadata", default=None, help="Copy modules.load/dep/alias metadata")
    parser.add_argument("--strip-modules", default=None, help="Strip debug symbols from .ko files")
    parser.add_argument("--ramdisk-dir", default=None, help="Directory to copy into ramdisk/")
    parser.add_argument("--vendor-ramdisk-dir", default=None, help="Directory to copy into vendor_ramdisk/")
    parser.add_argument("--patch-dir", default=None, help="Directory to copy into patch/")
    parser.add_argument("--vendor-patch-dir", default=None, help="Directory to copy into vendor_patch/")
    parser.add_argument("--banner", default=None, help="Banner text or file path")
    parser.add_argument("--banner-file", default=None, help="Banner file path")
    parser.add_argument("--version", default=None, help="Version text or file path")
    parser.add_argument("--version-file", default=None, help="Version file path")
    parser.add_argument("--require-kernel", default=None, help="Fail if no kernel image is found")


def _extract_kernel_kwargs(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "kernel_image": _pick_str(args.kernel_image, "kernel-image"),
        "kernel_image_name": _pick_str(args.kernel_image_name, "kernel-image-name"),
        "kernel_dir": _pick_str(args.kernel_dir, "kernel-dir"),
        "autodetect": _pick_bool(args.autodetect, "autodetect", False),
        "artifacts_dir": _opt_path(_pick_str(args.artifacts_dir, "artifacts-dir")),
        "search_arch": _pick_str(args.search_arch, "search-arch"),
        "autodetect_extras": _pick_bool(args.autodetect_extras, "autodetect-extras", True),
        "verify_kernel": _pick_bool(args.verify_kernel, "verify-kernel", False),
        "dtb_path": _pick_str(args.dtb_path, "dtb-path"),
        "dt_path": _pick_str(args.dt_path, "dt-path"),
        "dtbo_path": _pick_str(args.dtbo_path, "dtbo-path"),
        "recovery_dtbo_path": _pick_str(args.recovery_dtbo_path, "recovery-dtbo-path"),
        "system_dlkm_path": _pick_str(args.system_dlkm_path, "system-dlkm-path"),
        "vendor_dlkm_path": _pick_str(args.vendor_dlkm_path, "vendor-dlkm-path"),
        "modules_path": _pick_str(args.modules_path, "modules-path"),
        "modules_dest": _pick_str(args.modules_dest, "modules-dest", "system/lib/modules") or "system/lib/modules",
        "preserve_module_tree": _pick_bool(args.preserve_module_tree, "preserve-module-tree", False),
        "include_module_metadata": _pick_bool(args.include_module_metadata, "include-module-metadata", False),
        "strip_modules": _pick_bool(args.strip_modules, "strip-modules", False),
        "ramdisk_dir": _opt_path(_pick_str(args.ramdisk_dir, "ramdisk-dir")),
        "vendor_ramdisk_dir": _opt_path(_pick_str(args.vendor_ramdisk_dir, "vendor-ramdisk-dir")),
        "patch_dir": _opt_path(_pick_str(args.patch_dir, "patch-dir")),
        "vendor_patch_dir": _opt_path(_pick_str(args.vendor_patch_dir, "vendor-patch-dir")),
        "banner": _pick_raw(args.banner, "banner"),
        "banner_file": _opt_path(_pick_str(args.banner_file, "banner-file")),
        "version": _pick_raw(args.version, "version"),
        "version_file": _opt_path(_pick_str(args.version_file, "version-file")),
        "require_kernel": _pick_bool(args.require_kernel, "require-kernel", False),
    }


def _add_config_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config-file", default=None, help="Path to JSON/TOML/properties config file")
    parser.add_argument("--anykernel-script", default=None, help="Custom anykernel.sh path to use as base")
    parser.add_argument("--kernel-string", default=None, help="kernel.string property")
    parser.add_argument("--do-devicecheck", default=None, help="do.devicecheck property (0/1 or true/false)")
    parser.add_argument("--do-modules", default=None, help="do.modules property (0/1 or true/false)")
    parser.add_argument("--do-systemless", default=None, help="do.systemless property (0/1 or true/false)")
    parser.add_argument("--do-cleanup", default=None, help="do.cleanup property (0/1 or true/false)")
    parser.add_argument("--do-cleanuponabort", default=None, help="do.cleanuponabort property (0/1 or true/false)")
    parser.add_argument("--device-names", default=None, help="Comma/whitespace separated device.name# values")
    parser.add_argument("--supported-versions", default=None, help="supported.versions property")
    parser.add_argument("--supported-patchlevels", default=None, help="supported.patchlevels property")
    parser.add_argument("--supported-vendorpatchlevels", default=None, help="supported.vendorpatchlevels property")
    parser.add_argument("--extra-properties", default=None, help="Extra key=value lines for properties()")
    parser.add_argument("--block", default=None, help="BLOCK shell variable (e.g. boot, auto, /dev/block/...)")
    parser.add_argument("--is-slot-device", default=None, help="IS_SLOT_DEVICE (0, 1, auto)")
    parser.add_argument("--ramdisk-compression", default=None, help="RAMDISK_COMPRESSION (auto, gz, lz4, ...)")
    parser.add_argument("--patch-vbmeta-flag", default=None, help="PATCH_VBMETA_FLAG (auto, 0, 1)")
    parser.add_argument("--custom-dd", default=None, help="CUSTOMDD shell variable")
    parser.add_argument("--slot-select", default=None, help="SLOT_SELECT (active, inactive)")
    parser.add_argument("--no-block-display", default=None, help="Set NO_BLOCK_DISPLAY=1")
    parser.add_argument("--no-magisk-check", default=None, help="Set NO_MAGISK_CHECK=1")
    parser.add_argument("--no-vbmeta-partition-patch", default=None, help="Set NO_VBMETA_PARTITION_PATCH=1")
    parser.add_argument("--install-mode", default=None, help="dump_write, split_flash, or keep")
    parser.add_argument("--strip-example-patches", default=None, help="Remove upstream tuna sample patches")
    parser.add_argument("--boot-attributes", default=None, help="Override body of boot_attributes()")
    parser.add_argument("--ramdisk-patch-commands", default=None, help="Shell commands inserted into boot install")
    parser.add_argument("--flash-generic", default=None, help="Extra partitions for flash_generic in split_flash mode")
    parser.add_argument("--extra-partitions", default=None, help="JSON array of extra partition blocks")
    parser.add_argument("--strict", default=None, help="Fail validation on upstream default warnings")


def _add_package_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--create-zip", default=None, help="Whether to build the flashable zip (true/false)")
    parser.add_argument("--zip-name", default=None, help="Output zip filename (supports {device}, {kernel_version}, {date})")
    parser.add_argument("--output-dir", default=None, help="Directory to write output zip")
    parser.add_argument("--compression-level", default=None, help="Zip deflate level 0-9 (default 9)")
    parser.add_argument("--write-checksum-file", default=None, help="Write <zip>.sha256 sidecar file (true/false)")
    parser.add_argument("--include-readme", default=None, help="Keep README.md inside zip (true/false)")


def _collect_config_overrides(args: argparse.Namespace) -> dict[str, Any]:
    raw = {
        "anykernel-script": _pick_str(args.anykernel_script, "anykernel-script"),
        "kernel-string": _pick_str(args.kernel_string, "kernel-string"),
        "do-devicecheck": _pick_str(args.do_devicecheck, "do-devicecheck"),
        "do-modules": _pick_str(args.do_modules, "do-modules"),
        "do-systemless": _pick_str(args.do_systemless, "do-systemless"),
        "do-cleanup": _pick_str(args.do_cleanup, "do-cleanup"),
        "do-cleanuponabort": _pick_str(args.do_cleanuponabort, "do-cleanuponabort"),
        "device-names": _pick_raw(args.device_names, "device-names"),
        "supported-versions": _pick_str(args.supported_versions, "supported-versions"),
        "supported-patchlevels": _pick_str(args.supported_patchlevels, "supported-patchlevels"),
        "supported-vendorpatchlevels": _pick_str(args.supported_vendorpatchlevels, "supported-vendorpatchlevels"),
        "extra-properties": _pick_raw(args.extra_properties, "extra-properties"),
        "block": _pick_str(args.block, "block"),
        "is-slot-device": _pick_str(args.is_slot_device, "is-slot-device"),
        "ramdisk-compression": _pick_str(args.ramdisk_compression, "ramdisk-compression"),
        "patch-vbmeta-flag": _pick_str(args.patch_vbmeta_flag, "patch-vbmeta-flag"),
        "custom-dd": _pick_str(args.custom_dd, "custom-dd"),
        "slot-select": _pick_str(args.slot_select, "slot-select"),
        "no-block-display": _pick_str(args.no_block_display, "no-block-display"),
        "no-magisk-check": _pick_str(args.no_magisk_check, "no-magisk-check"),
        "no-vbmeta-partition-patch": _pick_str(args.no_vbmeta_partition_patch, "no-vbmeta-partition-patch"),
        "install-mode": _pick_str(args.install_mode, "install-mode"),
        "strip-example-patches": _pick_str(args.strip_example_patches, "strip-example-patches"),
        "boot-attributes": _pick_raw(args.boot_attributes, "boot-attributes"),
        "ramdisk-patch-commands": _pick_raw(args.ramdisk_patch_commands, "ramdisk-patch-commands"),
        "flash-generic": _pick_raw(args.flash_generic, "flash-generic"),
        "extra-partitions": _pick_raw(args.extra_partitions, "extra-partitions"),
        "strict": _pick_str(args.strict, "strict"),
    }
    return overrides_from_dict(raw)


def _cmd_prepare(args: argparse.Namespace) -> int:
    prepared = prepare_anykernel_base(
        anykernel_repo=_pick_str(args.anykernel_repo, "anykernel-repo", DEFAULT_ANYKERNEL_REPO) or DEFAULT_ANYKERNEL_REPO,
        anykernel_ref=_pick_str(args.anykernel_ref, "anykernel-ref", DEFAULT_ANYKERNEL_REF) or DEFAULT_ANYKERNEL_REF,
        anykernel_path=_opt_path(_pick_str(args.anykernel_path, "anykernel-path")),
        staging_dir=_opt_path(_pick_str(args.staging_dir, "staging-dir")),
        clean_staging=_pick_bool(args.clean_staging, "clean-staging", True),
        strip_readme=_pick_bool(args.strip_readme, "strip-readme", True),
    )
    set_github_output("staging-dir", str(prepared.staging_dir))
    set_github_output("ak-base-version", prepared.ak_base_version)
    set_github_output("anykernel-commit", prepared.commit_sha)
    return 0


def _cmd_discover(args: argparse.Namespace) -> int:
    exclude: set[Path] = set()
    staging_raw = _pick_str(args.staging_dir, "staging-dir")
    if staging_raw:
        exclude.add(Path(staging_raw).resolve())
    action_root = os.environ.get("ACTION_PATH", "").strip()
    if action_root:
        exclude.add(Path(action_root).resolve())

    res = discover_kernel_artifacts(
        cwd=Path.cwd(),
        workspace=_opt_path(_pick_str(args.workspace, "workspace")),
        artifacts_dir=_opt_path(_pick_str(args.artifacts_dir, "artifacts-dir")),
        target_arch=_pick_str(args.search_arch, "search-arch"),
        autodetect_extras=_pick_bool(args.autodetect_extras, "autodetect-extras", True),
        exclude_dirs=exclude,
    )

    set_github_output("kernel-image", str(res.kernel_path))
    set_github_output("kernel-image-name", res.kernel_image_name)
    set_github_output("kernel-dir", str(res.kernel_path.parent))
    set_github_output("kernel-version", res.verification.kernel_version)
    set_github_output("kernel-arch", res.verification.arch_hint)
    set_github_output("kernel-confidence", res.verification.confidence)
    set_github_output("discovered-from", res.source_stage)
    set_github_output("kernel-source-dir", str(res.kernel_source_dir) if res.kernel_source_dir else "")
    set_github_output("dtb-path", str(res.dtb_path) if res.dtb_path else "")
    set_github_output("dtbo-path", str(res.dtbo_path) if res.dtbo_path else "")
    set_github_output("system-dlkm-path", str(res.system_dlkm_path) if res.system_dlkm_path else "")
    set_github_output("vendor-dlkm-path", str(res.vendor_dlkm_path) if res.vendor_dlkm_path else "")
    set_github_output("modules-path", str(res.modules_path) if res.modules_path else "")
    return 0


def _cmd_process_kernel(args: argparse.Namespace) -> int:
    staging_raw = _pick_str(args.staging_dir, "staging-dir")
    if not staging_raw:
        raise ValueError("--staging-dir (or INPUT_STAGING-DIR) is required for process-kernel.")

    # Optional context hints so {device} and {kernel_string} resolve inside banner/version
    device_names_raw = get_env_raw("device-names")
    kernel_string_raw = get_env_input("kernel-string") or ""
    extra_ctx: dict[str, str] = {}
    if device_names_raw:
        from .github_env import parse_list

        devs = parse_list(device_names_raw)
        if devs:
            extra_ctx["device"] = devs[0]
            extra_ctx["devices"] = "-".join(devs)
    if kernel_string_raw:
        extra_ctx["kernel_string"] = kernel_string_raw

    res, discovery = stage_kernel_with_optional_autodetect(
        Path(staging_raw),
        template_context=extra_ctx or None,
        **_extract_kernel_kwargs(args),
    )
    set_github_output("kernel-image", res.kernel_image)
    set_github_output("kernel-version", res.kernel_version)
    set_github_output("modules-count", str(res.modules_count))
    set_github_output("discovered-from", discovery.source_stage if discovery else "manual")
    set_github_output("kernel-confidence", res.verification.confidence if res.verification else "")
    return 0


def _cmd_configure(args: argparse.Namespace) -> int:
    staging_raw = _pick_str(args.staging_dir, "staging-dir")
    if not staging_raw:
        raise ValueError("--staging-dir (or INPUT_STAGING-DIR) is required for configure.")

    props = resolve_configured_script(
        Path(staging_raw),
        config_file=_opt_path(_pick_str(args.config_file, "config-file")),
        overrides=_collect_config_overrides(args),
    )
    set_github_output("kernel-string", props.get("kernel.string", ""))
    devices = [v for k, v in props.items() if k.startswith("device.name") and v.strip()]
    set_github_output("device-names", ",".join(devices))
    return 0


def _cmd_package(args: argparse.Namespace) -> int:
    staging_raw = _pick_str(args.staging_dir, "staging-dir")
    if not staging_raw:
        raise ValueError("--staging-dir (or INPUT_STAGING-DIR) is required for package.")

    if not _pick_bool(args.create_zip, "create-zip", True):
        print("Skipping zip creation (create-zip=false)")
        set_github_output("zip-path", "")
        set_github_output("zip-name", "")
        set_github_output("sha256", "")
        set_github_output("md5", "")
        set_github_output("size-bytes", "0")
        return 0

    comp_raw = _pick_str(args.compression_level, "compression-level", "9") or "9"
    include_readme_raw = _pick_str(args.include_readme, "include-readme")
    if include_readme_raw is not None:
        include_readme = is_truthy(include_readme_raw, default=False)
    else:
        include_readme = not _pick_bool(None, "strip-readme", True)
    pkg = package_anykernel_zip(
        Path(staging_raw),
        output_dir=_opt_path(_pick_str(args.output_dir, "output-dir")),
        zip_name=_pick_str(args.zip_name, "zip-name"),
        compression_level=int(comp_raw),
        include_readme=include_readme,
        write_checksum_file=_pick_bool(args.write_checksum_file, "write-checksum-file", False),
    )
    set_github_output("zip-path", str(pkg.zip_path))
    set_github_output("zip-name", pkg.zip_name)
    set_github_output("sha256", pkg.sha256)
    set_github_output("md5", pkg.md5)
    set_github_output("size-bytes", str(pkg.size_bytes))
    return 0


def _cmd_build(args: argparse.Namespace) -> int:
    comp_raw = _pick_str(args.compression_level, "compression-level", "9") or "9"
    res = run_full_pipeline(
        anykernel_repo=_pick_str(args.anykernel_repo, "anykernel-repo", DEFAULT_ANYKERNEL_REPO) or DEFAULT_ANYKERNEL_REPO,
        anykernel_ref=_pick_str(args.anykernel_ref, "anykernel-ref", DEFAULT_ANYKERNEL_REF) or DEFAULT_ANYKERNEL_REF,
        anykernel_path=_opt_path(_pick_str(args.anykernel_path, "anykernel-path")),
        staging_dir=_opt_path(_pick_str(args.staging_dir, "staging-dir")),
        clean_staging=_pick_bool(args.clean_staging, "clean-staging", True),
        strip_readme=_pick_bool(args.strip_readme, "strip-readme", True),
        **_extract_kernel_kwargs(args),
        config_file=_opt_path(_pick_str(args.config_file, "config-file")),
        config_overrides=_collect_config_overrides(args),
        create_zip=_pick_bool(args.create_zip, "create-zip", True),
        output_dir=_opt_path(_pick_str(args.output_dir, "output-dir")),
        zip_name=_pick_str(args.zip_name, "zip-name"),
        compression_level=int(comp_raw),
        write_checksum_file=_pick_bool(args.write_checksum_file, "write-checksum-file", False),
    )

    set_github_output("staging-dir", str(res.prepared.staging_dir))
    set_github_output("ak-base-version", res.prepared.ak_base_version)
    set_github_output("anykernel-commit", res.prepared.commit_sha)
    set_github_output("kernel-image", res.kernel.kernel_image)
    set_github_output("kernel-version", res.kernel.kernel_version)
    set_github_output("modules-count", str(res.kernel.modules_count))
    set_github_output("discovered-from", res.discovery.source_stage if res.discovery else "manual")
    set_github_output(
        "kernel-confidence",
        res.kernel.verification.confidence if res.kernel.verification else "",
    )
    set_github_output("kernel-string", res.properties.get("kernel.string", ""))
    if res.package is not None:
        set_github_output("zip-path", str(res.package.zip_path))
        set_github_output("zip-name", res.package.zip_name)
        set_github_output("sha256", res.package.sha256)
        set_github_output("md5", res.package.md5)
        set_github_output("size-bytes", str(res.package.size_bytes))
    else:
        set_github_output("zip-path", "")
        set_github_output("zip-name", "")
        set_github_output("sha256", "")
        set_github_output("md5", "")
        set_github_output("size-bytes", "0")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ak3-actions",
        description="Modular Python components for packaging compiled kernels with AnyKernel3.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_prepare = sub.add_parser("prepare", help="Prepare AnyKernel3 base and strip .git tags/metadata")
    _add_repo_args(p_prepare)
    p_prepare.set_defaults(func=_cmd_prepare)

    p_discover = sub.add_parser(
        "discover",
        help="[Beta] Scour cwd, kernel source, out/, arch/, and artifacts for a verified kernel",
    )
    p_discover.add_argument("--workspace", default=None, help="Workspace root directory to index")
    p_discover.add_argument("--artifacts-dir", default=None, help="Downloaded artifacts directory to check")
    p_discover.add_argument("--search-arch", default=None, help="Target architecture hint (arm64, arm, x86, riscv)")
    p_discover.add_argument("--autodetect-extras", default=None, help="Also detect companion dtb/dtbo/modules (true/false)")
    p_discover.add_argument("--staging-dir", default=None, help="Optional AnyKernel3 staging dir to exclude from scour")
    p_discover.set_defaults(func=_cmd_discover)

    p_kernel = sub.add_parser("process-kernel", help="Discover and stage compiled kernel artifacts")
    p_kernel.add_argument("--staging-dir", default=None, help="Prepared AnyKernel3 staging directory")
    _add_kernel_args(p_kernel)
    p_kernel.set_defaults(func=_cmd_process_kernel)

    p_configure = sub.add_parser("configure", help="Configure anykernel.sh properties and variables")
    p_configure.add_argument("--staging-dir", default=None, help="Prepared AnyKernel3 staging directory")
    _add_config_args(p_configure)
    p_configure.set_defaults(func=_cmd_configure)

    p_package = sub.add_parser("package", help="Package staged AnyKernel3 tree into a flashable zip")
    p_package.add_argument("--staging-dir", default=None, help="Prepared AnyKernel3 staging directory")
    _add_package_args(p_package)
    p_package.set_defaults(func=_cmd_package)

    p_build = sub.add_parser("build", help="Run full AnyKernel3 pipeline end-to-end")
    _add_repo_args(p_build)
    _add_kernel_args(p_build)
    _add_config_args(p_build)
    _add_package_args(p_build)
    p_build.set_defaults(func=_cmd_build)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
