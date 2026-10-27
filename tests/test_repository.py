import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(shutil.which("git") is None or not (ROOT / ".git").exists(), reason="needs a git checkout")
def test_no_shipped_file_is_git_ignored():
    """Regression: a broad 'data/' ignore rule once kept aims/data/skills.json out of every commit."""
    shipped = [p for folder in ("aims", "tests", "scripts", ".github") for p in (ROOT / folder).rglob("*")
               if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"]
    shipped += [ROOT / name for name in ("launcher.pyw", "requirements.txt", "requirements-dev.txt", "pyproject.toml")]
    paths = "\n".join(str(p.relative_to(ROOT)).replace("\\", "/") for p in shipped)
    r = subprocess.run(["git", "check-ignore", "--stdin"], cwd=ROOT, input=paths, capture_output=True, text=True)
    assert r.stdout.strip() == "", f"These files would never be committed:\n{r.stdout}"
