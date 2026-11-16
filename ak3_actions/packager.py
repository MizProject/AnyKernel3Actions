"""Component 4: Package staged AnyKernel3 tree into a flashable zip archive."""

from __future__ import annotations

import hashlib
import os
import re
import stat
import zipfile
from dataclasses import dataclass
from pathlib import Path

from .github_env import (
    append_step_summary,
    build_template_context,
    info,
    load_staging_state,
    resolve_template,
)

_EXCLUDED_DIR_NAMES = frozenset({".git", ".github"})
_EXCLUDED_FILE_NAMES = frozenset({".gitignore", ".gitattributes", ".gitmodules", "placeholder"})


@dataclass
class PackageResult:
    """Result from packaging an AnyKernel3 staging directory."""

    zip_path: Path
    zip_name: str
    size_bytes: int
    sha256: str
    md5: str
    file_count: int
    checksum_path: Path | None = None


def _sanitize_filename(name: str) -> str:
    # Collapse repeated separators left by empty template placeholders (e.g. `AK3--20261117.zip`)
    stem = name[:-4] if name.lower().endswith(".zip") else name
    stem = stem.replace("/", "-").replace("\\", "-").replace("\0", "")
    stem = re.sub(r"-{2,}", "-", stem)
    stem = re.sub(r"_{2,}", "_", stem)
    stem = stem.strip("-_. ")
    if not stem:
        stem = "UPDATE-AnyKernel3"
    return f"{stem}.zip"


def resolve_zip_name(
    staging_dir: Path,
    *,
    zip_name: str | None = None,
    workspace: Path | None = None,
) -> str:
    """Resolve output zip filename using template variables from staging state."""
    state = load_staging_state(staging_dir)
    devices = state.get("device_names") or []
    ctx = build_template_context(
        workspace=workspace,
        kernel_version=str(state.get("kernel_version", "")),
        kernel_image=str(state.get("kernel_image", "")),
        kernel_string=str(state.get("kernel_string", "")),
        device_names=list(devices) if isinstance(devices, list) else [],
        ak_base_version=str(state.get("ak_base_version", "")),
    )
    if zip_name and zip_name.strip():
        rendered = resolve_template(zip_name.strip(), ctx)
    else:
        parts = ["AnyKernel3"]
        if ctx["device"]:
            parts.append(ctx["device"])
        if ctx["kernel_version"]:
            parts.append(ctx["kernel_version"])
        parts.append(ctx["date"])
        rendered = "-".join(parts)
    return _sanitize_filename(rendered)


def _should_include(rel_path: Path, *, include_readme: bool = False) -> bool:
    for part in rel_path.parts[:-1]:
        if part in _EXCLUDED_DIR_NAMES:
            return False
    fname = rel_path.name
    if fname in _EXCLUDED_DIR_NAMES or fname in _EXCLUDED_FILE_NAMES:
        return False
    if not include_readme and fname.lower() == "readme.md" and len(rel_path.parts) == 1:
        return False
    return True


def _zip_info_for_path(file_path: Path, arcname: str) -> tuple[zipfile.ZipInfo, bytes | None]:
    if file_path.is_symlink():
        target = os.readlink(file_path)
        zinfo = zipfile.ZipInfo(arcname)
        zinfo.create_system = 3
        zinfo.external_attr = ((stat.S_IFLNK | 0o777) & 0xFFFF) << 16
        return zinfo, target.encode("utf-8")

    zinfo = zipfile.ZipInfo.from_file(file_path, arcname=arcname)
    zinfo.create_system = 3
    st_mode = file_path.stat().st_mode
    if arcname == "META-INF/com/google/android/update-binary" or (
        arcname.startswith("tools/") and arcname != "tools/ak3-core.sh"
    ):
        st_mode = stat.S_IFREG | 0o755
    else:
        st_mode = stat.S_IFREG | stat.S_IMODE(st_mode)
    zinfo.external_attr = (st_mode & 0xFFFF) << 16
    return zinfo, None


def _compute_hashes(path: Path) -> tuple[str, str]:
    sha256_h = hashlib.sha256()
    md5_h = hashlib.md5()
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(1024 * 1024)
            if not chunk:
                break
            sha256_h.update(chunk)
            md5_h.update(chunk)
    return sha256_h.hexdigest(), md5_h.hexdigest()


def _format_size(num_bytes: int) -> str:
    size = float(num_bytes)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024.0 or unit == "GiB":
            return f"{int(size)} B" if unit == "B" else f"{size:.2f} {unit}"
        size /= 1024.0
    return f"{num_bytes} B"


