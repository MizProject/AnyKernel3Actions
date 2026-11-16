"""Component 2: Discover and process compiled kernel artifacts into AnyKernel3 staging."""

from __future__ import annotations

import bz2
import glob
import lzma
import os
import re
import shutil
import struct
import subprocess
import zlib
from dataclasses import dataclass, field
from pathlib import Path

from .github_env import (
    build_template_context,
    info,
    load_staging_state,
    resolve_template,
    save_staging_state,
    warn,
)

AK3_SUPPORTED_KERNEL_NAMES: tuple[str, ...] = (
    "zImage",
    "zImage-dtb",
    "Image",
    "Image-dtb",
    "Image.gz",
    "Image.gz-dtb",
    "Image.bz2",
    "Image.bz2-dtb",
    "Image.lzo",
    "Image.lzo-dtb",
    "Image.lzma",
    "Image.lzma-dtb",
    "Image.xz",
    "Image.xz-dtb",
    "Image.lz4",
    "Image.lz4-dtb",
    "Image.fit",
)

KERNEL_DISCOVERY_PRIORITY: tuple[str, ...] = (
    "Image.gz-dtb",
    "Image.lz4-dtb",
    "Image.xz-dtb",
    "Image.bz2-dtb",
    "Image.lzma-dtb",
    "Image.lzo-dtb",
    "zImage-dtb",
    "Image-dtb",
    "Image.gz",
    "Image.lz4",
    "Image.xz",
    "Image.bz2",
    "Image.lzma",
    "Image.lzo",
    "Image.fit",
    "zImage",
    "Image",
)

_COMMON_BOOT_SUBDIRS: tuple[str, ...] = (
    ".",
    "arch/arm64/boot",
    "arch/arm/boot",
    "arch/x86/boot",
    "arch/riscv/boot",
)

_MODULE_META_FILES = frozenset({
    "modules.load",
    "modules.dep",
    "modules.alias",
    "modules.softdep",
    "modules.order",
    "modules.builtin",
    "modules.blocklist",
})

_SIMPLE_EXTRA_IMAGES: dict[str, tuple[str, ...]] = {
    "dt": ("dt", "dt.img"),
    "recovery_dtbo": ("recovery_dtbo", "recovery_dtbo.img"),
    "dtbo": ("dtbo", "dtbo.img"),
    "system_dlkm": ("system_dlkm", "system_dlkm.img"),
    "vendor_dlkm": ("vendor_dlkm", "vendor_dlkm.img"),
}

_LINUX_BANNER_RE = re.compile(rb"Linux version ([0-9]+\.[0-9]+[^\x00\r\n]{0,200})")
_UTS_RELEASE_RE = re.compile(r'#define\s+UTS_RELEASE\s+"([^"]+)"')
_FDT_MAGIC = b"\xd0\x0d\xfe\xed"
_GZIP_MAGIC = b"\x1f\x8b\x08"
_XZ_MAGIC = b"\xfd7zXZ\x00"
_BZ2_MAGIC = b"BZh"
_LZ4_LEGACY_MAGIC = b"\x02\x21\x4c\x18"
_LZ4_FRAME_MAGIC = b"\x04\x22\x4d\x18"
_LZO_MAGIC = b"\x89LZO\x00\r\n\x1a\n"

_NON_KERNEL_PREFIXES: tuple[tuple[bytes, str], ...] = (
    (b"\x7fELF", "ELF binary (expected compiled boot Image/zImage, not userland ELF/vmlinux)"),
    (b"\x89PNG\r\n\x1a\n", "PNG image"),
    (b"GIF8", "GIF image"),
    (b"\xff\xd8\xff", "JPEG image"),
    (b"%PDF-", "PDF document"),
    (b"PK\x03\x04", "ZIP archive"),
    (b"#!/", "shell/script file"),
    (b"<!DOCTYPE", "HTML document"),
    (b"<html", "HTML document"),
)

_KERNEL_STRING_MARKERS: tuple[bytes, ...] = (
    b"Kernel command line:",
    b"Kernel panic - not syncing",
    b"Booting Linux on physical CPU",
    b"Calibrating delay loop",
    b"Freeing unused kernel",
    b"Virtual kernel memory layout:",
    b"Memory policy:",
    b"Machine model:",
    b"Unable to handle kernel",
    b"Internal error: Oops",
    b"earlycon",
    b"kallsyms",
    b"kthreadd",
    b"initcall",
)


