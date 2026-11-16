"""GitHub Actions environment, input parsing, template, and state helpers."""

from __future__ import annotations

import datetime as _dt
import json
import os
import re
import subprocess
import uuid
from pathlib import Path
from typing import Any

_TRUTHY_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSY_VALUES = frozenset({"0", "false", "no", "off"})
_TEMPLATE_VAR_RE = re.compile(r"\{([a-zA-Z0-9_]+)\}")


def get_env_input(name: str, default: str | None = None) -> str | None:
    """Read an action input from environment variables (supporting '_' and '-')."""
    base = name.strip().upper()
    candidates = [
        f"INPUT_{base.replace('-', '_')}",
        f"INPUT_{base.replace('_', '-')}",
        base.replace("-", "_"),
    ]
    for key in candidates:
        val = os.environ.get(key)
        if val is not None and val.strip() != "":
            return val.strip()
    return default


def get_env_raw(name: str) -> str | None:
    """Read an action input preserving internal newlines/indentation when non-empty."""
    base = name.strip().upper()
    candidates = [
        f"INPUT_{base.replace('-', '_')}",
        f"INPUT_{base.replace('_', '-')}",
        base.replace("-", "_"),
    ]
    for key in candidates:
        val = os.environ.get(key)
        if val is not None and val != "":
            return val
    return None


