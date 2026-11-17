"""Component 3: Parse, configure, render, and validate AnyKernel3 anykernel.sh."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .github_env import (
    info,
    is_truthy,
    load_staging_state,
    normalize_ak_flag,
    parse_kv_lines,
    parse_list,
    save_staging_state,
    warn,
)

UPSTREAM_DEFAULT_BLOCK = "/dev/block/platform/omap/omap_hsmmc.0/by-name/boot"
UPSTREAM_DEFAULT_DEVICES = frozenset({"maguro", "toro", "toroplus", "tuna", ""})
VALID_RAMDISK_COMPRESSIONS = frozenset({
    "auto",
    "gz",
    "lzo",
    "lzma",
    "xz",
    "bz2",
    "lz4",
    "lz4-l",
    "cpio",
    "none",
})
VALID_IS_SLOT = frozenset({"0", "1", "auto"})
VALID_PATCH_VBMETA = frozenset({"auto", "0", "1"})
VALID_SLOT_SELECT = frozenset({"active", "inactive"})
VALID_INSTALL_MODES = frozenset({"dump_write", "split_flash", "keep"})
_PATCHLEVEL_RE = re.compile(
    r"^(?:\d{4}-\d{2}|-\s*\d{4}-\d{2}|\d{4}-\d{2}\s*-|\d{4}-\d{2}\s*-\s*\d{4}-\d{2})$"
)

_PROPERTIES_BLOCK_RE = re.compile(
    r"(properties\(\)\s*\{\s*'\r?\n)(.*?)(\r?\n';\s*\}(?:[^\n]*)?)",
    re.DOTALL,
)
_BOOT_ATTR_BLOCK_RE = re.compile(
    r"(boot_attributes\(\)\s*\{\r?\n)(.*?)(\r?\n\}\s*#[^\n]*)",
    re.DOTALL,
)
_BOOT_INSTALL_BLOCK_RE = re.compile(
    r"(# boot install\r?\n)(.*?)(\r?\n## end boot install)",
    re.DOTALL,
)
_AK3_CORE_LINE_RE = re.compile(r"^\.\s+tools/ak3-core\.sh;\s*$", re.MULTILINE)
_COMMENTED_PARTITIONS_RE = re.compile(
    r"\n## init_boot files attributes\b.*\Z",
    re.DOTALL,
)

_STANDARD_PROP_ORDER = (
    "kernel.string",
    "do.devicecheck",
    "do.modules",
    "do.systemless",
    "do.cleanup",
    "do.cleanuponabort",
)
_TRAILING_PROP_ORDER = (
    "supported.versions",
    "supported.patchlevels",
    "supported.vendorpatchlevels",
)
_DEVICE_NAME_KEY_RE = re.compile(r"^device\.name(\d+)$")


@dataclass
class PartitionBlock:
    """Additional AnyKernel3 partition block (e.g. init_boot, vendor_kernel_boot, vendor_boot)."""

    name: str
    block: str = ""
    is_slot_device: str = "1"
    ramdisk_compression: str = "auto"
    patch_vbmeta_flag: str = "auto"
    install_mode: str = "dump_write"
    attributes_body: str = ""
    commands: str = ""
    keep_on_reset: bool = False

    def __post_init__(self) -> None:
        self.name = self.name.strip()
        if not self.name or not re.match(r"^[a-zA-Z_][a-zA-Z0-9_]*$", self.name):
            raise ValueError(f"Invalid partition block name: {self.name!r}")
        if not self.block:
            self.block = self.name
        if self.install_mode not in {"dump_write", "split_flash"}:
            raise ValueError(
                f"Partition {self.name!r} install_mode must be 'dump_write' or 'split_flash', got {self.install_mode!r}"
            )


@dataclass
class AnyKernelConfig:
    """Configuration options for AnyKernel3 anykernel.sh."""

    # properties() entries
    kernel_string: str | None = None
    do_devicecheck: str | None = None
    do_modules: str | None = None
    do_systemless: str | None = None
    do_cleanup: str | None = None
    do_cleanuponabort: str | None = None
    device_names: list[str] | None = None
    supported_versions: str | None = None
    supported_patchlevels: str | None = None
    supported_vendorpatchlevels: str | None = None
    extra_properties: dict[str, str] = field(default_factory=dict)

    # boot shell variables
    block: str | None = None
    is_slot_device: str | None = None
    ramdisk_compression: str | None = None
    patch_vbmeta_flag: str | None = None
    custom_dd: str | None = None
    slot_select: str | None = None
    no_block_display: bool | None = None
    no_magisk_check: bool | None = None
    no_vbmeta_partition_patch: bool | None = None

    # install / script behavior
    custom_script_path: Path | None = None
    install_mode: str = "dump_write"
    strip_example_patches: bool = True
    boot_attributes_body: str | None = None
    ramdisk_patch_commands: str | None = None
    deepsleep_fix: bool = False
    flash_generic_partitions: list[str] = field(default_factory=list)
    extra_partitions: list[PartitionBlock] = field(default_factory=list)
    strict: bool = False


DEEPSLEEP_FIX_MARKER = "# Deepsleep fix (@Chainfire)"
DEEPSLEEP_FIX_SNIPPET = """# Deepsleep fix (@Chainfire)
for i in `ls /sys/class/scsi_disk/ 2>/dev/null`; do
\tcat /sys/class/scsi_disk/$i/write_protect 2>/dev/null | grep 1 >/dev/null
\tif [ $? -eq 0 ]; then
\t\techo 'temporary none' > /sys/class/scsi_disk/$i/cache_type
\tfi
done;"""


def parse_extra_partitions(raw: str | list[dict[str, Any]] | None) -> list[PartitionBlock]:
    """Parse extra partition definitions from JSON string or list of dicts."""
    if not raw:
        return []
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return []
        data = json.loads(text)
    else:
        data = raw

    if not isinstance(data, list):
        raise ValueError("extra-partitions must be a JSON array of partition objects.")

    blocks: list[PartitionBlock] = []
    for item in data:
        if not isinstance(item, dict):
            raise ValueError(f"Each extra-partitions entry must be an object, got {type(item).__name__}")
        blocks.append(
            PartitionBlock(
                name=str(item["name"]),
                block=str(item.get("block", item["name"])),
                is_slot_device=normalize_ak_flag(item.get("is_slot_device", item.get("is-slot-device", "1")), allow_auto=True) or "1",
                ramdisk_compression=str(item.get("ramdisk_compression", item.get("ramdisk-compression", "auto"))),
                patch_vbmeta_flag=normalize_ak_flag(item.get("patch_vbmeta_flag", item.get("patch-vbmeta-flag", "auto")), allow_auto=True) or "auto",
                install_mode=str(item.get("install_mode", item.get("install-mode", "dump_write"))),
                attributes_body=str(item.get("attributes_body", item.get("boot_attributes", ""))),
                commands=str(item.get("commands", "")),
                keep_on_reset=is_truthy(item.get("keep_on_reset", False)),
            )
        )
    return blocks


_KEY_ALIASES: dict[str, str] = {
    "kernel.string": "kernel-string",
    "kernel_string": "kernel-string",
    "do.devicecheck": "do-devicecheck",
    "do_devicecheck": "do-devicecheck",
    "do.modules": "do-modules",
    "do_modules": "do-modules",
    "do.systemless": "do-systemless",
    "do_systemless": "do-systemless",
    "do.cleanup": "do-cleanup",
    "do_cleanup": "do-cleanup",
    "do.cleanuponabort": "do-cleanuponabort",
    "do_cleanuponabort": "do-cleanuponabort",
    "device.names": "device-names",
    "device_names": "device-names",
    "supported.versions": "supported-versions",
    "supported_versions": "supported-versions",
    "supported.patchlevels": "supported-patchlevels",
    "supported_patchlevels": "supported-patchlevels",
    "supported.vendorpatchlevels": "supported-vendorpatchlevels",
    "supported_vendorpatchlevels": "supported-vendorpatchlevels",
    "extra_properties": "extra-properties",
    "properties": "extra-properties",
    "BLOCK": "block",
    "IS_SLOT_DEVICE": "is-slot-device",
    "is_slot_device": "is-slot-device",
    "RAMDISK_COMPRESSION": "ramdisk-compression",
    "ramdisk_compression": "ramdisk-compression",
    "PATCH_VBMETA_FLAG": "patch-vbmeta-flag",
    "patch_vbmeta_flag": "patch-vbmeta-flag",
    "CUSTOMDD": "custom-dd",
    "custom_dd": "custom-dd",
    "SLOT_SELECT": "slot-select",
    "slot_select": "slot-select",
    "NO_BLOCK_DISPLAY": "no-block-display",
    "no_block_display": "no-block-display",
    "NO_MAGISK_CHECK": "no-magisk-check",
    "no_magisk_check": "no-magisk-check",
    "NO_VBMETA_PARTITION_PATCH": "no-vbmeta-partition-patch",
    "no_vbmeta_partition_patch": "no-vbmeta-partition-patch",
    "anykernel_script": "anykernel-script",
    "custom_script_path": "anykernel-script",
    "install_mode": "install-mode",
    "strip_example_patches": "strip-example-patches",
    "boot_attributes": "boot-attributes",
    "boot_attributes_body": "boot-attributes",
    "ramdisk_patch_commands": "ramdisk-patch-commands",
    "boot_install_commands": "ramdisk-patch-commands",
    "deepsleep_fix": "deepsleep-fix",
    "deepsleep.fix": "deepsleep-fix",
    "flash_generic": "flash-generic",
    "flash_generic_partitions": "flash-generic",
    "extra_partitions": "extra-partitions",
}


def canonicalize_config_dict(data: dict[str, Any]) -> dict[str, Any]:
    """Normalize config keys (AK3 dot keys, snake_case, uppercase vars) to canonical kebab-case."""
    canonical: dict[str, Any] = {}
    numbered_devices: list[tuple[int, str]] = []

    for key, val in data.items():
        if val is None:
            continue
        if isinstance(val, str) and val.strip() == "":
            continue
        m = _DEVICE_NAME_KEY_RE.match(str(key))
        if m:
            numbered_devices.append((int(m.group(1)), str(val).strip()))
            continue
        canon_key = _KEY_ALIASES.get(str(key), str(key))
        canonical[canon_key] = val

    if numbered_devices and "device-names" not in canonical:
        numbered_devices.sort(key=lambda pair: pair[0])
        canonical["device-names"] = [v for _, v in numbered_devices]

    return canonical


def config_from_mapping(data: dict[str, Any]) -> AnyKernelConfig:
    """Build an AnyKernelConfig from a dict (supporting AK3 dot keys, kebab-case, and snake_case)."""
    norm = canonicalize_config_dict(data)

    def pick(key: str, default: Any = None) -> Any:
        val = norm.get(key)
        if val is not None and val != "":
            return val
        return default

    cfg = AnyKernelConfig()
    cfg.kernel_string = pick("kernel-string")
    cfg.do_devicecheck = normalize_ak_flag(pick("do-devicecheck"))
    cfg.do_modules = normalize_ak_flag(pick("do-modules"))
    cfg.do_systemless = normalize_ak_flag(pick("do-systemless"))
    cfg.do_cleanup = normalize_ak_flag(pick("do-cleanup"))
    cfg.do_cleanuponabort = normalize_ak_flag(pick("do-cleanuponabort"))

    raw_devices = pick("device-names")
    if raw_devices is not None:
        cfg.device_names = parse_list(raw_devices)

    sv = pick("supported-versions")
    if sv is not None:
        cfg.supported_versions = str(sv).strip()
    sp = pick("supported-patchlevels")
    if sp is not None:
        cfg.supported_patchlevels = str(sp).strip()
    svp = pick("supported-vendorpatchlevels")
    if svp is not None:
        cfg.supported_vendorpatchlevels = str(svp).strip()

    extra_props = pick("extra-properties")
    if isinstance(extra_props, dict):
        cfg.extra_properties = {str(k).strip(): str(v).strip() for k, v in extra_props.items()}
    elif isinstance(extra_props, str):
        cfg.extra_properties = parse_kv_lines(extra_props)

    cfg.block = pick("block")
    cfg.is_slot_device = normalize_ak_flag(pick("is-slot-device"), allow_auto=True)
    cfg.ramdisk_compression = pick("ramdisk-compression")
    cfg.patch_vbmeta_flag = normalize_ak_flag(pick("patch-vbmeta-flag"), allow_auto=True)
    cfg.custom_dd = pick("custom-dd")
    cfg.slot_select = pick("slot-select")

    nbd = pick("no-block-display")
    if nbd is not None:
        cfg.no_block_display = is_truthy(nbd)
    nmc = pick("no-magisk-check")
    if nmc is not None:
        cfg.no_magisk_check = is_truthy(nmc)
    nvp = pick("no-vbmeta-partition-patch")
    if nvp is not None:
        cfg.no_vbmeta_partition_patch = is_truthy(nvp)

    cs = pick("anykernel-script")
    if cs is not None:
        cfg.custom_script_path = Path(str(cs))

    im = pick("install-mode")
    if im is not None:
        cfg.install_mode = str(im).strip()
    elif cfg.custom_script_path is not None:
        cfg.install_mode = "keep"

    sep = pick("strip-example-patches")
    if sep is not None:
        cfg.strip_example_patches = is_truthy(sep)
    elif cfg.custom_script_path is not None:
        cfg.strip_example_patches = False

    cfg.boot_attributes_body = pick("boot-attributes")
    cfg.ramdisk_patch_commands = pick("ramdisk-patch-commands")

    dsf = pick("deepsleep-fix")
    if dsf is not None:
        cfg.deepsleep_fix = is_truthy(dsf)

    fg = pick("flash-generic")
    if fg is not None:
        cfg.flash_generic_partitions = parse_list(fg)

    ep = pick("extra-partitions")
    if ep is not None:
        cfg.extra_partitions = parse_extra_partitions(ep)

    st = pick("strict")
    if st is not None:
        cfg.strict = is_truthy(st)

    return cfg


def parse_properties_block(script_text: str) -> dict[str, str]:
    """Extract ordered key=value properties from anykernel.sh's properties() function."""
    match = _PROPERTIES_BLOCK_RE.search(script_text)
    if not match:
        raise ValueError("Could not locate `properties() { ' ... '; }` block in anykernel.sh")
    props: dict[str, str] = {}
    for line in match.group(2).splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in stripped:
            continue
        key, val = stripped.split("=", 1)
        props[key.strip()] = val.strip()
    return props