@dataclass
class KernelVerificationResult:
    """Result of validating binary magic and embedded strings in a kernel image."""

    path: Path
    valid: bool
    arch_hint: str = ""
    compression: str = ""
    has_dtb_appended: bool = False
    kernel_version: str = ""
    kernel_banner: str = ""
    matched_strings: list[str] = field(default_factory=list)
    header_magic: str = ""
    confidence: str = "none"
    reason: str = ""


@dataclass
class KernelProcessResult:
    """Summary of compiled kernel artifacts staged into AnyKernel3."""

    staging_dir: Path
    kernel_image: str = ""
    kernel_source: str = ""
    kernel_version: str = ""
    kernel_banner: str = ""
    verification: KernelVerificationResult | None = None
    staged_images: list[str] = field(default_factory=list)
    modules_count: int = 0
    staged_overlays: list[str] = field(default_factory=list)


def _expand_glob_or_path(raw: str | Path) -> list[Path]:
    text = str(raw).strip()
    if any(ch in text for ch in ("*", "?", "[")):
        return sorted(Path(p) for p in glob.glob(text, recursive=True))
    return [Path(text)]


def _extract_banner_from_bytes(data: bytes) -> tuple[str, str]:
    match = _LINUX_BANNER_RE.search(data)
    if not match:
        return "", ""
    banner = match.group(0).decode("utf-8", errors="replace").strip()
    ver_part = match.group(1).decode("utf-8", errors="replace").strip()
    short_ver = ver_part.split()[0] if ver_part else ""
    return short_ver, banner


def _decompress_lz4_block(block: bytes, max_output: int = 16 * 1024 * 1024) -> bytes:
    """Decompress a single raw LZ4 block cleanly in pure Python."""
    out = bytearray()
    pos = 0
    length = len(block)

    while pos < length and len(out) < max_output:
        token = block[pos]
        pos += 1

        lit_len = token >> 4
        if lit_len == 15:
            while pos < length:
                extra = block[pos]
                pos += 1
                lit_len += extra
                if extra != 255:
                    break

        if pos + lit_len > length:
            raise ValueError("Truncated LZ4 literal run")
        out.extend(block[pos : pos + lit_len])
        pos += lit_len

        if pos >= length:
            break

        if pos + 2 > length:
            raise ValueError("Truncated LZ4 match offset")
        offset = struct.unpack_from("<H", block, pos)[0]
        pos += 2
        if offset == 0 or offset > len(out):
            raise ValueError("Invalid LZ4 match offset")

        match_len = (token & 0x0F) + 4
        if (token & 0x0F) == 15:
            while pos < length:
                extra = block[pos]
                pos += 1
                match_len += extra
                if extra != 255:
                    break

        for _ in range(match_len):
            if len(out) >= max_output:
                break
            out.append(out[-offset])

    return bytes(out)


def _decompress_lz4_stream(raw: bytes, max_output: int = 16 * 1024 * 1024) -> bytes:
    """Decompress LZ4 legacy or standard frame payloads in pure Python."""
    if raw.startswith(_LZ4_LEGACY_MAGIC):
        pos = 4
        out = bytearray()
        while pos + 4 <= len(raw) and len(out) < max_output:
            block_size = struct.unpack_from("<I", raw, pos)[0]
            pos += 4
            if block_size == 0 or block_size > 8 * 1024 * 1024 or pos + block_size > len(raw):
                break
            out.extend(_decompress_lz4_block(raw[pos : pos + block_size], max_output - len(out)))
            pos += block_size
        return bytes(out)

    if raw.startswith(_LZ4_FRAME_MAGIC):
        if len(raw) < 7:
            return b""
        flg = raw[4]
        pos = 6  # after magic(4) + FLG(1) + BD(1)
        if flg & 0x08:  # content size
            pos += 8
        if flg & 0x01:  # dictionary id
            pos += 4
        pos += 1  # header checksum
        block_checksum = bool(flg & 0x10)
        out = bytearray()
        while pos + 4 <= len(raw) and len(out) < max_output:
            raw_size = struct.unpack_from("<I", raw, pos)[0]
            pos += 4
            if raw_size == 0:
                break
            uncompressed = bool(raw_size & 0x80000000)
            block_size = raw_size & 0x7FFFFFFF
            if block_size > 16 * 1024 * 1024 or pos + block_size > len(raw):
                break
            chunk = raw[pos : pos + block_size]
            pos += block_size
            if block_checksum:
                pos += 4
            if uncompressed:
                out.extend(chunk[: max_output - len(out)])
            else:
                out.extend(_decompress_lz4_block(chunk, max_output - len(out)))
        return bytes(out)

    return b""


