"""并发写 SQLite 不应抛 InterfaceError / SystemError（搜岗走线程池）。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from repo2resume.storage.db import Database
from repo2resume.storage.models import (
    JobCandidate,
    JobContent,
    JobDiscovery,
    JobVerification,
    SearchRun,
)


def _candidate(i: int, run_id: str) -> JobCandidate:
    return JobCandidate(
        candidate_id=f"cand-{i}",
        run_id=run_id,
        source="tavily",
        url=f"https://example.com/job/{i}",
        confidence="high",
        discovery=JobDiscovery(
            query=f"q{i}",
            source="tavily",
            title=f"Job {i}",
            discovered_at="2026-01-01T00:00:00Z",
        ),
        content=JobContent(title=f"Job {i}"),
        verification=JobVerification(status="live"),
    )


def test_parallel_search_run_and_candidate_writes(tmp_path):
    db = Database(tmp_path / "concurrent.db")
    n = 64

    def _write(i: int) -> None:
        run_id = f"run-{i % 8}"
        db.save_search_run(
            SearchRun(
                run_id=run_id,
                prefs_version=1,
                queries=[f"q{i}"],
                sources=["tavily"],
                started_at="2026-01-01T00:00:00Z",
            )
        )
        db.upsert_job_candidate(_candidate(i, run_id))
        db.load_latest_search_run()

    with ThreadPoolExecutor(max_workers=8) as pool:
        for fut in [pool.submit(_write, i) for i in range(n)]:
            fut.result()

    rows = db.conn.execute("SELECT COUNT(*) FROM job_candidates").fetchone()
    assert int(rows[0]) == n
    runs = db.conn.execute("SELECT COUNT(*) FROM job_search_runs").fetchone()
    assert int(runs[0]) == 8
    db.close()