def write_package_summary(staging_dir: Path, pkg: PackageResult) -> None:
    """Append a concise AnyKernel3 build summary to GITHUB_STEP_SUMMARY."""
    state = load_staging_state(staging_dir)
    devices = state.get("device_names") or []
    devices_str = ", ".join(devices) if devices else "(none)"
    staged_images = state.get("staged_images") or []
    images_str = ", ".join(staged_images) if staged_images else "(none)"

    disc_from = state.get("discovered_from") or "manual"
    disc_label = f"`{disc_from}` (Beta autodetect)" if disc_from != "manual" else "`manual`"
    conf = state.get("kernel_confidence") or ""
    arch = state.get("kernel_arch") or ""
    verify_str = f"`{conf}` ({arch or 'unknown'})" if conf else "`unchecked`"

    lines = [
        "### AnyKernel3 Package Summary",
        "",
        "| Field | Value |",
        "| --- | --- |",
        f"| Zip | `{pkg.zip_name}` ({_format_size(pkg.size_bytes)}) |",
        f"| SHA-256 | `{pkg.sha256}` |",
        f"| Kernel String | `{state.get('kernel_string', '')}` |",
        f"| Kernel Version | `{state.get('kernel_version', '') or 'unknown'}` |",
        f"| Discovery / Verify | {disc_label} / {verify_str} |",
        f"| Staged Images | `{images_str}` |",
        f"| Modules | `{state.get('modules_count', 0)}` (do.modules=`{state.get('do_modules', '')}`) |",
        f"| Devices | `{devices_str}` (do.devicecheck=`{state.get('do_devicecheck', '')}`) |",
        f"| Block / Slot | `{state.get('block', '')}` / slot=`{state.get('is_slot_device', '')}` |",
        f"| Compression / Mode | `{state.get('ramdisk_compression', '')}` / `{state.get('install_mode', '')}` |",
    ]
    append_step_summary("\n".join(lines) + "\n")


def package_anykernel_zip(
    staging_dir: Path,
    *,
    output_dir: Path | None = None,
    zip_name: str | None = None,
    compression_level: int = 9,
    include_readme: bool = False,
    write_checksum_file: bool = False,
    workspace: Path | None = None,
) -> PackageResult:
    """Create a flashable AnyKernel3 zip archive excluding git metadata and placeholders."""
    staging_dir = staging_dir.resolve()
    if not staging_dir.is_dir():
        raise FileNotFoundError(f"Staging directory does not exist: {staging_dir}")
    if not (staging_dir / "anykernel.sh").is_file():
        raise FileNotFoundError(f"Missing anykernel.sh in staging directory: {staging_dir}")

    if not 0 <= compression_level <= 9:
        raise ValueError(f"compression-level must be between 0 and 9, got {compression_level}")

    resolved_name = resolve_zip_name(staging_dir, zip_name=zip_name, workspace=workspace)
    dest_dir = (output_dir.resolve() if output_dir else Path.cwd().resolve())
    dest_dir.mkdir(parents=True, exist_ok=True)
    zip_path = (dest_dir / resolved_name).resolve()

    collected: list[tuple[Path, str]] = []
    for root, dirs, files in os.walk(staging_dir, topdown=True, followlinks=False):
        root_path = Path(root)
        dirs[:] = sorted(d for d in dirs if d not in _EXCLUDED_DIR_NAMES)
        for fname in sorted(files):
            fpath = root_path / fname
            if fpath.resolve() == zip_path:
                continue
            rel = fpath.relative_to(staging_dir)
            if _should_include(rel, include_readme=include_readme):
                collected.append((fpath, rel.as_posix()))

    if not collected:
        raise RuntimeError(f"No files left to package in staging directory: {staging_dir}")

    compression = zipfile.ZIP_STORED if compression_level == 0 else zipfile.ZIP_DEFLATED
    with zipfile.ZipFile(
        zip_path,
        mode="w",
        compression=compression,
        compresslevel=compression_level if compression == zipfile.ZIP_DEFLATED else None,
    ) as zf:
        for fpath, arcname in collected:
            zinfo, symlink_payload = _zip_info_for_path(fpath, arcname)
            if symlink_payload is not None:
                zf.writestr(zinfo, symlink_payload)
            else:
                with fpath.open("rb") as src_fh:
                    zf.writestr(zinfo, src_fh.read())

    sha256, md5 = _compute_hashes(zip_path)
    checksum_path: Path | None = None
    if write_checksum_file:
        checksum_path = zip_path.with_name(f"{zip_path.name}.sha256")
        checksum_path.write_text(f"{sha256}  {zip_path.name}\n", encoding="utf-8")

    result = PackageResult(
        zip_path=zip_path,
        zip_name=zip_path.name,
        size_bytes=zip_path.stat().st_size,
        sha256=sha256,
        md5=md5,
        file_count=len(collected),
        checksum_path=checksum_path,
    )
    write_package_summary(staging_dir, result)
    info(
        f"Packaged {result.zip_path} ({_format_size(result.size_bytes)}, "
        f"{result.file_count} files, sha256={result.sha256})"
    )
    return result

