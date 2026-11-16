"""Component 1: Prepare AnyKernel3 base template and strip .git tags/metadata."""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .github_env import info, save_staging_state, warn

DEFAULT_ANYKERNEL_REPO = "https://github.com/osm0sis/AnyKernel3.git"
DEFAULT_ANYKERNEL_REF = "master"

_REQUIRED_AK3_FILES = (
    "META-INF/com/google/android/update-binary",
    "tools/ak3-core.sh",
    "anykernel.sh",
)
_GIT_METADATA_NAMES = frozenset({
    ".git",
    ".github",
    ".gitignore",
    ".gitattributes",
    ".gitmodules",
})
_AK_BASE_VERSION_RE = re.compile(r"AK_BASE_VERSION=([^\s#]+)")


@dataclass
class PreparedRepo:
    """Result returned by preparing the AnyKernel3 staging tree."""

    staging_dir: Path
    source: str
    ref: str
    commit_sha: str = ""
    ak_base_version: str = ""
    deleted_tags: list[str] = field(default_factory=list)
    removed_paths: list[str] = field(default_factory=list)


def _handle_remove_readonly(func, path, exc_info) -> None:
    """Make read-only files (e.g. git pack objects) writable before deletion."""
    try:
        os.chmod(path, stat.S_IWRITE | stat.S_IREAD | stat.S_IEXEC)
    except OSError:
        pass
    func(path)


def _rmtree(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
    elif path.exists():
        shutil.rmtree(path, onerror=_handle_remove_readonly)


def _run_git(args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    cmd = ["git", *args]
    return subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        check=False,
    )


def default_staging_dir() -> Path:
    """Create a default staging directory inside RUNNER_TEMP or system temp."""
    root = Path(os.environ.get("RUNNER_TEMP") or tempfile.gettempdir())
    root.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix="anykernel3-", dir=root))


def delete_git_tags(repo_dir: Path) -> list[str]:
    """Delete all local git tags inside a cloned repository directory."""
    git_entry = repo_dir / ".git"
    if not git_entry.exists():
        return []

    deleted: list[str] = []
    list_proc = _run_git(["-C", str(repo_dir), "tag", "-l"])
    if list_proc.returncode == 0:
        tags = [line.strip() for line in list_proc.stdout.splitlines() if line.strip()]
        if tags:
            del_proc = _run_git(["-C", str(repo_dir), "tag", "-d", *tags])
            if del_proc.returncode == 0:
                deleted.extend(tags)

    # Also ensure refs/tags is empty even if git binary is absent or .git is partial
    tags_dir = git_entry / "refs" / "tags"
    if tags_dir.is_dir():
        for entry in sorted(tags_dir.rglob("*")):
            if entry.is_file() and entry.name not in deleted:
                deleted.append(entry.name)
        _rmtree(tags_dir)
        tags_dir.mkdir(parents=True, exist_ok=True)

    packed_refs = git_entry / "packed-refs"
    if packed_refs.is_file():
        kept_lines: list[str] = []
        changed = False
        for line in packed_refs.read_text(encoding="utf-8", errors="replace").splitlines():
            if " refs/tags/" in line or line.startswith("^"):
                changed = True
                if " refs/tags/" in line:
                    tag_name = line.split(" refs/tags/", 1)[1].strip()
                    if tag_name and tag_name not in deleted:
                        deleted.append(tag_name)
                continue
            kept_lines.append(line)
        if changed:
            packed_refs.write_text(
                ("\n".join(kept_lines) + "\n") if kept_lines else "",
                encoding="utf-8",
            )

    return deleted


def strip_git_metadata(staging_dir: Path, *, strip_readme: bool = True) -> tuple[list[str], list[str]]:
    """Delete git tags and remove .git, .github, .git* files, and optionally README.md."""
    deleted_tags = delete_git_tags(staging_dir)
    removed_paths: list[str] = []

    for root, dirs, files in os.walk(staging_dir, topdown=True):
        root_path = Path(root)
        # Prune and remove .git / .github directories
        for dname in list(dirs):
            if dname in _GIT_METADATA_NAMES:
                target = root_path / dname
                _rmtree(target)
                removed_paths.append(str(target.relative_to(staging_dir)))
                dirs.remove(dname)
        # Remove .git file (submodule pointer), .gitignore, .gitattributes, etc.
        for fname in files:
            if fname in _GIT_METADATA_NAMES:
                target = root_path / fname
                _rmtree(target)
                removed_paths.append(str(target.relative_to(staging_dir)))

    if strip_readme:
        for readme_name in ("README.md", "readme.md"):
            readme_path = staging_dir / readme_name
            if readme_path.exists():
                readme_path.unlink()
                removed_paths.append(readme_name)

    return deleted_tags, sorted(set(removed_paths))


def read_ak_base_version(staging_dir: Path) -> str:
    """Extract AK_BASE_VERSION from META-INF/com/google/android/updater-script if present."""
    updater = staging_dir / "META-INF" / "com" / "google" / "android" / "updater-script"
    if not updater.is_file():
        return ""
    text = updater.read_text(encoding="utf-8", errors="replace")
    match = _AK_BASE_VERSION_RE.search(text)
    return match.group(1).strip() if match else ""


