"""[Beta] Automatic workspace, kernel-source, out/, and artifacts kernel discovery."""

from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .github_env import info
from .kernel import (
    AK3_SUPPORTED_KERNEL_NAMES,
    KERNEL_DISCOVERY_PRIORITY,
    KernelVerificationResult,
    inspect_kernel_binary,
)

_PRIORITY_INDEX: dict[str, int] = {name: i for i, name in enumerate(KERNEL_DISCOVERY_PRIORITY)}
_CONFIDENCE_RANK: dict[str, int] = {"high": 0, "medium": 1, "low": 2, "none": 3}
_DEFAULT_ARCHES: tuple[str, ...] = ("arm64", "arm", "x86", "x86_64", "riscv")
_SKIP_DIR_NAMES = frozenset({
    ".git",
    ".github",
    ".hg",
    ".svn",
    "node_modules",
    ".venv",
    "venv",
    "__pycache__",
    ".repo",
})
_ARTIFACT_DIR_NAMES: tuple[str, ...] = (
    "artifacts",
    "artifact",
    ".artifacts",
    "downloads",
    "downloaded-artifacts",
    "dist",
    "output",
    "build",
)
_KBUILD_MAKEFILE_RE = re.compile(r"^\s*(VERSION|PATCHLEVEL)\s*=", re.MULTILINE)


@dataclass
class DiscoveryCandidate:
    """Verified compiled kernel candidate found during autodetect scour."""

    path: Path
    stage: str
    verification: KernelVerificationResult


@dataclass
class DiscoveryResult:
    """Result of [Beta] kernel autodetect scour across workspace, out/, and artifacts."""

    kernel_path: Path
    kernel_image_name: str
    verification: KernelVerificationResult
    source_stage: str
    kernel_source_dir: Path | None = None
    dtb_path: Path | None = None
    dtbo_path: Path | None = None
    system_dlkm_path: Path | None = None
    vendor_dlkm_path: Path | None = None
    modules_path: Path | None = None
    searched_locations: list[str] = field(default_factory=list)
    rejected_candidates: list[tuple[Path, str]] = field(default_factory=list)


def _is_staging_dir(path: Path, exclude_dirs: set[Path]) -> bool:
    name = path.name
    if name.startswith(("anykernel3-", ".ak3-")):
        return True
    try:
        resolved = path.resolve()
    except OSError:
        return False
    return resolved in exclude_dirs


def is_linux_kernel_source_dir(directory: Path) -> bool:
    """Return True when directory looks like a Linux kernel source tree root."""
    if not directory.is_dir():
        return False
    makefile = directory / "Makefile"
    arch_dir = directory / "arch"
    if not makefile.is_file() or not arch_dir.is_dir():
        return False
    if not (
        (directory / "kernel").is_dir()
        or (directory / "drivers").is_dir()
        or (directory / "Kconfig").is_file()
    ):
        return False
    try:
        head = makefile.read_text(encoding="utf-8", errors="replace")[:4096]
    except OSError:
        return False
    matches = set(_KBUILD_MAKEFILE_RE.findall(head))
    return ("VERSION" in matches and "PATCHLEVEL" in matches) or "KBUILD_" in head


def find_kernel_source_dirs(
    workspace: Path,
    *,
    max_depth: int = 3,
    exclude_dirs: set[Path] | None = None,
) -> list[Path]:
    """Index workspace subdirectories up to max_depth to find Linux kernel source roots."""
    root = workspace.resolve()
    if not root.is_dir():
        return []
    excluded = exclude_dirs or set()
    found: list[Path] = []
    seen: set[Path] = set()

    def _walk(curr: Path, depth: int) -> None:
        if curr in seen or _is_staging_dir(curr, excluded):
            return
        seen.add(curr)
        if is_linux_kernel_source_dir(curr):
            found.append(curr)
            return  # Do not descend into subdirs of a matched kernel source tree
        if depth >= max_depth:
            return
        try:
            children = sorted(p for p in curr.iterdir() if p.is_dir() and not p.is_symlink())
        except OSError:
            return
        for child in children:
            if child.name in _SKIP_DIR_NAMES:
                continue
            _walk(child, depth + 1)

    _walk(root, 0)
    return found


