from __future__ import annotations

import sqlite3
from pathlib import Path

from repo2resume.storage.db import MIGRATIONS, SCHEMA_VERSION, Database, open_db
from repo2resume.storage.models import (
    JobCandidate,
    JobContent,
    JobDiscovery,
    JobSnapshot,
    JobVerification,
    SearchRun,
)


def test_open_db_migrates(tmp_path: Path) -> None:
    db = open_db(tmp_path / "repo2resume.db")
    assert db.current_version() == SCHEMA_VERSION
    tables = {
        row[0]
        for row in db.conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    assert "skill_profiles" in tables
    assert "chat_sessions" in tables
    assert "llm_traces" in tables
    db.close()


def test_obs_run_id_column_on_traces(tmp_path: Path) -> None:
    db = open_db(tmp_path / "repo2resume.db")
    cols = {row[1] for row in db.conn.execute("PRAGMA table_info(tool_traces)").fetchall()}
    assert "run_id" in cols
    llm_cols = {row[1] for row in db.conn.execute("PRAGMA table_info(llm_traces)").fetchall()}
    assert "run_id" in llm_cols
    db.close()


def test_migrate_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "repo2resume.db"
    db1 = open_db(path)
    db1.close()
    db2 = open_db(path)
    assert db2.current_version() == SCHEMA_VERSION
    db2.close()


def test_fts_documents_uses_trigram(tmp_path: Path) -> None:
    db = open_db(tmp_path / "repo2resume.db")
    sql = db.conn.execute("SELECT sql FROM sqlite_master WHERE name = 'fts_documents'").fetchone()[
        0
    ]
    assert "trigram" in sql.lower()
    db.close()


def test_v7_migration_preserves_fts_rows(tmp_path: Path) -> None:
    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(path))
    for version in range(1, 7):
        conn.executescript(MIGRATIONS[version])
        conn.execute("INSERT INTO schema_migrations(version) VALUES (?)", (version,))
    conn.execute(
        "INSERT INTO fts_documents(doc_id, text) VALUES (?, ?)",
        ("keep-me", "kubernetes cluster"),
    )
    conn.commit()
    conn.close()

    db = Database(path)
    row = db.conn.execute("SELECT doc_id FROM fts_documents WHERE doc_id = 'keep-me'").fetchone()
    assert row is not None
    sql = db.conn.execute("SELECT sql FROM sqlite_master WHERE name = 'fts_documents'").fetchone()[
        0
    ]
    assert "trigram" in sql.lower()
    db.close()


def test_v8_layered_job_tables_exist(tmp_path: Path) -> None:
    db = open_db(tmp_path / "repo2resume.db")
    tables = {
        row[0]
        for row in db.conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    assert "job_search_runs" in tables
    assert "job_candidates" in tables
    assert "job_snapshots" in tables
    db.close()


def test_layered_job_dao_roundtrip(tmp_path: Path) -> None:
    db = open_db(tmp_path / "repo2resume.db")
    run = SearchRun(
        run_id="run-test",
        queries=["python backend"],
        sources=["tavily"],
        started_at="2026-08-25T00:00:00+00:00",
        completed_at="2026-08-25T00:00:02+00:00",
    )
    db.save_search_run(run)
    loaded_run = db.load_latest_search_run()
    assert loaded_run is not None
    assert loaded_run.run_id == "run-test"

    candidate = JobCandidate(
        candidate_id="tavily-abc",
        run_id="run-test",
        source="tavily",
        url="https://www.zhipin.com/job_detail/abc",
        confidence="high",
        discovery=JobDiscovery(
            title="Python backend",
            snippet="FastAPI",
            discovered_at="2026-08-25T00:00:00+00:00",
            search_query="python backend",
        ),
        verification=JobVerification(status="unverified", method="search_snippet"),
        content=JobContent(jd_text="Python FastAPI", completeness=0.7),
    )
    db.upsert_job_candidate(candidate)
    loaded_candidate = db.load_job_candidate("tavily-abc")
    assert loaded_candidate is not None
    assert loaded_candidate.discovery.title == "Python backend"
    rows = db.list_job_candidates("run-test")
    assert rows and rows[0].candidate_id == "tavily-abc"

    snap = JobSnapshot(
        snapshot_id="snap-1",
        job_id="tavily-abc",
        candidate_id="tavily-abc",
        fetched_at="2026-08-25T00:00:05+00:00",
        source="detail_fetch",
        url="https://www.zhipin.com/job_detail/abc",
        content="Full job detail",
        content_hash="h1",
        completeness=0.9,
        verification_status="live",
    )
    db.save_job_snapshot(snap)
    loaded_snap = db.load_latest_job_snapshot("tavily-abc")
    assert loaded_snap is not None
    assert loaded_snap.content == "Full job detail"
    db.close()
