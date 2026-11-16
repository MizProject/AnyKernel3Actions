from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from ak3_actions.repo import (
    delete_git_tags,
    prepare_anykernel_base,
    read_ak_base_version,
    strip_git_metadata,
)


def _create_mock_ak3_git_repo(root: Path) -> tuple[Path, str]:
    """Create a local git repository mimicking osm0sis/AnyKernel3 with tags."""
    root.mkdir(parents=True, exist_ok=True)
    (root / ".github").mkdir(parents=True, exist_ok=True)
    (root / ".github" / "FUNDING.yml").write_text("github: osm0sis\n", encoding="utf-8")
    (root / ".gitignore").write_text("*.zip\n", encoding="utf-8")
    (root / "LICENSE").write_text("GPLv3\n", encoding="utf-8")
    (root / "README.md").write_text("# AnyKernel3\n", encoding="utf-8")
    (root / "anykernel.sh").write_text(
        "### AnyKernel3\nproperties() { '\nkernel.string=Example\n'; } # end properties\n. tools/ak3-core.sh;\n",
        encoding="utf-8",
    )
    meta = root / "META-INF" / "com" / "google" / "android"
    meta.mkdir(parents=True, exist_ok=True)
    (meta / "update-binary").write_text("#!/sbin/sh\necho ok\n", encoding="utf-8")
    (meta / "updater-script").write_text("# AK_BASE_VERSION=20261027\n", encoding="utf-8")
    tools = root / "tools"
    tools.mkdir(parents=True, exist_ok=True)
    (tools / "ak3-core.sh").write_text("# core\n", encoding="utf-8")
    (tools / "busybox").write_bytes(b"\x7fELFstub")
    (root / "ramdisk").mkdir(parents=True, exist_ok=True)
    (root / "ramdisk" / "placeholder").write_text("", encoding="utf-8")

    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
    }
    subprocess.run(["git", "init", "-b", "master"], cwd=root, check=True, capture_output=True, env=env)
    subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True, env=env)
    subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True, capture_output=True, env=env)
    subprocess.run(["git", "tag", "v1.0.0"], cwd=root, check=True, capture_output=True, env=env)
    subprocess.run(["git", "tag", "-a", "v1.1.0", "-m", "release"], cwd=root, check=True, capture_output=True, env=env)
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, check=True, capture_output=True, text=True
    ).stdout.strip()
    return root, sha


class TestRepoComponent(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="ak3_test_repo_"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_delete_git_tags_removes_all_tags(self) -> None:
        repo_dir, _ = _create_mock_ak3_git_repo(self.tmp / "upstream")
        deleted = delete_git_tags(repo_dir)
        self.assertEqual(sorted(deleted), ["v1.0.0", "v1.1.0"])
        remaining = subprocess.run(
            ["git", "-C", str(repo_dir), "tag", "-l"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        self.assertEqual(remaining, "")

    def test_prepare_clones_and_strips_git_and_tags(self) -> None:
        repo_dir, sha = _create_mock_ak3_git_repo(self.tmp / "upstream")
        staging = self.tmp / "staging"
        res = prepare_anykernel_base(
            anykernel_repo=repo_dir.as_uri(),
            anykernel_ref="master",
            staging_dir=staging,
        )
        self.assertEqual(res.commit_sha, sha)
        self.assertEqual(res.ak_base_version, "20261027")
        self.assertFalse((staging / ".git").exists())
        self.assertFalse((staging / ".github").exists())
        self.assertFalse((staging / ".gitignore").exists())
        self.assertFalse((staging / "README.md").exists())
        self.assertTrue((staging / "LICENSE").is_file())
        self.assertTrue((staging / "tools" / "ak3-core.sh").is_file())

    def test_prepare_supports_commit_sha_ref(self) -> None:
        repo_dir, sha = _create_mock_ak3_git_repo(self.tmp / "upstream")
        staging = self.tmp / "staging-sha"
        res = prepare_anykernel_base(
            anykernel_repo=repo_dir.as_uri(),
            anykernel_ref=sha,
            staging_dir=staging,
        )
        self.assertEqual(res.commit_sha, sha)
        self.assertFalse((staging / ".git").exists())

    def test_prepare_local_path_does_not_mutate_source(self) -> None:
        repo_dir, _ = _create_mock_ak3_git_repo(self.tmp / "local_ak3")
        staging = self.tmp / "staging-local"
        res = prepare_anykernel_base(
            anykernel_path=repo_dir,
            staging_dir=staging,
        )
        self.assertTrue((repo_dir / ".git").exists())
        self.assertFalse((staging / ".git").exists())
        self.assertFalse((staging / ".github").exists())
        self.assertEqual(read_ak_base_version(staging), "20261027")
        self.assertIn(".git", res.removed_paths)

    def test_strip_git_metadata_handles_submodule_git_file(self) -> None:
        sub_dir, _ = _create_mock_ak3_git_repo(self.tmp / "sub")
        strip_git_metadata(sub_dir, strip_readme=False)
        (sub_dir / ".git").write_text("gitdir: ../../.git/modules/ak3\n", encoding="utf-8")
        _, removed = strip_git_metadata(sub_dir, strip_readme=True)
        self.assertIn(".git", removed)
        self.assertIn("README.md", removed)
        self.assertFalse((sub_dir / ".git").exists())

    def test_missing_required_files_raises(self) -> None:
        bad = self.tmp / "empty_ak3"
        bad.mkdir()
        with self.assertRaises(FileNotFoundError):
            prepare_anykernel_base(anykernel_path=bad, staging_dir=self.tmp / "bad_stage")


if __name__ == "__main__":
    unittest.main()