def _validate_property_value(key: str, value: str) -> str:
    if "\n" in value or "\r" in value:
        raise ValueError(f"Property {key!r} cannot contain newlines.")
    if "'" in value:
        raise ValueError(
            f"Property {key!r} contains a single quote ('), which breaks AnyKernel3's "
            "`properties() { ' ... '; }` single-quoted shell block."
        )
    return value


def _render_properties(existing: dict[str, str], config: AnyKernelConfig) -> str:
    merged = dict(existing)

    direct_updates = {
        "kernel.string": config.kernel_string,
        "do.devicecheck": config.do_devicecheck,
        "do.modules": config.do_modules,
        "do.systemless": config.do_systemless,
        "do.cleanup": config.do_cleanup,
        "do.cleanuponabort": config.do_cleanuponabort,
        "supported.versions": config.supported_versions,
        "supported.patchlevels": config.supported_patchlevels,
        "supported.vendorpatchlevels": config.supported_vendorpatchlevels,
    }
    for key, val in direct_updates.items():
        if val is not None:
            merged[key] = _validate_property_value(key, val)

    # Extract existing device names in numeric order
    existing_devices: list[tuple[int, str]] = []
    for k in list(merged.keys()):
        m = _DEVICE_NAME_KEY_RE.match(k)
        if m:
            existing_devices.append((int(m.group(1)), merged.pop(k)))
    existing_devices.sort(key=lambda pair: pair[0])

    if config.device_names is not None:
        final_devices = [d.strip() for d in config.device_names if d.strip()]
    else:
        raw_existing = [val for _, val in existing_devices]
        # If devicecheck is explicitly disabled and only upstream defaults exist, strip stale defaults
        if merged.get("do.devicecheck") == "0" and set(raw_existing).issubset(UPSTREAM_DEFAULT_DEVICES):
            final_devices = []
        else:
            final_devices = [val for val in raw_existing if val]

    for idx, dev in enumerate(final_devices, start=1):
        merged[f"device.name{idx}"] = _validate_property_value(f"device.name{idx}", dev)

    for key, val in config.extra_properties.items():
        merged[key] = _validate_property_value(key, val)

    lines: list[str] = []
    emitted: set[str] = set()

    for key in _STANDARD_PROP_ORDER:
        if key in merged:
            lines.append(f"{key}={merged[key]}")
            emitted.add(key)

    for idx in range(1, len(final_devices) + 1):
        key = f"device.name{idx}"
        if key in merged:
            lines.append(f"{key}={merged[key]}")
            emitted.add(key)

    for key in _TRAILING_PROP_ORDER:
        if key in merged:
            lines.append(f"{key}={merged[key]}")
            emitted.add(key)

    for key, val in merged.items():
        if key not in emitted:
            lines.append(f"{key}={val}")

    return "\n".join(lines)


