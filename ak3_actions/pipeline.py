"""Pipeline orchestration merging config files, env inputs, and CLI args."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import (
    AnyKernelConfig,
    canonicalize_config_dict,
    config_from_mapping,
    configure_anykernel_script,
    parse_extra_partitions,
)
from .discover import DiscoveryResult, discover_kernel_artifacts
from .github_env import (
    build_template_context,
    info,
    is_truthy,
    load_config_file,
    load_staging_state,
    normalize_ak_flag,
    parse_kv_lines,
    parse_list,
    resolve_template,
    save_staging_state,
)
from .kernel import KernelProcessResult, process_compiled_kernel
from .packager import PackageResult, package_anykernel_zip
from .repo import DEFAULT_ANYKERNEL_REF, DEFAULT_ANYKERNEL_REPO, PreparedRepo, prepare_anykernel_base


@dataclass
class BuildResult:
    """End-to-end result of preparing, staging, configuring, and packaging AnyKernel3."""

    prepared: PreparedRepo
    kernel: KernelProcessResult
    properties: dict[str, str]
    package: PackageResult | None = None
    discovery: DiscoveryResult | None = None


def _to_kv_dict(raw: Any) -> dict[str, str]:
    if isinstance(raw, dict):
        return {str(k).strip(): str(v).strip() for k, v in raw.items()}
    if isinstance(raw, str):
        return parse_kv_lines(raw)
    return {}


def build_effective_config(
    *,
    config_file: Path | None = None,
    overrides: dict[str, Any] | None = None,
) -> AnyKernelConfig:
    """Merge optional config file with explicit non-empty overrides."""
    merged: dict[str, Any] = {}
    if config_file is not None:
        merged.update(canonicalize_config_dict(load_config_file(config_file)))

    if overrides:
        norm_overrides = canonicalize_config_dict(overrides)
        if "extra-properties" in merged and "extra-properties" in norm_overrides:
            combined_extra = _to_kv_dict(merged["extra-properties"])
            combined_extra.update(_to_kv_dict(norm_overrides["extra-properties"]))
            norm_overrides["extra-properties"] = combined_extra
        merged.update(norm_overrides)

    return config_from_mapping(merged)


def resolve_configured_script(
    staging_dir: Path,
    *,
    config_file: Path | None = None,
    overrides: dict[str, Any] | None = None,
    workspace: Path | None = None,
) -> dict[str, str]:
    """Resolve templates, auto-enable do.modules when modules were staged, and configure anykernel.sh."""
    cfg = build_effective_config(config_file=config_file, overrides=overrides)
    state = load_staging_state(staging_dir)

    modules_count = int(state.get("modules_count") or 0)
    if cfg.do_modules is None and modules_count > 0:
        cfg.do_modules = "1"
        info(f"Auto-enabling do.modules=1 because {modules_count} kernel module(s) were staged.")

    ctx = build_template_context(
        workspace=workspace,
        kernel_version=str(state.get("kernel_version", "")),
        kernel_image=str(state.get("kernel_image", "")),
        device_names=cfg.device_names,
        ak_base_version=str(state.get("ak_base_version", "")),
    )

    if cfg.kernel_string is not None:
        cfg.kernel_string = resolve_template(cfg.kernel_string, ctx)
    if cfg.extra_properties:
        cfg.extra_properties = {
            k: resolve_template(v, ctx) for k, v in cfg.extra_properties.items()
        }
    if cfg.boot_attributes_body is not None:
        cfg.boot_attributes_body = resolve_template(cfg.boot_attributes_body, ctx)
    if cfg.ramdisk_patch_commands is not None:
        cfg.ramdisk_patch_commands = resolve_template(cfg.ramdisk_patch_commands, ctx)

    props = configure_anykernel_script(staging_dir, cfg)

    # Re-resolve banner/version in case kernel.string itself expanded templates (e.g. {kernel_version})
    ctx["kernel_string"] = props.get("kernel.string", "")
    for extra_name in ("banner", "version"):
        extra_file = staging_dir / extra_name
        if extra_file.is_file():
            raw_text = extra_file.read_text(encoding="utf-8")
            resolved_text = resolve_template(raw_text, ctx)
            if resolved_text != raw_text:
                extra_file.write_text(resolved_text, encoding="utf-8")

    return props


def stage_kernel_with_optional_autodetect(
    staging_dir: Path,
    *,
    kernel_image: str | Path | None = None,
    kernel_image_name: str | None = None,
    kernel_dir: str | Path | None = None,
    autodetect: bool = False,
    artifacts_dir: Path | None = None,
    search_arch: str | None = None,
    autodetect_extras: bool = True,
    verify_kernel: bool = False,
    dtb_path: str | Path | None = None,
    dt_path: str | Path | None = None,
    dtbo_path: str | Path | None = None,
    recovery_dtbo_path: str | Path | None = None,
    system_dlkm_path: str | Path | None = None,
    vendor_dlkm_path: str | Path | None = None,
    modules_path: str | Path | None = None,
    modules_dest: str = "system/lib/modules",
    preserve_module_tree: bool = False,
    include_module_metadata: bool = False,
    strip_modules: bool = False,
    ramdisk_dir: Path | None = None,
    vendor_ramdisk_dir: Path | None = None,
    patch_dir: Path | None = None,
    vendor_patch_dir: Path | None = None,
    banner: str | None = None,
    banner_file: Path | None = None,
    version: str | None = None,
    version_file: Path | None = None,
    workspace: Path | None = None,
    template_context: dict[str, str] | None = None,
    require_kernel: bool = False,
) -> tuple[KernelProcessResult, DiscoveryResult | None]:
    """Run compiled kernel staging, invoking [Beta] autodetect scour when requested."""
    discovery: DiscoveryResult | None = None
    effective_verify = verify_kernel

    if not kernel_image and not kernel_dir and (autodetect or artifacts_dir is not None):
        exclude: set[Path] = {staging_dir.resolve()}
        action_root = os.environ.get("ACTION_PATH", "").strip()
        if action_root:
            exclude.add(Path(action_root).resolve())
        discovery = discover_kernel_artifacts(
            cwd=Path.cwd(),
            workspace=workspace,
            artifacts_dir=artifacts_dir,
            target_arch=search_arch,
            autodetect_extras=autodetect_extras,
            exclude_dirs=exclude,
        )
        kernel_image = str(discovery.kernel_path)
        effective_verify = True
        if autodetect_extras:
            dtb_path = dtb_path or discovery.dtb_path
            dtbo_path = dtbo_path or discovery.dtbo_path
            system_dlkm_path = system_dlkm_path or discovery.system_dlkm_path
            vendor_dlkm_path = vendor_dlkm_path or discovery.vendor_dlkm_path
            modules_path = modules_path or discovery.modules_path

    res = process_compiled_kernel(
        staging_dir,
        kernel_image=kernel_image,
        kernel_image_name=kernel_image_name,
        kernel_dir=kernel_dir,
        dtb_path=dtb_path,
        dt_path=dt_path,
        dtbo_path=dtbo_path,
        recovery_dtbo_path=recovery_dtbo_path,
        system_dlkm_path=system_dlkm_path,
        vendor_dlkm_path=vendor_dlkm_path,
        modules_path=modules_path,
        modules_dest=modules_dest,
        preserve_module_tree=preserve_module_tree,
        include_module_metadata=include_module_metadata,
        strip_modules=strip_modules,
        ramdisk_dir=ramdisk_dir,
        vendor_ramdisk_dir=vendor_ramdisk_dir,
        patch_dir=patch_dir,
        vendor_patch_dir=vendor_patch_dir,
        banner=banner,
        banner_file=banner_file,
        version=version,
        version_file=version_file,
        workspace=workspace,
        template_context=template_context,
        require_kernel=require_kernel,
        verify_kernel=effective_verify,
    )

    state_updates: dict[str, Any] = {
        "discovered_from": discovery.source_stage if discovery else "manual",
        "kernel_confidence": res.verification.confidence if res.verification else "",
        "kernel_arch": res.verification.arch_hint if res.verification else "",
    }
    if discovery and discovery.kernel_source_dir is not None:
        state_updates["kernel_source_dir"] = str(discovery.kernel_source_dir)
    save_staging_state(staging_dir, state_updates)

    return res, discovery


def run_full_pipeline(
    *,
    # Base repo options
    anykernel_repo: str = DEFAULT_ANYKERNEL_REPO,
    anykernel_ref: str = DEFAULT_ANYKERNEL_REF,
    anykernel_path: Path | None = None,
    staging_dir: Path | None = None,
    clean_staging: bool = True,
    strip_readme: bool = True,
    # Kernel options
    kernel_image: str | Path | None = None,
    kernel_image_name: str | None = None,
    kernel_dir: str | Path | None = None,
    autodetect: bool = False,
    artifacts_dir: Path | None = None,
    search_arch: str | None = None,
    autodetect_extras: bool = True,
    verify_kernel: bool = False,
    dtb_path: str | Path | None = None,
    dt_path: str | Path | None = None,
    dtbo_path: str | Path | None = None,
    recovery_dtbo_path: str | Path | None = None,
    system_dlkm_path: str | Path | None = None,
    vendor_dlkm_path: str | Path | None = None,
    modules_path: str | Path | None = None,
    modules_dest: str = "system/lib/modules",
    preserve_module_tree: bool = False,
    include_module_metadata: bool = False,
    strip_modules: bool = False,
    ramdisk_dir: Path | None = None,
    vendor_ramdisk_dir: Path | None = None,
    patch_dir: Path | None = None,
    vendor_patch_dir: Path | None = None,
    banner: str | None = None,
    banner_file: Path | None = None,
    version: str | None = None,
    version_file: Path | None = None,
    require_kernel: bool = False,
    # Config options
    config_file: Path | None = None,
    config_overrides: dict[str, Any] | None = None,
    # Packaging options
    create_zip: bool = True,
    output_dir: Path | None = None,
    zip_name: str | None = None,
    compression_level: int = 9,
    write_checksum_file: bool = False,
    workspace: Path | None = None,
) -> BuildResult:
    """Execute the complete AnyKernel3 modular packaging pipeline."""
    prepared = prepare_anykernel_base(
        anykernel_repo=anykernel_repo,
        anykernel_ref=anykernel_ref,
        anykernel_path=anykernel_path,
        staging_dir=staging_dir,
        clean_staging=clean_staging,
        strip_readme=strip_readme,
    )

    cfg_preview = build_effective_config(config_file=config_file, overrides=config_overrides)
    preview_ctx = build_template_context(
        workspace=workspace,
        device_names=cfg_preview.device_names,
        kernel_string=cfg_preview.kernel_string or "",
        ak_base_version=prepared.ak_base_version,
    )

    kernel_res, discovery = stage_kernel_with_optional_autodetect(
        prepared.staging_dir,
        kernel_image=kernel_image,
        kernel_image_name=kernel_image_name,
        kernel_dir=kernel_dir,
        autodetect=autodetect,
        artifacts_dir=artifacts_dir,
        search_arch=search_arch,
        autodetect_extras=autodetect_extras,
        verify_kernel=verify_kernel,
        dtb_path=dtb_path,
        dt_path=dt_path,
        dtbo_path=dtbo_path,
        recovery_dtbo_path=recovery_dtbo_path,
        system_dlkm_path=system_dlkm_path,
        vendor_dlkm_path=vendor_dlkm_path,
        modules_path=modules_path,
        modules_dest=modules_dest,
        preserve_module_tree=preserve_module_tree,
        include_module_metadata=include_module_metadata,
        strip_modules=strip_modules,
        ramdisk_dir=ramdisk_dir,
        vendor_ramdisk_dir=vendor_ramdisk_dir,
        patch_dir=patch_dir,
        vendor_patch_dir=vendor_patch_dir,
        banner=banner,
        banner_file=banner_file,
        version=version,
        version_file=version_file,
        workspace=workspace,
        template_context=preview_ctx,
        require_kernel=require_kernel,
    )

    # Re-resolve banner/version if kernel_string itself contained templates
    props = resolve_configured_script(
        prepared.staging_dir,
        config_file=config_file,
        overrides=config_overrides,
        workspace=workspace,
    )

    pkg: PackageResult | None = None
    if create_zip:
        pkg = package_anykernel_zip(
            prepared.staging_dir,
            output_dir=output_dir,
            zip_name=zip_name,
            compression_level=compression_level,
            include_readme=not strip_readme,
            write_checksum_file=write_checksum_file,
            workspace=workspace,
        )

    return BuildResult(
        prepared=prepared,
        kernel=kernel_res,
        properties=props,
        package=pkg,
        discovery=discovery,
    )


def overrides_from_dict(raw: dict[str, Any]) -> dict[str, Any]:
    """Normalize raw CLI/env input keys into config overrides."""
    normalized: dict[str, Any] = {}
    for k, v in raw.items():
        if v is None:
            continue
        if isinstance(v, str) and v.strip() == "":
            continue
        normalized[k] = v

    # Parse structured strings if provided as raw strings
    if isinstance(normalized.get("device-names"), str):
        normalized["device-names"] = parse_list(normalized["device-names"])
    if isinstance(normalized.get("extra-properties"), str):
        normalized["extra-properties"] = parse_kv_lines(normalized["extra-properties"])
    if isinstance(normalized.get("extra-partitions"), str):
        normalized["extra-partitions"] = parse_extra_partitions(normalized["extra-partitions"])
    for flag_key in (
        "do-devicecheck",
        "do-modules",
        "do-systemless",
        "do-cleanup",
        "do-cleanuponabort",
    ):
        if flag_key in normalized:
            normalized[flag_key] = normalize_ak_flag(normalized[flag_key])
    for auto_flag_key in ("is-slot-device", "patch-vbmeta-flag"):
        if auto_flag_key in normalized:
            normalized[auto_flag_key] = normalize_ak_flag(normalized[auto_flag_key], allow_auto=True)
    for bool_key in (
        "no-block-display",
        "no-magisk-check",
        "no-vbmeta-partition-patch",
        "strip-example-patches",
        "deepsleep-fix",
        "strict",
    ):
        if bool_key in normalized:
            normalized[bool_key] = is_truthy(normalized[bool_key])
    return normalized
