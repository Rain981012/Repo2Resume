from __future__ import annotations

import os
import subprocess
from pathlib import Path


def run(
    cmd: list[str],
    cwd: Path,
    *,
    author_name: str | None = None,
    author_email: str | None = None,
) -> None:
    env = {
        **os.environ,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
    }
    # Clear inherited author overrides so local git config wins.
    for key in (
        "GIT_AUTHOR_NAME",
        "GIT_AUTHOR_EMAIL",
        "GIT_COMMITTER_NAME",
        "GIT_COMMITTER_EMAIL",
    ):
        env.pop(key, None)
    if author_name is not None:
        env["GIT_AUTHOR_NAME"] = author_name
        env["GIT_COMMITTER_NAME"] = author_name
    if author_email is not None:
        env["GIT_AUTHOR_EMAIL"] = author_email
        env["GIT_COMMITTER_EMAIL"] = author_email

    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, env=env)
    if result.returncode != 0:
        raise RuntimeError(f"cmd failed: {cmd}\nstdout={result.stdout}\nstderr={result.stderr}")


def init_fixture_repo(
    root: Path,
    *,
    author_name: str = "Ada Lovelace",
    author_email: str = "ada@example.com",
) -> Path:
    """Create a tiny git repo with known commits for miner assertions."""
    root.mkdir(parents=True, exist_ok=True)
    run(["git", "init", "-b", "main"], root)
    run(["git", "config", "user.name", author_name], root)
    run(["git", "config", "user.email", author_email], root)

    (root / "app.py").write_text("print('hello')\n" * 10)
    (root / "requirements.txt").write_text("fastapi>=0.100\nsqlalchemy>=2.0\n")
    run(["git", "add", "app.py", "requirements.txt"], root)
    run(
        ["git", "commit", "-m", "feat: initial api"],
        root,
        author_name=author_name,
        author_email=author_email,
    )

    (root / "app.py").write_text("print('hello')\n" * 12 + "# fix\n")
    run(["git", "add", "app.py"], root)
    run(
        ["git", "commit", "-m", "fix: off-by-one"],
        root,
        author_name=author_name,
        author_email=author_email,
    )

    (root / "README.md").write_text("# Demo\n")
    run(["git", "add", "README.md"], root)
    run(
        ["git", "commit", "-m", "docs: readme"],
        root,
        author_name="Other Dev",
        author_email="other@example.com",
    )

    return root