def _format_shell_assignment(name: str, value: str) -> str:
    if name == "CUSTOMDD":
        escaped = value.replace('"', '\\"')
        return f'{name}="{escaped}";'
    if any(ch.isspace() for ch in value):
        escaped = value.replace('"', '\\"')
        return f'{name}="{escaped}";'
    return f"{name}={value};"


def _update_boot_shell_variables(script_text: str, config: AnyKernelConfig) -> str:
    """Update boot shell variables only in the primary boot section (before reset_ak)."""
    # Limit variable edits to the primary boot section so multi-partition reset_ak blocks stay intact
    reset_match = re.search(r"^\s*reset_ak\b", script_text, re.MULTILINE)
    split_pos = reset_match.start() if reset_match else len(script_text)
    head = script_text[:split_pos]
    tail = script_text[split_pos:]

    updates: dict[str, str | None] = {}
    if config.block is not None:
        updates["BLOCK"] = config.block
    if config.is_slot_device is not None:
        updates["IS_SLOT_DEVICE"] = config.is_slot_device
    if config.ramdisk_compression is not None:
        updates["RAMDISK_COMPRESSION"] = config.ramdisk_compression
    if config.patch_vbmeta_flag is not None:
        updates["PATCH_VBMETA_FLAG"] = config.patch_vbmeta_flag
    if config.custom_dd is not None:
        updates["CUSTOMDD"] = config.custom_dd
    if config.slot_select is not None:
        updates["SLOT_SELECT"] = config.slot_select

    # AnyKernel3 checks `[ ! "$NO_*" ]`, so false must omit/remove the variable rather than setting 0
    bool_var_map = {
        "NO_BLOCK_DISPLAY": config.no_block_display,
        "NO_MAGISK_CHECK": config.no_magisk_check,
        "NO_VBMETA_PARTITION_PATCH": config.no_vbmeta_partition_patch,
    }
    removals: set[str] = set()
    for var_name, bool_val in bool_var_map.items():
        if bool_val is True:
            updates[var_name] = "1"
        elif bool_val is False:
            removals.add(var_name)

    if not updates and not removals:
        return script_text

    updated_keys: set[str] = set()
    out_lines: list[str] = []
    assign_re = re.compile(r"^([A-Z][A-Z0-9_]*)=([^;\n]*);(.*)$")

    for line in head.splitlines():
        m = assign_re.match(line.strip())
        if m:
            var_name = m.group(1)
            suffix_comment = m.group(3)
            if var_name in removals:
                continue
            if var_name in updates and updates[var_name] is not None:
                rendered = _format_shell_assignment(var_name, str(updates[var_name]))
                out_lines.append(f"{rendered}{suffix_comment}")
                updated_keys.add(var_name)
                continue
        out_lines.append(line)

    new_head = "\n".join(out_lines)
    if head.endswith("\n"):
        new_head += "\n"

    missing_lines = [
        _format_shell_assignment(k, str(v))
        for k, v in updates.items()
        if k not in updated_keys and v is not None
    ]
    if missing_lines:
        insertion = "\n".join(missing_lines) + "\n\n"
        core_match = _AK3_CORE_LINE_RE.search(new_head)
        if core_match:
            idx = core_match.start()
            new_head = new_head[:idx] + insertion + new_head[idx:]
        else:
            new_head = new_head + "\n" + insertion

    return new_head + tail