def _extract_inner_payload(
    raw: bytes,
    filename: str = "",
    file_path: Path | None = None,
) -> tuple[bytes, str, bool]:
    """Return `(payload_bytes, compression_label, decompression_attempted_ok)`."""
    # Check container magic streams cleanly (supports piggybacked streams in zImage and *-dtb images)
    gz_idx = raw.find(_GZIP_MAGIC)
    if gz_idx != -1:
        search_start = gz_idx
        while search_start != -1:
            try:
                decomp = zlib.decompressobj(16 + zlib.MAX_WBITS)
                chunk = decomp.decompress(raw[search_start:], 16 * 1024 * 1024)
                if chunk:
                    return chunk, "gzip", True
            except zlib.error:
                pass
            search_start = raw.find(_GZIP_MAGIC, search_start + 1)

    xz_idx = raw.find(_XZ_MAGIC)
    if xz_idx != -1 or filename.endswith((".xz", ".xz-dtb", ".lzma", ".lzma-dtb")):
        candidates = [raw[xz_idx:]] if xz_idx != -1 else [raw]
        for cand in candidates:
            try:
                chunk = lzma.LZMADecompressor().decompress(cand, max_length=16 * 1024 * 1024)
                if chunk:
                    return chunk, "xz", True
            except lzma.LZMAError:
                pass

    bz2_idx = raw.find(_BZ2_MAGIC)
    if bz2_idx != -1:
        try:
            chunk = bz2.BZ2Decompressor().decompress(raw[bz2_idx:], max_length=16 * 1024 * 1024)
            if chunk:
                return chunk, "bz2", True
        except OSError:
            pass

    for magic in (_LZ4_LEGACY_MAGIC, _LZ4_FRAME_MAGIC):
        lz4_idx = raw.find(magic)
        if lz4_idx != -1:
            try:
                chunk = _decompress_lz4_stream(raw[lz4_idx:])
                if chunk:
                    return chunk, "lz4", True
            except ValueError:
                pass
            if file_path is not None and shutil.which("lz4"):
                proc = subprocess.run(["lz4", "-dc", str(file_path)], capture_output=True, check=False)
                if proc.stdout:
                    return proc.stdout[: 16 * 1024 * 1024], "lz4", True

    if (raw.startswith(_LZO_MAGIC) or ".lzo" in filename) and file_path is not None and shutil.which("lzop"):
        proc = subprocess.run(["lzop", "-dc", str(file_path)], capture_output=True, check=False)
        if proc.stdout:
            return proc.stdout[: 16 * 1024 * 1024], "lzo", True

    if filename.endswith((".gz", ".gz-dtb", ".xz", ".xz-dtb", ".bz2", ".bz2-dtb", ".lzma", ".lzma-dtb", ".lz4", ".lz4-dtb")):
        return raw, "corrupt-or-unknown", False

    return raw, "raw", True


def _detect_header_magic(raw: bytes, inner: bytes) -> tuple[str, str]:
    """Identify binary Linux boot image header magic and architecture hint."""
    if len(raw) >= 48 and raw[0x24:0x28] in (b"\x18\x28\x6f\x01", b"\x01\x28\x6f\x18"):
        return "ARM_ZIMAGE", "arm"
    if len(raw) >= 520 and raw[0x202:0x206] == b"HdrS":
        return "X86_BZIMAGE", "x86"
    if raw.startswith(_FDT_MAGIC):
        return "FIT_IMAGE", "fit"
    for payload in (inner, raw):
        if len(payload) >= 64 and payload[0x38:0x3C] == b"ARM\x64":
            return "ARM64_IMAGE", "arm64"
        if len(payload) >= 64 and (payload[0x30:0x36] == b"RISCV\x00" or payload[0x38:0x3C] == b"RSC\x05"):
            return "RISCV_IMAGE", "riscv"
    return "", ""


