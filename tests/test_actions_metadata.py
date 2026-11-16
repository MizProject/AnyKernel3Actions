from __future__ import annotations

import re
import unittest
from pathlib import Path

try:
    import yaml  # type: ignore[import-untyped]
except ImportError:
    yaml = None

REPO_ROOT = Path(__file__).resolve().parent.parent
ACTION_FILES = [
    REPO_ROOT / "action.yaml",
    REPO_ROOT / "components" / "prepare" / "action.yaml",
    REPO_ROOT / "components" / "discover" / "action.yaml",
    REPO_ROOT / "components" / "process-kernel" / "action.yaml",
    REPO_ROOT / "components" / "configure" / "action.yaml",
    REPO_ROOT / "components" / "package" / "action.yaml",
]


@unittest.skipIf(yaml is None, "PyYAML not available")
class TestActionYamlFiles(unittest.TestCase):
    def test_all_action_yaml_files_are_valid_and_safe(self) -> None:
        for action_path in ACTION_FILES:
            with self.subTest(action=str(action_path.relative_to(REPO_ROOT))):
                self.assertTrue(action_path.is_file(), f"Missing {action_path}")
                data = yaml.safe_load(action_path.read_text(encoding="utf-8"))
                self.assertIsInstance(data, dict)
                self.assertEqual(data["runs"]["using"], "composite")
                declared_inputs = set((data.get("inputs") or {}).keys())

                steps = data["runs"]["steps"]
                self.assertGreater(len(steps), 0)
                step_ids = {s.get("id") for s in steps if s.get("id")}

                for step in steps:
                    run_cmd = step.get("run", "")
                    # Never interpolate ${{ inputs.* }} directly inside shell run scripts
                    self.assertNotIn("${{ inputs.", run_cmd)
                    self.assertEqual(step.get("shell"), "bash")

                    for env_key, env_val in (step.get("env") or {}).items():
                        if isinstance(env_val, str):
                            m = re.match(r"^\$\{\{\s*inputs\.([a-zA-Z0-9_-]+)\s*\}\}$", env_val)
                            if m:
                                inp = m.group(1)
                                self.assertIn(inp, declared_inputs)
                                if env_key.startswith("INPUT_"):
                                    self.assertEqual(env_key, f"INPUT_{inp.upper()}")

                for out_name, out_spec in (data.get("outputs") or {}).items():
                    val = out_spec.get("value", "")
                    m = re.match(r"^\$\{\{\s*steps\.([a-zA-Z0-9_-]+)\.outputs\.([a-zA-Z0-9_-]+)\s*\}\}$", val)
                    self.assertIsNotNone(m, f"Unexpected output expression for {out_name}: {val}")
                    self.assertIn(m.group(1), step_ids)


if __name__ == "__main__":
    unittest.main()

