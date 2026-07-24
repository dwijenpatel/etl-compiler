"""The type gate: mypy must pass over the product code (strict on the runtime).

Part of the normal suite so type-safety is enforced, not aspirational. Locates
mypy in the project venv (``uv sync --group dev``) or on PATH; if neither
exists the test fails with instructions rather than silently skipping — a gate
that can be absent is not a gate. Set ETL_SOLVED_SKIP_TYPECHECK=1 to bypass in
minimal environments (e.g. a customer clone exercising only runtime behavior).
"""
import os
import shutil
import subprocess
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _find_mypy() -> list[str] | None:
    venv = os.path.join(REPO, ".venv", "bin", "mypy")
    if os.path.exists(venv):
        return [venv]
    on_path = shutil.which("mypy")
    if on_path:
        return [on_path]
    return None


class TestTypeGate(unittest.TestCase):
    def test_mypy_clean(self) -> None:
        if os.environ.get("ETL_SOLVED_SKIP_TYPECHECK") == "1":
            self.skipTest("explicitly bypassed via ETL_SOLVED_SKIP_TYPECHECK=1")
        mypy = _find_mypy()
        self.assertIsNotNone(
            mypy,
            "mypy not found. Run `uv sync --group dev` in the repo root (or set "
            "ETL_SOLVED_SKIP_TYPECHECK=1 to bypass in a minimal environment).")
        assert mypy is not None
        proc = subprocess.run(
            mypy + ["--config-file", os.path.join(REPO, "pyproject.toml")],
            cwd=REPO, capture_output=True, text=True, timeout=300)
        self.assertEqual(
            proc.returncode, 0,
            "mypy failed:\n" + proc.stdout + proc.stderr)


if __name__ == "__main__":
    unittest.main()