def inspect_kernel_binary(path: Path) -> KernelVerificationResult:
    """Inspect binary magic and embedded Linux strings to verify a real kernel image."""
    resolved = path.resolve()
    if not resolved.is_file():
        return KernelVerificationResult(path=resolved, valid=False, reason="File does not exist")

    size = resolved.stat().st_size
    if size < 64:
        return KernelVerificationResult(
            path=resolved,
            valid=False,
            reason=f"File too small ({size} bytes) to be a compiled kernel image",
        )

    # Read up to 64 MiB for kernel image inspection cleanly
    with resolved.open("rb") as fh:
        raw = fh.read(64 * 1024 * 1024)

    for prefix, desc in _NON_KERNEL_PREFIXES:
        if raw.startswith(prefix):
            return KernelVerificationResult(
                path=resolved,
                valid=False,
                reason=f"File appears to be a {desc}, not an AnyKernel3 boot Image",
            )

    inner, compression, decomp_ok = _extract_inner_payload(raw, resolved.name, resolved)
    if not decomp_ok:
        return KernelVerificationResult(
            path=resolved,
            valid=False,
            compression=compression,
            reason=f"Failed to decompress kernel container for {resolved.name}",
        )

    header_magic, arch_hint = _detect_header_magic(raw, inner)
    has_dtb = (
        resolved.name.endswith("-dtb") and _FDT_MAGIC in raw[64:]
    ) or (header_magic == "FIT_IMAGE")

    if compression == "raw" and not header_magic and b"\x00" not in raw[:4096]:
        try:
            raw[:4096].decode("utf-8")
            return KernelVerificationResult(
                path=resolved,
                valid=False,
                compression=compression,
                reason="File is plain text without binary kernel header or null-terminated sections",
            )
        except UnicodeDecodeError:
            pass

    ver, banner = _extract_banner_from_bytes(inner)
    if not ver and inner is not raw:
        ver, banner = _extract_banner_from_bytes(raw)

    matched_markers: list[str] = []
    scan_buf = inner if inner is not raw else raw
    for marker in _KERNEL_STRING_MARKERS:
        if marker in scan_buf:
            matched_markers.append(marker.decode("ascii", errors="ignore"))

    if header_magic and (banner or matched_markers):
        return KernelVerificationResult(
            path=resolved,
            valid=True,
            arch_hint=arch_hint,
            compression=compression,
            has_dtb_appended=has_dtb,
            kernel_version=ver,
            kernel_banner=banner,
            matched_strings=matched_markers,
            header_magic=header_magic,
            confidence="high",
            reason=f"Verified {header_magic} header and Linux kernel strings",
        )

    if banner and matched_markers:
        return KernelVerificationResult(
            path=resolved,
            valid=True,
            arch_hint=arch_hint,
            compression=compression,
            has_dtb_appended=has_dtb,
            kernel_version=ver,
            kernel_banner=banner,
            matched_strings=matched_markers,
            header_magic=header_magic,
            confidence="high",
            reason="Verified Linux version banner and multiple kernel .rodata strings",
        )

    if banner and (compression != "raw" or b"\x00" in raw):
        return KernelVerificationResult(
            path=resolved,
            valid=True,
            arch_hint=arch_hint,
            compression=compression,
            has_dtb_appended=has_dtb,
            kernel_version=ver,
            kernel_banner=banner,
            matched_strings=matched_markers,
            header_magic=header_magic,
            confidence="medium",
            reason="Verified binary payload with embedded Linux version banner",
        )

    if header_magic:
        return KernelVerificationResult(
            path=resolved,
            valid=False,
            arch_hint=arch_hint,
            compression=compression,
            has_dtb_appended=has_dtb,
            header_magic=header_magic,
            confidence="low",
            reason=(
                f"Matched {header_magic} magic bytes, but found no Linux version banner "
                "or kernel strings in payload"
            ),
        )

    return KernelVerificationResult(
        path=resolved,
        valid=False,
        compression=compression,
        reason="No Linux kernel binary header magic or embedded kernel strings found",
    )


