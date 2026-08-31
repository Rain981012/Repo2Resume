from __future__ import annotations

import os
from pathlib import Path

import pytest

_PROXY_VARS = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
)
for _proxy_var in _PROXY_VARS:
    os.environ.pop(_proxy_var, None)


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "repo2resume-home"
    root.mkdir()
    monkeypatch.setenv("REPO2RESUME_DATA_DIR", str(root))
    return root