def _strip_tuna_examples(install_body: str) -> str:
    """Remove upstream Galaxy Nexus (tuna) example ramdisk commands while keeping boot flow."""
    cleaned_lines: list[str] = []
    skip_section = False
    for line in install_body.splitlines():
        stripped = line.strip()
        if stripped in {"# init.rc", "# init.tuna.rc", "# fstab.tuna"}:
            skip_section = True
            continue
        if skip_section:
            if stripped == "":
                skip_section = False
            continue
        cleaned_lines.append(line)
    # Collapse excessive blank lines
    text = "\n".join(cleaned_lines)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text


def _collect_install_snippets(config: AnyKernelConfig, existing_text: str) -> str:
    snippets: list[str] = []
    if config.deepsleep_fix and DEEPSLEEP_FIX_MARKER not in existing_text:
        snippets.append(DEEPSLEEP_FIX_SNIPPET)
    if config.ramdisk_patch_commands and config.ramdisk_patch_commands.strip():
        snippets.append(config.ramdisk_patch_commands.strip())
    return "\n\n".join(snippets)


def _inject_after_unpack(body: str, snippet: str) -> str:
    if not snippet:
        return body
    out_lines: list[str] = []
    inserted = False
    for line in body.splitlines():
        out_lines.append(line)
        if not inserted and re.match(r"^(dump_boot|split_boot);", line.strip()):
            out_lines.append("")
            out_lines.append(snippet)
            inserted = True
    if inserted:
        return "\n".join(out_lines)
    return _inject_before_flash(body, snippet)