def detect_kernel_version(
    kernel_image_path: Path | None = None,
    kernel_dir: Path | None = None,
) -> tuple[str, str]:
    """Detect Linux kernel version from build metadata files or kernel binary."""
    search_roots: list[Path] = []
    if kernel_dir is not None:
        search_roots.append(kernel_dir.resolve())
    if kernel_image_path is not None:
        resolved = kernel_image_path.resolve()
        search_roots.extend([
            resolved.parent,
            resolved.parent.parent,
            resolved.parent.parent.parent,
            resolved.parent.parent.parent.parent,
        ])

    seen: set[Path] = set()
    for root in search_roots:
        if root in seen or not root.is_dir():
            continue
        seen.add(root)
        release_file = root / "include" / "config" / "kernel.release"
        if release_file.is_file():
            rel = release_file.read_text(encoding="utf-8", errors="replace").strip()
            if rel:
                return rel, ""
        uts_file = root / "include" / "generated" / "utsrelease.h"
        if uts_file.is_file():
            match = _UTS_RELEASE_RE.search(uts_file.read_text(encoding="utf-8", errors="replace"))
            if match:
                return match.group(1).strip(), ""

    if kernel_image_path is None or not kernel_image_path.is_file():
        return "", ""

    raw = kernel_image_path.read_bytes()
    inner, _, _ = _extract_inner_payload(raw, kernel_image_path.name, kernel_image_path)
    ver, banner = _extract_banner_from_bytes(inner)
    if ver:
        return ver, banner
    if inner is not raw:
        return _extract_banner_from_bytes(raw)
    return "", ""


def resolve_kernel_image(
    *,
    kernel_image: str | Path | None = None,
    kernel_dir: str | Path | None = None,
    kernel_image_name: str | None = None,
) -> tuple[Path, str]:
    """Locate the compiled kernel image and determine its AnyKernel3 target filename."""
    candidates: list[Path] = []

    if kernel_image:
        matches = _expand_glob_or_path(kernel_image)
        existing = [p for p in matches if p.exists()]
        if not existing:
            raise FileNotFoundError(f"kernel-image not found: {kernel_image}")
        if len(existing) == 1 and existing[0].is_file():
            src = existing[0].resolve()
            dest_name = kernel_image_name or src.name
            if dest_name not in AK3_SUPPORTED_KERNEL_NAMES:
                warn(
                    f"Kernel filename {dest_name!r} is not in standard AnyKernel3 list "
                    f"({', '.join(AK3_SUPPORTED_KERNEL_NAMES)}); consider setting kernel-image-name."
                )
            return src, dest_name
        if len(existing) == 1 and existing[0].is_dir():
            candidates.append(existing[0].resolve())
        else:
            # Multiple glob matches -> rank by discovery priority
            by_name = {p.name: p.resolve() for p in existing if p.is_file()}
            for preferred in KERNEL_DISCOVERY_PRIORITY:
                if preferred in by_name:
                    return by_name[preferred], kernel_image_name or preferred
            first = sorted(p for p in existing if p.is_file())[0].resolve()
            return first, kernel_image_name or first.name

    if kernel_dir:
        base_dir = Path(kernel_dir).resolve()
        if not base_dir.is_dir():
            raise FileNotFoundError(f"kernel-dir does not exist or is not a directory: {base_dir}")
        candidates.append(base_dir)

    for base in candidates:
        for subdir in _COMMON_BOOT_SUBDIRS:
            search_dir = (base / subdir).resolve()
            if not search_dir.is_dir():
                continue
            for name in KERNEL_DISCOVERY_PRIORITY:
                candidate = search_dir / name
                if candidate.is_file() and candidate.stat().st_size > 0:
                    return candidate, kernel_image_name or name

    raise FileNotFoundError(
        "No compiled kernel image found. Specify 'kernel-image' or a 'kernel-dir' containing one of: "
        + ", ".join(KERNEL_DISCOVERY_PRIORITY)
    )


def _remove_existing_kernel_images(staging_dir: Path) -> None:
    for name in AK3_SUPPORTED_KERNEL_NAMES:
        target = staging_dir / name
        if target.is_file() or target.is_symlink():
            target.unlink()