def _arch_boot_dirs(base: Path, target_arch: str | None = None) -> list[Path]:
    """Return existing `arch/<arch>/boot` dirs (and shallow nested ones under `out/`) in priority order."""
    arch_list = [target_arch] if target_arch else list(_DEFAULT_ARCHES)
    dirs: list[Path] = []
    seen: set[Path] = set()

    def _add(p: Path) -> None:
        if p.is_dir():
            rp = p.resolve()
            if rp not in seen:
                seen.add(rp)
                dirs.append(rp)

    for arch in arch_list:
        _add(base / "arch" / arch / "boot")

    arch_root = base / "arch"
    if arch_root.is_dir():
        for arch_sub in sorted(arch_root.iterdir()):
            _add(arch_sub / "boot")

    # Also support multi-stage `out/<target>/.../arch/<arch>/boot` (e.g. Kleaf / O=out/target)
    if base.is_dir():
        for root, dirnames, _ in os.walk(base, followlinks=False):
            rel_depth = len(Path(root).relative_to(base).parts)
            dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIR_NAMES)
            if rel_depth > 4:
                dirnames[:] = []
                continue
            root_path = Path(root)
            if root_path.name == "boot" and root_path.parent.parent.name == "arch":
                if not target_arch or root_path.parent.name == target_arch:
                    _add(root_path)

    return dirs


def _scan_dir_for_kernels(
    directory: Path,
    stage: str,
    *,
    searched: list[str],
    rejected: list[tuple[Path, str]],
    visited_dirs: set[Path],
) -> list[DiscoveryCandidate]:
    if not directory.is_dir():
        return []
    resolved = directory.resolve()
    if resolved in visited_dirs:
        return []
    visited_dirs.add(resolved)
    searched.append(f"{stage}:{resolved}")

    candidates: list[DiscoveryCandidate] = []
    for name in AK3_SUPPORTED_KERNEL_NAMES:
        cand = resolved / name
        if not cand.is_file():
            continue
        ver = inspect_kernel_binary(cand)
        if ver.valid:
            info(
                f"[Beta] Verified kernel image: {cand} "
                f"(confidence={ver.confidence}, arch={ver.arch_hint or 'unknown'}, "
                f"comp={ver.compression}, version={ver.kernel_version or 'unknown'})"
            )
            candidates.append(DiscoveryCandidate(path=cand, stage=stage, verification=ver))
        else:
            rejected.append((cand, ver.reason))
            info(f"[Beta] Rejected candidate {cand}: {ver.reason}")
    return candidates


def _scan_out_tree(
    out_dir: Path,
    stage: str,
    *,
    target_arch: str | None,
    searched: list[str],
    rejected: list[tuple[Path, str]],
    visited_dirs: set[Path],
) -> list[DiscoveryCandidate]:
    if not out_dir.is_dir():
        return []
    found: list[DiscoveryCandidate] = []
    for boot_dir in _arch_boot_dirs(out_dir, target_arch=target_arch):
        found.extend(
            _scan_dir_for_kernels(
                boot_dir,
                stage,
                searched=searched,
                rejected=rejected,
                visited_dirs=visited_dirs,
            )
        )
    for extra in (out_dir / "dist", out_dir):
        if extra.is_dir():
            found.extend(
                _scan_dir_for_kernels(
                    extra,
                    stage,
                    searched=searched,
                    rejected=rejected,
                    visited_dirs=visited_dirs,
                )
            )
    return found


def _scour_recursive(
    root: Path,
    stage: str,
    *,
    max_depth: int,
    exclude_dirs: set[Path],
    searched: list[str],
    rejected: list[tuple[Path, str]],
    visited_dirs: set[Path],
) -> list[DiscoveryCandidate]:
    if not root.is_dir():
        return []
    resolved_root = root.resolve()
    if _is_staging_dir(resolved_root, exclude_dirs):
        return []

    found: list[DiscoveryCandidate] = []
    for dirpath, dirnames, filenames in os.walk(resolved_root, topdown=True, followlinks=False):
        curr = Path(dirpath)
        rel_depth = len(curr.relative_to(resolved_root).parts)
        dirnames[:] = sorted(
            d for d in dirnames
            if d not in _SKIP_DIR_NAMES and not _is_staging_dir(curr / d, exclude_dirs)
        )
        if rel_depth > max_depth:
            dirnames[:] = []
            continue
        if any(name in AK3_SUPPORTED_KERNEL_NAMES for name in filenames):
            found.extend(
                _scan_dir_for_kernels(
                    curr,
                    stage,
                    searched=searched,
                    rejected=rejected,
                    visited_dirs=visited_dirs,
                )
            )
    return found