def _inject_before_flash(body: str, snippet: str) -> str:
    if not snippet:
        return body
    inserted = False
    out_lines: list[str] = []
    for line in body.splitlines():
        if not inserted and re.match(r"^(write_boot|flash_boot);", line.strip()):
            out_lines.append("")
            out_lines.append(snippet)
            out_lines.append("")
            inserted = True
        out_lines.append(line)
    if not inserted:
        out_lines.extend(["", snippet])
    return "\n".join(out_lines)


def _apply_install_extras(body: str, config: AnyKernelConfig) -> str:
    if config.deepsleep_fix and DEEPSLEEP_FIX_MARKER not in body:
        body = _inject_after_unpack(body, DEEPSLEEP_FIX_SNIPPET)
    if config.ramdisk_patch_commands and config.ramdisk_patch_commands.strip():
        body = _inject_before_flash(body, config.ramdisk_patch_commands.strip())
    return body


def _build_boot_install_body(
    existing_body: str,
    config: AnyKernelConfig,
    staging_dir: Path | None,
) -> str:
    if config.install_mode not in VALID_INSTALL_MODES:
        raise ValueError(
            f"Invalid install-mode {config.install_mode!r}; expected one of {sorted(VALID_INSTALL_MODES)}"
        )

    if config.install_mode == "keep":
        body = _strip_tuna_examples(existing_body) if config.strip_example_patches else existing_body.strip()
        return _apply_install_extras(body, config)

    extra_cmds = _collect_install_snippets(config, "")
    parts: list[str] = []
    if config.install_mode == "dump_write":
        parts.append("dump_boot;")
        if extra_cmds:
            parts.append("")
            parts.append(extra_cmds)
        parts.append("")
        parts.append("write_boot;")
    elif config.install_mode == "split_flash":
        parts.append("split_boot;")
        if extra_cmds:
            parts.append("")
            parts.append(extra_cmds)
        parts.append("")
        parts.append("flash_boot;")

        # Flash generic images when present in staging or explicitly requested
        generic_targets = list(config.flash_generic_partitions)
        if staging_dir is not None:
            for generic_name in ("vendor_dlkm", "system_dlkm", "dtbo"):
                if generic_name not in generic_targets and (
                    (staging_dir / generic_name).is_file()
                    or (staging_dir / f"{generic_name}.img").is_file()
                ):
                    generic_targets.append(generic_name)
        for generic_name in generic_targets:
            parts.append(f"flash_generic {generic_name};")

    return "\n".join(parts)