def _stage_dtb(dtb_input: str | Path, staging_dir: Path) -> str:
    matches = _expand_glob_or_path(dtb_input)
    existing = [p for p in matches if p.exists()]
    if not existing:
        raise FileNotFoundError(f"dtb-path not found: {dtb_input}")

    if len(existing) == 1 and existing[0].is_file():
        src = existing[0].resolve()
        dest_name = "dtb.img" if src.name == "dtb.img" else "dtb"
        shutil.copy2(src, staging_dir / dest_name)
        return dest_name

    # Directory or glob of .dtb files
    if len(existing) == 1 and existing[0].is_dir():
        root = existing[0].resolve()
        for prebuilt in ("dtb", "dtb.img"):
            if (root / prebuilt).is_file():
                shutil.copy2(root / prebuilt, staging_dir / prebuilt)
                return prebuilt
        dtb_files = sorted(p for p in root.rglob("*.dtb") if p.is_file())
    else:
        dtb_files = sorted(p.resolve() for p in existing if p.is_file())

    if not dtb_files:
        raise FileNotFoundError(f"No dtb / dtb.img / *.dtb files found at: {dtb_input}")

    out_path = staging_dir / "dtb"
    with out_path.open("wb") as out_fh:
        for dtb_file in dtb_files:
            payload = dtb_file.read_bytes()
            if not payload.startswith(_FDT_MAGIC):
                warn(f"DTB file {dtb_file} does not begin with FDT magic 0xd00dfeed")
            out_fh.write(payload)
    info(f"Concatenated {len(dtb_files)} .dtb file(s) into {out_path.name}")
    return "dtb"


def _stage_simple_image(kind: str, raw_input: str | Path, staging_dir: Path) -> str:
    accepted_names = _SIMPLE_EXTRA_IMAGES[kind]
    matches = [p for p in _expand_glob_or_path(raw_input) if p.exists()]
    if not matches:
        raise FileNotFoundError(f"{kind}-path not found: {raw_input}")

    src: Path | None = None
    if len(matches) == 1 and matches[0].is_file():
        src = matches[0].resolve()
    elif len(matches) == 1 and matches[0].is_dir():
        root = matches[0].resolve()
        for candidate_name in accepted_names:
            candidate = root / candidate_name
            if candidate.is_file():
                src = candidate
                break
        if src is None and kind == "dtbo":
            dtbos = sorted(root.rglob("*.dtbo"))
            if dtbos:
                raise ValueError(
                    f"Found raw *.dtbo files in {root} without a packed dtbo.img; "
                    "pack them with mkdtboimg or pass dtbo.img directly."
                )

    if src is None:
        raise FileNotFoundError(f"Could not resolve {kind} image file from: {raw_input}")

    dest_name = src.name if src.name in accepted_names else accepted_names[0]
    shutil.copy2(src, staging_dir / dest_name)
    return dest_name


def _remove_placeholders(root: Path) -> None:
    if not root.is_dir():
        return
    for placeholder in root.rglob("placeholder"):
        if placeholder.is_file():
            placeholder.unlink()


def _strip_kernel_module(ko_path: Path) -> None:
    stripper = shutil.which("llvm-strip") or shutil.which("aarch64-linux-gnu-strip") or shutil.which("strip")
    if not stripper:
        warn(f"strip-modules requested but no strip tool found on PATH; skipping {ko_path.name}")
        return
    proc = subprocess.run([stripper, "--strip-debug", str(ko_path)], capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        warn(f"Failed to strip {ko_path.name} with {stripper}: {proc.stderr.strip()}")


def _stage_modules(
    modules_input: str | Path,
    staging_dir: Path,
    *,
    modules_dest: str = "system/lib/modules",
    preserve_tree: bool = False,
    include_metadata: bool = False,
    strip_modules: bool = False,
) -> int:
    matches = [p for p in _expand_glob_or_path(modules_input) if p.exists()]
    if not matches:
        raise FileNotFoundError(f"modules-path not found: {modules_input}")

    modules_root = staging_dir / "modules"
    _remove_placeholders(modules_root)
    # Remove default empty template dir if targeting a different partition (e.g. vendor/lib/modules)
    default_placeholder_dir = modules_root / "system" / "lib" / "modules"
    if default_placeholder_dir.is_dir() and not any(default_placeholder_dir.iterdir()):
        shutil.rmtree(modules_root / "system", ignore_errors=True)

    modules_root.mkdir(parents=True, exist_ok=True)
    clean_dest = modules_dest.strip("/")
    copied = 0

    # If a single directory already uses AnyKernel3 partition roots (system/, vendor/, product/, system_ext/)
    if len(matches) == 1 and matches[0].is_dir():
        src_dir = matches[0].resolve()
        partition_roots = [
            p for p in src_dir.iterdir()
            if p.is_dir() and p.name in {"system", "vendor", "product", "system_ext"}
        ]
        if partition_roots:
            for part_dir in partition_roots:
                dest_part = modules_root / part_dir.name
                shutil.copytree(
                    part_dir,
                    dest_part,
                    ignore=shutil.ignore_patterns(".git", "placeholder"),
                    dirs_exist_ok=True,
                )
            for ko in modules_root.rglob("*.ko"):
                copied += 1
                if strip_modules:
                    _strip_kernel_module(ko)
            return copied

    target_base = modules_root / clean_dest if clean_dest else modules_root
    target_base.mkdir(parents=True, exist_ok=True)

    files_to_copy: list[tuple[Path, Path]] = []
    for entry in matches:
        if entry.is_file():
            if entry.suffix == ".ko" or (include_metadata and entry.name in _MODULE_META_FILES):
                files_to_copy.append((entry.resolve(), target_base / entry.name))
        elif entry.is_dir():
            src_dir = entry.resolve()
            # Handle INSTALL_MOD_PATH layout (`lib/modules/<kver>/...`) cleanly
            lib_mod = src_dir / "lib" / "modules"
            walk_root = lib_mod if lib_mod.is_dir() else src_dir
            for root, _, files in os.walk(walk_root):
                root_path = Path(root)
                for fname in sorted(files):
                    fpath = root_path / fname
                    if fpath.suffix == ".ko" or (include_metadata and fname in _MODULE_META_FILES):
                        if preserve_tree:
                            rel = fpath.relative_to(walk_root)
                            # Strip leading <kver> component when copying from lib/modules/<kver>
                            if walk_root == lib_mod and len(rel.parts) > 1:
                                rel = Path(*rel.parts[1:])
                            dest_file = target_base / rel
                        else:
                            dest_file = target_base / fname
                        files_to_copy.append((fpath, dest_file))

    if not files_to_copy:
        raise FileNotFoundError(f"No .ko kernel modules found at: {modules_input}")

    for src_file, dest_file in files_to_copy:
        dest_file.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_file, dest_file)
        if dest_file.suffix == ".ko":
            copied += 1
            if strip_modules:
                _strip_kernel_module(dest_file)

    return copied