def _rank_candidates(
    candidates: list[DiscoveryCandidate],
    target_arch: str | None = None,
) -> DiscoveryCandidate:
    def _sort_key(cand: DiscoveryCandidate) -> tuple[int, int, int, str]:
        arch_penalty = 0
        if target_arch:
            arch_match = (
                cand.verification.arch_hint == target_arch
                or f"/arch/{target_arch}/" in cand.path.as_posix()
            )
            arch_penalty = 0 if arch_match else 1
        conf_rank = _CONFIDENCE_RANK.get(cand.verification.confidence, 9)
        prio_rank = _PRIORITY_INDEX.get(cand.path.name, len(KERNEL_DISCOVERY_PRIORITY))
        return (arch_penalty, conf_rank, prio_rank, cand.path.as_posix())

    return min(candidates, key=_sort_key)


def _discover_companion_extras(
    kernel_path: Path,
    *,
    search_roots: list[Path],
    exclude_dirs: set[Path],
) -> tuple[Path | None, Path | None, Path | None, Path | None, Path | None]:
    """Look cleanly for companion dtb/dtbo/dlkm images and compiled .ko modules."""
    dtb_path: Path | None = None
    dtbo_path: Path | None = None
    system_dlkm_path: Path | None = None
    vendor_dlkm_path: Path | None = None
    modules_path: Path | None = None

    probe_dirs: list[Path] = [kernel_path.parent]
    for root in search_roots:
        if root.is_dir() and root.resolve() not in probe_dirs:
            probe_dirs.append(root.resolve())

    already_has_dtb = kernel_path.name.endswith("-dtb")

    for pdir in probe_dirs:
        if dtb_path is None and not already_has_dtb:
            for candidate in ("dtb", "dtb.img"):
                if (pdir / candidate).is_file():
                    dtb_path = pdir / candidate
                    break
        if dtbo_path is None:
            for candidate in ("dtbo.img", "dtbo"):
                if (pdir / candidate).is_file():
                    dtbo_path = pdir / candidate
                    break
        if system_dlkm_path is None:
            for candidate in ("system_dlkm.img", "system_dlkm"):
                if (pdir / candidate).is_file():
                    system_dlkm_path = pdir / candidate
                    break
        if vendor_dlkm_path is None:
            for candidate in ("vendor_dlkm.img", "vendor_dlkm"):
                if (pdir / candidate).is_file():
                    vendor_dlkm_path = pdir / candidate
                    break

    # Look for staged module directories (`modules/`, `modules_install/`, or `lib/modules/<ver>`)
    mod_candidates: list[Path] = []
    for root in probe_dirs:
        for sub in (
            root,
            root / "modules",
            root / "modules_install",
            root.parent / "modules_install",
            root.parent / "modules",
        ):
            if not sub.is_dir() or _is_staging_dir(sub, exclude_dirs):
                continue
            if (
                (sub / "lib" / "modules").is_dir()
                or any((sub / p).is_dir() for p in ("system", "vendor", "product", "system_ext"))
                or any(p.is_file() and p.suffix == ".ko" for p in sub.iterdir())
            ):
                mod_candidates.append(sub)

    for mdir in mod_candidates:
        if any(mdir.rglob("*.ko")):
            modules_path = mdir
            break

    return dtb_path, dtbo_path, system_dlkm_path, vendor_dlkm_path, modules_path