def _render_extra_partition(block: PartitionBlock) -> str:
    lines: list[str] = [f"## {block.name} install"]
    if block.attributes_body:
        lines.extend([
            f"{block.name}_attributes() {{",
            block.attributes_body.strip(),
            "} # end attributes",
            "",
        ])
    elif block.install_mode == "dump_write":
        lines.extend([
            f"{block.name}_attributes() {{",
            "set_perm_recursive 0 0 755 644 $RAMDISK/*;",
            "set_perm_recursive 0 0 750 750 $RAMDISK/init* $RAMDISK/sbin;",
            "} # end attributes",
            "",
        ])

    lines.extend([
        f"# {block.name} shell variables",
        _format_shell_assignment("BLOCK", block.block),
        _format_shell_assignment("IS_SLOT_DEVICE", block.is_slot_device),
        _format_shell_assignment("RAMDISK_COMPRESSION", block.ramdisk_compression),
        _format_shell_assignment("PATCH_VBMETA_FLAG", block.patch_vbmeta_flag),
        "",
        "reset_ak keep;" if block.keep_on_reset else "reset_ak;",
        "",
        f"# {block.name} install",
        "dump_boot;" if block.install_mode == "dump_write" else "split_boot;",
    ])
    if block.commands.strip():
        lines.extend(["", block.commands.strip()])
    lines.extend([
        "",
        "write_boot;" if block.install_mode == "dump_write" else "flash_boot;",
        f"## end {block.name} install",
    ])
    return "\n".join(lines)


