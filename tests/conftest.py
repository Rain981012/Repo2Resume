from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "repo2resume-home"
    root.mkdir()
    monkeypatch.setenv("REPO2RESUME_DATA_DIR", str(root))
    return root
