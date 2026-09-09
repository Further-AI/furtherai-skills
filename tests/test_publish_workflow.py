"""Exercise the workflow's guard against publishing an older main commit."""

import os
import subprocess
from pathlib import Path

import pytest
import yaml


@pytest.mark.parametrize("stale", [False, True])
def test_publish_workflow_rejects_stale_main(tmp_path: Path, stale: bool) -> None:
    remote = tmp_path / "remote.git"
    checkout = tmp_path / "checkout"

    def git(*args: str, cwd: Path = tmp_path) -> str:
        return subprocess.check_output(["git", *args], cwd=cwd, stderr=subprocess.STDOUT, text=True).strip()

    git("init", "--bare", str(remote))
    git("clone", str(remote), str(checkout))
    git("checkout", "-b", "main", cwd=checkout)
    for revision in ("first", "second"):
        (checkout / "skill.txt").write_text(revision)
        git("add", "skill.txt", cwd=checkout)
        git("-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", revision, cwd=checkout)
    git("push", "origin", "main", cwd=checkout)
    sha = git("rev-parse", "HEAD~1" if stale else "HEAD", cwd=checkout)
    workflow = yaml.safe_load((Path(__file__).parents[1] / ".github/workflows/validate.yml").read_text())
    guard = next(
        step["run"]
        for step in workflow["jobs"]["publish"]["steps"]
        if step.get("name") == "Require the current main commit"
    )
    result = subprocess.run(
        ["bash", "-e", "-c", guard],
        cwd=checkout,
        env={**os.environ, "GITHUB_SHA": sha},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == (1 if stale else 0)
    if stale:
        assert "A newer commit is on main" in result.stdout