def _validate_anykernel_content(script_path: Path, config: AnyKernelConfig) -> dict[str, str]:
    text = script_path.read_text(encoding="utf-8")
    props = parse_properties_block(text)

    if not _AK3_CORE_LINE_RE.search(text):
        raise ValueError("anykernel.sh is missing required `. tools/ak3-core.sh;` import.")

    do_devicecheck = props.get("do.devicecheck", "0")
    devices = [
        val for key, val in props.items()
        if _DEVICE_NAME_KEY_RE.match(key) and val.strip()
    ]
    if do_devicecheck == "1":
        if not devices:
            raise ValueError("do.devicecheck=1 requires at least device.name1 to be non-empty.")
        if set(devices).issubset(UPSTREAM_DEFAULT_DEVICES) and config.device_names is None:
            msg = (
                "do.devicecheck=1 is still using upstream Galaxy Nexus device names (maguro/toro/tuna); "
                "set 'device-names' or set 'do-devicecheck: 0'."
            )
            if config.strict:
                raise ValueError(msg)
            warn(msg)

    for level_key in ("supported.patchlevels", "supported.vendorpatchlevels"):
        level_val = props.get(level_key, "").strip()
        if level_val and not _PATCHLEVEL_RE.match(level_val):
            raise ValueError(
                f"Invalid {level_key} format {level_val!r}; expected 'YYYY-MM', 'YYYY-MM -', "
                "'- YYYY-MM', or 'YYYY-MM - YYYY-MM'."
            )

    # Validate shell variables in primary boot block
    reset_match = re.search(r"^\s*reset_ak\b", text, re.MULTILINE)
    primary_section = text[: reset_match.start()] if reset_match else text

    block_match = re.search(r"^BLOCK=([^;\n]+);", primary_section, re.MULTILINE)
    if not block_match:
        raise ValueError(
            "anykernel.sh is missing a primary `BLOCK=<partition>;` variable. "
            "Specify 'block' pointing to a proper boot partition path (e.g. '/dev/block/bootdevice/by-name/boot')."
        )

    block_val = block_match.group(1).strip().strip('"').strip("'")
    block_target = block_val.split()[0] if block_val else ""
    if not block_target:
        raise ValueError(
            "BLOCK cannot be empty; specify 'block' pointing to a proper boot partition path "
            "(e.g. '/dev/block/bootdevice/by-name/boot')."
        )
    if block_target == UPSTREAM_DEFAULT_BLOCK and config.block is None and config.custom_script_path is None:
        raise ValueError(
            f"BLOCK is still set to the upstream Galaxy Nexus placeholder ({UPSTREAM_DEFAULT_BLOCK}); "
            "specifying 'block' pointing to a proper boot partition path is required "
            "(e.g. '/dev/block/bootdevice/by-name/boot' or '/dev/block/platform/<soc>/by-name/boot')."
        )
    if not block_target.startswith("/dev/") and block_target != "auto":
        msg = (
            f"BLOCK={block_target!r} is a short partition name rather than a full block device path; "
            "specify the full partition path (e.g. '/dev/block/bootdevice/by-name/boot', "
            "'/dev/block/by-name/boot', or '/dev/block/platform/<soc>/by-name/boot') or 'auto'."
        )
        if config.strict:
            raise ValueError(msg)
        warn(msg)

    slot_match = re.search(r"^IS_SLOT_DEVICE=([^;\n]+);", primary_section, re.MULTILINE)
    is_slot_val = slot_match.group(1).strip().strip('"') if slot_match else "0"
    if config.is_slot_device is None and config.custom_script_path is None:
        warn(
            "is-slot-device was not specified; using upstream IS_SLOT_DEVICE=0 (non-A/B). "
            "Set is-slot-device to '1' for A/B devices (boot_a/boot_b), '0' for non-A/B, or 'auto'."
        )

    if block_target.startswith("/dev/") and block_target.endswith(("_a", "_b")):
        base_block = block_target[:-2]
        if is_slot_val in {"1", "auto"}:
            raise ValueError(
                f"BLOCK={block_target!r} includes slot suffix while IS_SLOT_DEVICE={is_slot_val}; "
                f"specify the base partition path without '_a'/'_b' (e.g. '{base_block}') so AnyKernel3 "
                "resolves boot_a/boot_b dynamically."
            )
        warn(
            f"BLOCK={block_target!r} targets a hardcoded slot partition with IS_SLOT_DEVICE=0; "
            f"prefer base path '{base_block}' with IS_SLOT_DEVICE=1 (or 'auto') for boot_a/boot_b devices."
        )

    for var_name, allowed in (
        ("IS_SLOT_DEVICE", VALID_IS_SLOT),
        ("RAMDISK_COMPRESSION", VALID_RAMDISK_COMPRESSIONS),
        ("PATCH_VBMETA_FLAG", VALID_PATCH_VBMETA),
        ("SLOT_SELECT", VALID_SLOT_SELECT),
    ):
        for match in re.finditer(rf"^{var_name}=([^;\n]+);", text, re.MULTILINE):
            val = match.group(1).strip().strip('"')
            if val not in allowed:
                raise ValueError(
                    f"Invalid {var_name}={val!r}; allowed values: {', '.join(sorted(allowed))}"
                )

    sh_bin = shutil.which("sh")
    if sh_bin:
        proc = subprocess.run([sh_bin, "-n", str(script_path)], capture_output=True, text=True, check=False)
        if proc.returncode != 0:
            raise ValueError(f"anykernel.sh failed POSIX shell syntax check (`sh -n`): {proc.stderr.strip()}")

    return props