def _stage_overlay_dir(src_dir: Path, staging_dir: Path, target_name: str) -> str:
    resolved = src_dir.resolve()
    if not resolved.is_dir():
        raise FileNotFoundError(f"{target_name}-dir does not exist or is not a directory: {resolved}")
    dest_dir = staging_dir / target_name
    _remove_placeholders(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    shutil.copytree(
        resolved,
        dest_dir,
        symlinks=True,
        ignore=shutil.ignore_patterns(".git", ".github", "placeholder"),
        dirs_exist_ok=True,
    )
    return target_name


def _write_text_or_file(
    staging_dir: Path,
    target_name: str,
    *,
    inline_text: str | None = None,
    file_path: Path | None = None,
    template_context: dict[str, str] | None = None,
) -> bool:
    content: str | None = None
    if file_path is not None:
        resolved = file_path.resolve()
        if not resolved.is_file():
            raise FileNotFoundError(f"{target_name}-file not found: {resolved}")
        content = resolved.read_text(encoding="utf-8")
    elif inline_text is not None and inline_text != "":
        # Allow passing a file path in inline banner/version field if it's a single line matching an existing file
        if "\n" not in inline_text and Path(inline_text).is_file():
            content = Path(inline_text).read_text(encoding="utf-8")
        else:
            content = inline_text

    if content is None:
        return False

    if template_context:
        content = resolve_template(content, template_context)

    normalized = content.replace("\r\n", "\n")
    if normalized and not normalized.endswith("\n"):
        normalized += "\n"
    (staging_dir / target_name).write_text(normalized, encoding="utf-8")
    return True


def process_compiled_kernel(
    staging_dir: Path,
    *,
    kernel_image: str | Path | None = None,
    kernel_image_name: str | None = None,
    kernel_dir: str | Path | None = None,
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
    verify_kernel: bool = False,
) -> KernelProcessResult:
    """Copy compiled kernel products, DTBs, modules, and overlays into AnyKernel3 staging."""
    staging_dir = staging_dir.resolve()
    if not staging_dir.is_dir():
        raise FileNotFoundError(f"Staging directory does not exist: {staging_dir}")

    result = KernelProcessResult(staging_dir=staging_dir)

    if kernel_image or kernel_dir:
        src_kernel, dest_name = resolve_kernel_image(
            kernel_image=kernel_image,
            kernel_dir=kernel_dir,
            kernel_image_name=kernel_image_name,
        )
        verification = inspect_kernel_binary(src_kernel)
        if verify_kernel and not verification.valid:
            raise ValueError(
                f"Kernel image verification failed for {src_kernel}: {verification.reason}"
            )
        if not verification.valid:
            warn(f"Kernel verification note for {src_kernel.name}: {verification.reason}")

        _remove_existing_kernel_images(staging_dir)
        shutil.copy2(src_kernel, staging_dir / dest_name)
        result.kernel_image = dest_name
        result.kernel_source = str(src_kernel)
        result.verification = verification
        result.staged_images.append(dest_name)

        kver, kbanner = detect_kernel_version(
            src_kernel,
            Path(kernel_dir).resolve() if kernel_dir else None,
        )
        result.kernel_version = kver or verification.kernel_version
        result.kernel_banner = kbanner or verification.kernel_banner
        info(
            f"Staged kernel image {src_kernel} -> {dest_name}"
            + (f" (version={result.kernel_version})" if result.kernel_version else "")
            + (
                f" [verified: {verification.confidence}, arch={verification.arch_hint or 'unknown'}, "
                f"comp={verification.compression}]"
                if verification.valid
                else ""
            )
        )
    elif require_kernel:
        raise ValueError("Either 'kernel-image' or 'kernel-dir' must be provided.")
    else:
        # Check if staging_dir already contains a kernel image (e.g. from local anykernel-path)
        for name in KERNEL_DISCOVERY_PRIORITY:
            existing = staging_dir / name
            if existing.is_file():
                result.kernel_image = name
                result.verification = inspect_kernel_binary(existing)
                result.kernel_version, result.kernel_banner = detect_kernel_version(existing)
                if not result.kernel_version and result.verification.kernel_version:
                    result.kernel_version = result.verification.kernel_version
                    result.kernel_banner = result.verification.kernel_banner
                break

    if dtb_path:
        result.staged_images.append(_stage_dtb(dtb_path, staging_dir))

    extra_inputs: list[tuple[str, str | Path | None]] = [
        ("dt", dt_path),
        ("recovery_dtbo", recovery_dtbo_path),
        ("dtbo", dtbo_path),
        ("system_dlkm", system_dlkm_path),
        ("vendor_dlkm", vendor_dlkm_path),
    ]
    for kind, path_val in extra_inputs:
        if path_val:
            staged_name = _stage_simple_image(kind, path_val, staging_dir)
            result.staged_images.append(staged_name)
            info(f"Staged {kind} image -> {staged_name}")

    if modules_path:
        result.modules_count = _stage_modules(
            modules_path,
            staging_dir,
            modules_dest=modules_dest,
            preserve_tree=preserve_module_tree,
            include_metadata=include_module_metadata,
            strip_modules=strip_modules,
        )
        info(f"Staged {result.modules_count} kernel module(s) into modules/")

    overlay_dirs: list[tuple[str, Path | None]] = [
        ("ramdisk", ramdisk_dir),
        ("vendor_ramdisk", vendor_ramdisk_dir),
        ("patch", patch_dir),
        ("vendor_patch", vendor_patch_dir),
    ]
    for target_name, src_dir in overlay_dirs:
        if src_dir is not None:
            result.staged_overlays.append(_stage_overlay_dir(src_dir, staging_dir, target_name))
            info(f"Staged {target_name}/ overlay from {src_dir}")

    state = load_staging_state(staging_dir)
    ctx = build_template_context(
        workspace=workspace,
        kernel_version=result.kernel_version,
        kernel_image=result.kernel_image,
        ak_base_version=str(state.get("ak_base_version", "")),
    )
    if template_context:
        for key, value in template_context.items():
            if value or key not in ctx:
                ctx[key] = value

    if _write_text_or_file(
        staging_dir,
        "banner",
        inline_text=banner,
        file_path=banner_file,
        template_context=ctx,
    ):
        result.staged_overlays.append("banner")

    if _write_text_or_file(
        staging_dir,
        "version",
        inline_text=version,
        file_path=version_file,
        template_context=ctx,
    ):
        result.staged_overlays.append("version")

    save_staging_state(
        staging_dir,
        {
            "kernel_image": result.kernel_image,
            "kernel_version": result.kernel_version,
            "kernel_banner": result.kernel_banner,
            "staged_images": result.staged_images,
            "modules_count": result.modules_count,
            "staged_overlays": result.staged_overlays,
        },
    )

    return result