def discover_kernel_artifacts(
    *,
    cwd: Path | None = None,
    workspace: Path | None = None,
    artifacts_dir: Path | None = None,
    target_arch: str | None = None,
    autodetect_extras: bool = True,
    exclude_dirs: set[Path] | None = None,
) -> DiscoveryResult:
    """
    [Beta] Scour cwd, indexed kernel source tree (out/ then arch/), and GitHub Actions
    artifacts directories for verified compiled Linux kernel images.
    """
    info("[Beta] Autodetect enabled: scouring workspace, out/, arch/, and artifacts for a verified kernel...")

    curr = (cwd or Path.cwd()).resolve()
    ws_env = os.environ.get("GITHUB_WORKSPACE", "").strip()
    ws = (workspace or (Path(ws_env) if ws_env else curr)).resolve()
    excluded = {p.resolve() for p in (exclude_dirs or set())}

    searched: list[str] = []
    rejected: list[tuple[Path, str]] = []
    visited_dirs: set[Path] = set()
    chosen_stage = ""
    candidates: list[DiscoveryCandidate] = []
    matched_ksrc: Path | None = None

    # --- Stage 1: Current directory (`cwd`) -> `cwd/out` -> `cwd/arch/<arch>/boot` ---
    candidates = _scan_dir_for_kernels(
        curr, "cwd", searched=searched, rejected=rejected, visited_dirs=visited_dirs
    )
    if not candidates and (curr / "out").is_dir():
        candidates = _scan_out_tree(
            curr / "out",
            "cwd/out",
            target_arch=target_arch,
            searched=searched,
            rejected=rejected,
            visited_dirs=visited_dirs,
        )
    if not candidates and (curr / "arch").is_dir():
        for boot_dir in _arch_boot_dirs(curr, target_arch=target_arch):
            candidates.extend(
                _scan_dir_for_kernels(
                    boot_dir,
                    "cwd/arch",
                    searched=searched,
                    rejected=rejected,
                    visited_dirs=visited_dirs,
                )
            )
    if candidates:
        chosen_stage = candidates[0].stage
        if is_linux_kernel_source_dir(curr):
            matched_ksrc = curr

    # --- Stage 2: Index workspace to locate kernel source dirs -> out/ -> arch/ ---
    kernel_sources: list[Path] = []
    if not candidates:
        kernel_sources = find_kernel_source_dirs(ws, max_depth=3, exclude_dirs=excluded)
        if kernel_sources:
            info(f"[Beta] Indexed kernel source dir(s): {', '.join(str(p) for p in kernel_sources)}")
        for ksrc in kernel_sources:
            out_dir = ksrc / "out"
            if out_dir.is_dir():
                candidates = _scan_out_tree(
                    out_dir,
                    "kernel_source/out",
                    target_arch=target_arch,
                    searched=searched,
                    rejected=rejected,
                    visited_dirs=visited_dirs,
                )
                if candidates:
                    matched_ksrc = ksrc
                    chosen_stage = "kernel_source/out"
                    break

        # Sibling workspace/out fallback (e.g. `O=../out`)
        if not candidates and (ws / "out").is_dir():
            candidates = _scan_out_tree(
                ws / "out",
                "workspace/out",
                target_arch=target_arch,
                searched=searched,
                rejected=rejected,
                visited_dirs=visited_dirs,
            )
            if candidates:
                chosen_stage = "workspace/out"
                if kernel_sources:
                    matched_ksrc = kernel_sources[0]

        # If out/ is not present (or had no verified kernel), inspect <ksrc>/arch/<arch>/boot
        if not candidates:
            for ksrc in kernel_sources:
                for boot_dir in _arch_boot_dirs(ksrc, target_arch=target_arch):
                    candidates.extend(
                        _scan_dir_for_kernels(
                            boot_dir,
                            "kernel_source/arch",
                            searched=searched,
                            rejected=rejected,
                            visited_dirs=visited_dirs,
                        )
                    )
                if candidates:
                    matched_ksrc = ksrc
                    chosen_stage = "kernel_source/arch"
                    break

    # --- Stage 3: GitHub Actions artifacts & workspace scour ---
    artifact_roots: list[tuple[str, Path]] = []
    if not candidates:
        if artifacts_dir is not None:
            artifact_roots.append(("artifacts-dir", artifacts_dir.resolve()))
        for base in (curr, ws):
            for name in _ARTIFACT_DIR_NAMES:
                cand_dir = (base / name).resolve()
                if cand_dir.is_dir():
                    artifact_roots.append((f"artifacts:{name}", cand_dir))

        runner_temp = os.environ.get("RUNNER_TEMP", "").strip()
        if runner_temp:
            rt_path = Path(runner_temp).resolve()
            if rt_path.is_dir() and rt_path != Path(tempfile.gettempdir()).resolve():
                artifact_roots.append(("runner_temp", rt_path))

        seen_art: set[Path] = set()
        for stage_label, art_root in artifact_roots:
            if art_root in seen_art or _is_staging_dir(art_root, excluded):
                continue
            seen_art.add(art_root)
            candidates.extend(
                _scour_recursive(
                    art_root,
                    stage_label,
                    max_depth=5,
                    exclude_dirs=excluded,
                    searched=searched,
                    rejected=rejected,
                    visited_dirs=visited_dirs,
                )
            )
            if candidates:
                chosen_stage = stage_label
                break

    # Final bounded workspace scour when no out/ or explicit artifact dirs succeeded
    if not candidates:
        candidates = _scour_recursive(
            ws,
            "workspace_scour",
            max_depth=5,
            exclude_dirs=excluded,
            searched=searched,
            rejected=rejected,
            visited_dirs=visited_dirs,
        )
        if candidates:
            chosen_stage = "workspace_scour"

    if not candidates:
        rejected_msg = ""
        if rejected:
            sample = "; ".join(f"{p}: {reason}" for p, reason in rejected[:5])
            rejected_msg = f" Rejected candidate(s): {sample}."
        searched_msg = ", ".join(searched[:10]) if searched else str(ws)
        raise FileNotFoundError(
            f"[Beta] Autodetect could not locate a verified compiled kernel image. "
            f"Searched: {searched_msg}.{rejected_msg} "
            "Pass 'kernel-image', 'kernel-dir', or 'artifacts-dir' manually instead."
        )

    winner = _rank_candidates(candidates, target_arch=target_arch)
    info(
        f"[Beta] Autodetect selected kernel {winner.path} from stage={chosen_stage} "
        f"(confidence={winner.verification.confidence}, arch={winner.verification.arch_hint or 'unknown'}, "
        f"version={winner.verification.kernel_version or 'unknown'})"
    )

    extra_search_roots: list[Path] = [winner.path.parent]
    if matched_ksrc is not None and (matched_ksrc / "out").is_dir():
        extra_search_roots.append(matched_ksrc / "out")
    if artifacts_dir is not None:
        extra_search_roots.append(artifacts_dir.resolve())
    if (ws / "out").is_dir():
        extra_search_roots.append(ws / "out")
    for _, art_root in artifact_roots:
        extra_search_roots.append(art_root)

    dtb_p: Path | None = None
    dtbo_p: Path | None = None
    sys_dlkm_p: Path | None = None
    vnd_dlkm_p: Path | None = None
    mod_p: Path | None = None
    if autodetect_extras:
        dtb_p, dtbo_p, sys_dlkm_p, vnd_dlkm_p, mod_p = _discover_companion_extras(
            winner.path,
            search_roots=extra_search_roots,
            exclude_dirs=excluded,
        )
        if any((dtb_p, dtbo_p, sys_dlkm_p, vnd_dlkm_p, mod_p)):
            info(
                "[Beta] Autodetected companion artifact(s): "
                + ", ".join(
                    f"{k}={v}"
                    for k, v in (
                        ("dtb", dtb_p),
                        ("dtbo", dtbo_p),
                        ("system_dlkm", sys_dlkm_p),
                        ("vendor_dlkm", vnd_dlkm_p),
                        ("modules", mod_p),
                    )
                    if v is not None
                )
            )

    return DiscoveryResult(
        kernel_path=winner.path,
        kernel_image_name=winner.path.name,
        verification=winner.verification,
        source_stage=chosen_stage,
        kernel_source_dir=matched_ksrc,
        dtb_path=dtb_p,
        dtbo_path=dtbo_p,
        system_dlkm_path=sys_dlkm_p,
        vendor_dlkm_path=vnd_dlkm_p,
        modules_path=mod_p,
        searched_locations=searched,
        rejected_candidates=rejected,
    )