def is_truthy(value: str | bool | None, default: bool = False) -> bool:
    """Parse common boolean strings ('true'/'false', '1'/'0', 'yes'/'no')."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    norm = str(value).strip().lower()
    if norm == "":
        return default
    if norm in _TRUTHY_VALUES:
        return True
    if norm in _FALSY_VALUES:
        return False
    raise ValueError(f"Invalid boolean value: {value!r}")


def normalize_ak_flag(
    value: str | bool | int | None,
    *,
    allow_auto: bool = False,
) -> str | None:
    """Normalize an AnyKernel3 flag to '0', '1', or optionally 'auto'."""
    if value is None:
        return None
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int) and value in (0, 1):
        return str(value)
    norm = str(value).strip().lower()
    if norm == "":
        return None
    if allow_auto and norm == "auto":
        return "auto"
    if norm in _TRUTHY_VALUES:
        return "1"
    if norm in _FALSY_VALUES:
        return "0"
    allowed = "'0', '1', 'auto'" if allow_auto else "'0', '1'"
    raise ValueError(f"Invalid flag value {value!r}; expected {allowed} (or true/false)")


def parse_list(value: str | list[str] | tuple[str, ...] | None) -> list[str]:
    """Split comma-, whitespace-, or newline-delimited lists cleanly."""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        items: list[str] = []
        for entry in value:
            s = str(entry).strip()
            if s:
                items.append(s)
        return items
    text = str(value).strip()
    if not text:
        return []
    # Support JSON arrays if passed in action input
    if text.startswith("[") and text.endswith("]"):
        try:
            loaded = json.loads(text)
            if isinstance(loaded, list):
                return [str(x).strip() for x in loaded if str(x).strip()]
        except json.JSONDecodeError:
            pass
    return [part.strip() for part in re.split(r"[\s,]+", text) if part.strip()]


def parse_kv_lines(text: str | None) -> dict[str, str]:
    """Parse key=value lines (ignoring empty lines and # comments)."""
    result: dict[str, str] = {}
    if not text:
        return result
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ValueError(f"Expected key=value format, got: {raw_line!r}")
        key, val = line.split("=", 1)
        key = key.strip()
        if not key:
            raise ValueError(f"Empty key in property line: {raw_line!r}")
        result[key] = val.strip()
    return result


def load_config_file(path: Path) -> dict[str, Any]:
    """Load configuration from JSON, TOML (Python 3.11+), properties, or YAML (if PyYAML exists)."""
    if not path.is_file():
        raise FileNotFoundError(f"Config file not found: {path}")
    suffix = path.suffix.lower()
    raw = path.read_text(encoding="utf-8")
    if suffix == ".json":
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError(f"Config JSON root must be an object: {path}")
        return data
    if suffix == ".toml":
        try:
            import tomllib  # Python 3.11+
        except ImportError as exc:
            raise RuntimeError(
                f"TOML config {path} requires Python 3.11+ (tomllib); use JSON or .properties on older Python."
            ) from exc
        data = tomllib.loads(raw)
        if not isinstance(data, dict):
            raise ValueError(f"Config TOML root must be a table: {path}")
        return data
    if suffix in {".properties", ".conf", ".env", ".cfg"}:
        return parse_kv_lines(raw)
    if suffix in {".yml", ".yaml"}:
        try:
            import yaml  # type: ignore[import-untyped]
        except ImportError as exc:
            raise RuntimeError(
                f"YAML config {path} requires PyYAML; use JSON, TOML, or .properties for zero-dependency runs."
            ) from exc
        data = yaml.safe_load(raw) or {}
        if not isinstance(data, dict):
            raise ValueError(f"Config YAML root must be a mapping: {path}")
        return data
    # Fallback: try JSON first, then key=value properties
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass
    return parse_kv_lines(raw)


def set_github_output(name: str, value: str) -> None:
    """Write an output variable to GITHUB_OUTPUT if running under GitHub Actions."""
    out_file = os.environ.get("GITHUB_OUTPUT")
    if not out_file:
        return
    path = Path(out_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = str(value)
    with path.open("a", encoding="utf-8") as fh:
        if "\n" in text or "\r" in text:
            delimiter = f"ghadelimiter_{uuid.uuid4().hex}"
            fh.write(f"{name}<<{delimiter}\n{text}\n{delimiter}\n")
        else:
            fh.write(f"{name}={text}\n")


def append_step_summary(markdown: str) -> None:
    """Append markdown to GITHUB_STEP_SUMMARY when available."""
    summary_file = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_file:
        return
    path = Path(summary_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(markdown.rstrip() + "\n")


def warn(message: str) -> None:
    """Emit a GitHub Actions warning annotation or stderr log."""
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print(f"::warning::{message}")
    else:
        print(f"WARNING: {message}")


def info(message: str) -> None:
    """Print an informational log line."""
    print(message)


def detect_git_sha(workspace: Path) -> str:
    """Resolve current git SHA from GITHUB_SHA or git rev-parse."""
    env_sha = os.environ.get("GITHUB_SHA", "").strip()
    if env_sha:
        return env_sha
    try:
        proc = subprocess.run(
            ["git", "-C", str(workspace), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode == 0:
            return proc.stdout.strip()
    except OSError:
        pass
    return ""


def build_template_context(
    *,
    workspace: Path | None = None,
    kernel_version: str = "",
    kernel_image: str = "",
    kernel_string: str = "",
    device_names: list[str] | None = None,
    ak_base_version: str = "",
) -> dict[str, str]:
    """Build standard template substitution variables."""
    now = _dt.datetime.now(_dt.timezone.utc)
    sha = detect_git_sha(workspace or Path.cwd())
    devices = device_names or []
    return {
        "kernel_version": kernel_version,
        "kernel_image": kernel_image,
        "kernel_string": kernel_string,
        "device": devices[0] if devices else "",
        "devices": "-".join(devices) if devices else "",
        "date": now.strftime("%Y%m%d"),
        "datetime": now.strftime("%Y%m%d-%H%M%S"),
        "sha": sha,
        "short_sha": sha[:7] if sha else "",
        "ak_base_version": ak_base_version,
    }


def resolve_template(text: str, context: dict[str, str]) -> str:
    """Replace known `{var}` placeholders while leaving unknown placeholders intact."""
    if "{" not in text:
        return text

    def _replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key in context:
            return context[key]
        return match.group(0)

    resolved = _TEMPLATE_VAR_RE.sub(_replace, text)
    # Clean up redundant separators caused by empty variables in filenames/labels
    return resolved


def state_file_for_staging(staging_dir: Path) -> Path:
    """Return the external metadata state path associated with a staging directory."""
    resolved = staging_dir.resolve()
    return resolved.parent / f".{resolved.name}.ak3-state.json"


def load_staging_state(staging_dir: Path) -> dict[str, Any]:
    """Load persisted inter-step state for a staging directory."""
    path = state_file_for_staging(staging_dir)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_staging_state(staging_dir: Path, updates: dict[str, Any]) -> dict[str, Any]:
    """Merge and persist inter-step state outside the AnyKernel3 staging tree."""
    state = load_staging_state(staging_dir)
    state.update(updates)
    path = state_file_for_staging(staging_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return state