def validate_ak3_tree(staging_dir: Path) -> None:
    """Verify essential AnyKernel3 files exist in the staging directory."""
    missing = [rel for rel in _REQUIRED_AK3_FILES if not (staging_dir / rel).is_file()]
    if missing:
        raise FileNotFoundError(
            f"Invalid AnyKernel3 base directory {staging_dir}; missing required file(s): {', '.join(missing)}"
        )
    if not (staging_dir / "LICENSE").is_file():
        warn(
            "AnyKernel3 LICENSE file is missing; AnyKernel3 requires keeping LICENSE in flashable zips."
        )


def _clone_anykernel(repo_url: str, ref: str, dest: Path) -> str:
    """Clone AnyKernel3 shallowly with --no-tags and return the resolved HEAD commit SHA."""
    if shutil.which("git") is None:
        raise RuntimeError("git executable is required to clone AnyKernel3 repository.")

    # Attempt shallow branch/tag clone with --no-tags first
    clone_proc = _run_git([
        "clone",
        "--depth",
        "1",
        "--no-tags",
        "--branch",
        ref,
        repo_url,
        str(dest),
    ])
    if clone_proc.returncode != 0:
        # Clean partial directory before fallback fetch (supports exact commit SHAs)
        _rmtree(dest)
        dest.mkdir(parents=True, exist_ok=True)
        init_proc = _run_git(["init"], cwd=dest)
        if init_proc.returncode != 0:
            raise RuntimeError(f"git init failed: {init_proc.stderr.strip()}")
        remote_proc = _run_git(["remote", "add", "origin", repo_url], cwd=dest)
        if remote_proc.returncode != 0:
            raise RuntimeError(f"git remote add failed: {remote_proc.stderr.strip()}")
        fetch_proc = _run_git(["fetch", "--depth", "1", "--no-tags", "origin", ref], cwd=dest)
        if fetch_proc.returncode != 0:
            raise RuntimeError(
                f"Failed to clone or fetch {repo_url}@{ref}: {clone_proc.stderr.strip()} | {fetch_proc.stderr.strip()}"
            )
        checkout_proc = _run_git(["checkout", "--detach", "FETCH_HEAD"], cwd=dest)
        if checkout_proc.returncode != 0:
            raise RuntimeError(f"git checkout FETCH_HEAD failed: {checkout_proc.stderr.strip()}")

    rev_proc = _run_git(["rev-parse", "HEAD"], cwd=dest)
    return rev_proc.stdout.strip() if rev_proc.returncode == 0 else ""


def prepare_anykernel_base(
    *,
    anykernel_repo: str = DEFAULT_ANYKERNEL_REPO,
    anykernel_ref: str = DEFAULT_ANYKERNEL_REF,
    anykernel_path: Path | None = None,
    staging_dir: Path | None = None,
    clean_staging: bool = True,
    strip_readme: bool = True,
) -> PreparedRepo:
    """Clone or copy AnyKernel3 base, delete .git tags/metadata, and validate structure."""
    target_dir = staging_dir.resolve() if staging_dir else default_staging_dir()

    source_desc: str
    commit_sha = ""

    if anykernel_path is not None:
        src = anykernel_path.resolve()
        if not src.is_dir():
            raise FileNotFoundError(f"Local anykernel-path does not exist or is not a directory: {src}")
        source_desc = str(src)
        if src != target_dir:
            if clean_staging and target_dir.exists():
                _rmtree(target_dir)
            target_dir.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(src, target_dir, symlinks=True, dirs_exist_ok=True)
        if (target_dir / ".git").exists() and shutil.which("git"):
            rev = _run_git(["-C", str(target_dir), "rev-parse", "HEAD"])
            if rev.returncode == 0:
                commit_sha = rev.stdout.strip()
    else:
        source_desc = anykernel_repo
        if clean_staging and target_dir.exists():
            _rmtree(target_dir)
        target_dir.parent.mkdir(parents=True, exist_ok=True)
        commit_sha = _clone_anykernel(anykernel_repo, anykernel_ref, target_dir)

    deleted_tags, removed_paths = strip_git_metadata(target_dir, strip_readme=strip_readme)
    validate_ak3_tree(target_dir)
    ak_base_version = read_ak_base_version(target_dir)

    save_staging_state(
        target_dir,
        {
            "source": source_desc,
            "ref": anykernel_ref if anykernel_path is None else "local",
            "commit_sha": commit_sha,
            "ak_base_version": ak_base_version,
        },
    )

    info(
        f"Prepared AnyKernel3 in {target_dir} "
        f"(source={source_desc}, commit={commit_sha[:12] if commit_sha else 'n/a'}, "
        f"ak_base_version={ak_base_version or 'unknown'}, "
        f"deleted_tags={len(deleted_tags)}, removed={removed_paths})"
    )

    return PreparedRepo(
        staging_dir=target_dir,
        source=source_desc,
        ref=anykernel_ref if anykernel_path is None else "local",
        commit_sha=commit_sha,
        ak_base_version=ak_base_version,
        deleted_tags=deleted_tags,
        removed_paths=removed_paths,
    )

