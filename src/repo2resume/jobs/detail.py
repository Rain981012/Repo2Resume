"""职位详情抓取：用户选岗后补全 JD 快照。"""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime

import httpx

from repo2resume.storage.db import Database
from repo2resume.storage.models import JobSnapshot

_SCRIPT_RE = re.compile(r"<script[\s\S]*?</script>", re.I)
_STYLE_RE = re.compile(r"<style[\s\S]*?</style>", re.I)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _extract_text(html: str) -> str:
    text = _SCRIPT_RE.sub(" ", html or "")
    text = _STYLE_RE.sub(" ", text)
    text = _TAG_RE.sub(" ", text)
    text = _WS_RE.sub(" ", text).strip()
    return text


def _content_completeness(text: str) -> float:
    if not text:
        return 0.0
    return max(0.0, min(1.0, min(len(text), 4000) / 4000.0))


def fetch_job_snapshot(
    *,
    job_id: str,
    url: str | None,
    source: str = "detail_fetch",
    timeout_s: float = 12.0,
) -> JobSnapshot | None:
    if not url or not url.startswith("http"):
        return None
    try:
        with httpx.Client(timeout=timeout_s, follow_redirects=True) as client:
            resp = client.get(url)
    except Exception:
        return None
    if resp.status_code >= 400:
        return None
    fetched_at = datetime.now(UTC).isoformat()
    text = _extract_text(resp.text or "")
    if not text:
        return None
    content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    snapshot_id = f"snap-{hashlib.sha256((job_id + fetched_at).encode('utf-8')).hexdigest()[:16]}"
    return JobSnapshot(
        snapshot_id=snapshot_id,
        job_id=job_id,
        fetched_at=fetched_at,
        source=source,
        url=url,
        content=text,
        content_hash=content_hash,
        completeness=_content_completeness(text),
        verification_status="live",
    )


def ensure_job_snapshot(
    db: Database | None,
    *,
    job_id: str,
    url: str | None,
    source: str = "detail_fetch",
) -> JobSnapshot | None:
    if db is None:
        return None
    latest = db.load_latest_job_snapshot(job_id)
    if latest is not None:
        return latest
    snap = fetch_job_snapshot(job_id=job_id, url=url, source=source)
    if snap is None:
        return None
    db.save_job_snapshot(snap)
    return snap