def configure_anykernel_script(
    staging_dir: Path,
    config: AnyKernelConfig,
) -> dict[str, str]:
    """Apply properties, shell variables, and install rules to staging_dir/anykernel.sh."""
    staging_dir = staging_dir.resolve()
    script_path = staging_dir / "anykernel.sh"

    if config.custom_script_path is not None:
        src = config.custom_script_path.resolve()
        if not src.is_file():
            raise FileNotFoundError(f"Custom anykernel-script not found: {src}")
        shutil.copy2(src, script_path)
        info(f"Copied custom anykernel.sh from {src}")

    if not script_path.is_file():
        raise FileNotFoundError(f"anykernel.sh not found in staging directory: {script_path}")

    text = script_path.read_text(encoding="utf-8").replace("\r\n", "\n")

    # 1. Update properties() block
    existing_props = parse_properties_block(text)
    rendered_props = _render_properties(existing_props, config)
    text = _PROPERTIES_BLOCK_RE.sub(
        lambda m: f"{m.group(1)}{rendered_props}{m.group(3)}",
        text,
        count=1,
    )

    # 2. Update boot_attributes() if requested
    if config.boot_attributes_body is not None:
        attr_body = config.boot_attributes_body.strip()
        if _BOOT_ATTR_BLOCK_RE.search(text):
            text = _BOOT_ATTR_BLOCK_RE.sub(
                lambda m: f"{m.group(1)}{attr_body}{m.group(3)}",
                text,
                count=1,
            )

    # 3. Update primary boot shell variables
    text = _update_boot_shell_variables(text, config)

    # 4. Update boot install body
    install_match = _BOOT_INSTALL_BLOCK_RE.search(text)
    if install_match:
        new_body = _build_boot_install_body(install_match.group(2), config, staging_dir)
        text = (
            text[: install_match.start()]
            + f"{install_match.group(1)}{new_body}{install_match.group(3)}"
            + text[install_match.end() :]
        )
    elif config.deepsleep_fix or (config.ramdisk_patch_commands and config.ramdisk_patch_commands.strip()):
        text = _apply_install_extras(text.rstrip(), config) + "\n"

    # 5. Append extra partition blocks if configured
    if config.extra_partitions:
        text = _COMMENTED_PARTITIONS_RE.sub("", text).rstrip() + "\n\n"
        rendered_blocks = "\n\n".join(_render_extra_partition(b) for b in config.extra_partitions)
        text += rendered_blocks + "\n"

    if not text.endswith("\n"):
        text += "\n"
    script_path.write_text(text, encoding="utf-8", newline="\n")

    final_props = _validate_anykernel_content(script_path, config)

    # Persist summary info for packaging/reporting
    block_match = re.search(r"^BLOCK=([^;\n]+);", text, re.MULTILINE)
    is_slot_match = re.search(r"^IS_SLOT_DEVICE=([^;\n]+);", text, re.MULTILINE)
    comp_match = re.search(r"^RAMDISK_COMPRESSION=([^;\n]+);", text, re.MULTILINE)
    vbmeta_match = re.search(r"^PATCH_VBMETA_FLAG=([^;\n]+);", text, re.MULTILINE)
    devices = [
        val for k, val in final_props.items()
        if _DEVICE_NAME_KEY_RE.match(k) and val.strip()
    ]
    state = load_staging_state(staging_dir)
    save_staging_state(
        staging_dir,
        {
            "kernel_string": final_props.get("kernel.string", ""),
            "do_devicecheck": final_props.get("do.devicecheck", ""),
            "do_modules": final_props.get("do.modules", ""),
            "do_systemless": final_props.get("do.systemless", ""),
            "device_names": devices,
            "block": block_match.group(1).strip().strip('"') if block_match else "",
            "is_slot_device": is_slot_match.group(1).strip() if is_slot_match else "",
            "ramdisk_compression": comp_match.group(1).strip() if comp_match else "",
            "patch_vbmeta_flag": vbmeta_match.group(1).strip() if vbmeta_match else "",
            "install_mode": config.install_mode,
            "deepsleep_fix": config.deepsleep_fix or (DEEPSLEEP_FIX_MARKER in text),
            "ak_base_version": state.get("ak_base_version", ""),
        },
    )

    info(
        f"Configured {script_path} (kernel.string={final_props.get('kernel.string', '')!r}, "
        f"do.devicecheck={final_props.get('do.devicecheck')}, devices={devices}, "
        f"do.modules={final_props.get('do.modules')}, install_mode={config.install_mode})"
    )
    return final_props
