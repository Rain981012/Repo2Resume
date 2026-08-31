"""SQLite 持久化：技能画像、简历草稿、chat 会话、LLM trace 的统一存储。

用版本化迁移（`MIGRATIONS` dict）建表，`schema_migrations` 表记录已应用版本，
`Database.migrate` 启动时自动补齐未应用的迁移。chat 会话相关方法供 agent `chat` 命令做会话持久化。
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

SCHEMA_VERSION = 9

MIGRATIONS: dict[int, str] = {
    1: """
    CREATE TABLE IF NOT EXISTS schema_migrations (
        version INTEGER PRIMARY KEY,
        applied_at TEXT NOT NULL DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS skill_profiles (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at TEXT NOT NULL DEFAULT (datetime('now')),
        label TEXT,
        payload_json TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS resume_drafts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at TEXT NOT NULL DEFAULT (datetime('now')),
        job_id TEXT,
        payload_json TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS chat_sessions (
        id TEXT PRIMARY KEY,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at TEXT NOT NULL DEFAULT (datetime('now')),
        title TEXT,
        messages_json TEXT NOT NULL DEFAULT '[]'
    );

    CREATE TABLE IF NOT EXISTS llm_traces (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        session_id TEXT,
        model TEXT,
        input_tokens INTEGER,
        output_tokens INTEGER,
        cost_usd REAL,
        latency_ms INTEGER,
        payload_json TEXT
    );
    """,
    2: """
    CREATE TABLE IF NOT EXISTS tool_traces (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        session_id TEXT,
        tool_name TEXT NOT NULL,
        args_json TEXT,
        result_json TEXT,
        error TEXT,
        latency_ms INTEGER
    );
    CREATE INDEX IF NOT EXISTS idx_tool_traces_session ON tool_traces(session_id);
    """,
    3: """
    -- Phase 3: 检索层元数据与索引
    CREATE TABLE IF NOT EXISTS retrieval_collections (
        name TEXT PRIMARY KEY,
        embed_model TEXT NOT NULL,
        dimension INTEGER NOT NULL,
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    );

    -- 全文检索：doc_id 不索引，只检索 text
    CREATE VIRTUAL TABLE IF NOT EXISTS fts_documents USING fts5(
        doc_id UNINDEXED,
        text
    );

    -- 职位缓存（Tavily / 手动 / mock）
    CREATE TABLE IF NOT EXISTS jobs (
        id TEXT PRIMARY KEY,
        title TEXT NOT NULL,
        company TEXT,
        location TEXT,
        jd_text TEXT,
        skills TEXT, -- JSON list
        source TEXT,
        url TEXT,
        posted_at TEXT,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE INDEX IF NOT EXISTS idx_jobs_source ON jobs(source);

    -- 检索评测 golden 数据集
    CREATE TABLE IF NOT EXISTS eval_golden (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        query TEXT NOT NULL,
        expected_doc_ids TEXT NOT NULL, -- JSON array
        notes TEXT,
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    );
    """,
    4: """
    -- Phase 3: jobs 表增加通用 JSON 载荷字段，用于缓存搜索原始结果。
    ALTER TABLE jobs ADD COLUMN payload_json TEXT;
    """,
    5: """
    -- 搜岗偏好：主 agent set_job_prefs 写入；search_jobs 开搜前硬检查。
    CREATE TABLE IF NOT EXISTS job_search_prefs (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        payload_json TEXT NOT NULL,
        updated_at TEXT NOT NULL DEFAULT (datetime('now'))
    );
    """,
    6: """
    -- 完整 git 统计 bundle：analyze 写入，简历索引/事实清单读取（含 README/commits）。
    CREATE TABLE IF NOT EXISTS repo_stats_bundles (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        stats_hash TEXT,
        payload_json TEXT NOT NULL
    );
    """,
    7: """
    -- FTS5 trigram：默认 unicode61 不切 CJK，中文近乎零召回。
    -- 不能 DROP 原表，否则已索引素材丢失；建新表回填再换名。
    CREATE VIRTUAL TABLE IF NOT EXISTS fts_documents_v2 USING fts5(
        doc_id UNINDEXED,
        text,
        tokenize = 'trigram'
    );
    INSERT INTO fts_documents_v2(doc_id, text) SELECT doc_id, text FROM fts_documents;
    DROP TABLE fts_documents;
    ALTER TABLE fts_documents_v2 RENAME TO fts_documents;
    """,
    8: """
    -- 搜岗分层模型：SearchRun / JobCandidate / JobSnapshot
    CREATE TABLE IF NOT EXISTS job_search_runs (
        run_id TEXT PRIMARY KEY,
        prefs_version INTEGER NOT NULL DEFAULT 1,
        queries_json TEXT NOT NULL,
        sources_json TEXT NOT NULL,
        started_at TEXT NOT NULL,
        completed_at TEXT,
        payload_json TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_job_search_runs_started ON job_search_runs(started_at);

    CREATE TABLE IF NOT EXISTS job_candidates (
        candidate_id TEXT PRIMARY KEY,
        run_id TEXT NOT NULL,
        source TEXT,
        url TEXT,
        confidence TEXT,
        verification_status TEXT,
        last_seen_at TEXT,
        last_verified_at TEXT,
        updated_at TEXT NOT NULL DEFAULT (datetime('now')),
        payload_json TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_job_candidates_run_id ON job_candidates(run_id);
    CREATE INDEX IF NOT EXISTS idx_job_candidates_url ON job_candidates(url);

    CREATE TABLE IF NOT EXISTS job_snapshots (
        snapshot_id TEXT PRIMARY KEY,
        job_id TEXT NOT NULL,
        candidate_id TEXT,
        fetched_at TEXT NOT NULL,
        source TEXT,
        url TEXT,
        content_hash TEXT,
        completeness REAL,
        verification_status TEXT,
        payload_json TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_job_snapshots_job_id_time
        ON job_snapshots(job_id, fetched_at DESC);
    """,
    9: """
    ALTER TABLE llm_traces ADD COLUMN run_id TEXT;
    ALTER TABLE tool_traces ADD COLUMN run_id TEXT;
    CREATE INDEX IF NOT EXISTS idx_llm_traces_run_id ON llm_traces(run_id);
    CREATE INDEX IF NOT EXISTS idx_tool_traces_run_id ON tool_traces(run_id);
    """,
}


class Database:
    """SQLite 连接 + 版本化迁移 + 会话读写。row_factory=Row 方便按列名取值。"""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        # 搜岗走线程池，多线程共用一个 sqlite 连接会抛 InterfaceError / SystemError。
        # 可重入，允许 persist_jobs 这类组合写在外层持锁时嵌套调用。
        self._lock = threading.RLock()
        self.migrate()

    @property
    def conn(self) -> sqlite3.Connection:
        """暴露底层连接，供 pipeline 等直接写表（如 skill_profiles）。"""
        return self._conn

    @property
    def lock(self) -> threading.RLock:
        """直接操作 `conn` 的调用方须自行持有本锁。"""
        return self._lock

    def current_version(self) -> int:
        """返回已应用的最新迁移版本；从未迁移过返回 0。"""
        row = self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
        ).fetchone()
        if row is None:
            return 0
        ver = self._conn.execute(
            "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
        ).fetchone()
        return int(ver[0]) if ver else 0

    def migrate(self) -> None:
        """按版本号顺序应用所有未应用的迁移脚本，每应用一条记进 `schema_migrations`。"""
        current = self.current_version()
        for version in sorted(MIGRATIONS):
            if version <= current:
                continue
            self._conn.executescript(MIGRATIONS[version])
            self._conn.execute(
                "INSERT INTO schema_migrations(version) VALUES (?)",
                (version,),
            )
            self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # ---- chat sessions -------------------------------------------------

    def save_session(self, session_id: str, messages: list[dict], title: str | None = None) -> None:
        """Upsert a chat session's messages (and optionally title)."""
        import json

        payload = json.dumps(messages, ensure_ascii=False)
        existing = self._conn.execute(
            "SELECT id FROM chat_sessions WHERE id = ?", (session_id,)
        ).fetchone()
        if existing is None:
            self._conn.execute(
                "INSERT INTO chat_sessions(id, title, messages_json) VALUES (?, ?, ?)",
                (session_id, title, payload),
            )
        else:
            if title is not None:
                self._conn.execute(
                    "UPDATE chat_sessions SET messages_json = ?, title = ?, "
                    "updated_at = datetime('now') WHERE id = ?",
                    (payload, title, session_id),
                )
            else:
                self._conn.execute(
                    "UPDATE chat_sessions SET messages_json = ?, "
                    "updated_at = datetime('now') WHERE id = ?",
                    (payload, session_id),
                )
        self._conn.commit()

    def load_session(self, session_id: str) -> list[dict] | None:
        """Return messages for a session, or None if it doesn't exist."""
        import json

        row = self._conn.execute(
            "SELECT messages_json FROM chat_sessions WHERE id = ?", (session_id,)
        ).fetchone()
        if row is None:
            return None
        return json.loads(row["messages_json"])

    def list_sessions(self, limit: int = 10) -> list[dict]:
        """Most-recently-updated sessions (rowid 作 tiebreaker，避免同秒打平)."""
        rows = self._conn.execute(
            "SELECT id, title, updated_at FROM chat_sessions "
            "ORDER BY updated_at DESC, rowid DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    # ---- tool traces (Phase 2.5 Step 4 观测) ---------------------------

    def record_tool_trace(
        self,
        session_id: str,
        tool_name: str,
        args: dict,
        result: object,
        error: object,
        latency_ms: int,
    ) -> None:
        """写一行工具调用 trace。result/error 二选一：出错时 result 置 NULL、error 存文本。"""
        import json

        def _dumps(v: object) -> str | None:
            if v is None:
                return None
            try:
                return json.dumps(v, ensure_ascii=False, default=str)
            except (TypeError, ValueError):
                return repr(v)

        from repo2resume.observability.run_context import current_run_id

        self._conn.execute(
            "INSERT INTO tool_traces"
            "(session_id, tool_name, args_json, result_json, error, latency_ms, run_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                session_id,
                tool_name,
                _dumps(args),
                _dumps(result) if error is None else None,
                str(error) if error is not None else None,
                latency_ms,
                current_run_id(),
            ),
        )
        self._conn.commit()

    def tool_trace_stats(self, session_id: str, run_id: str | None = None) -> dict:
        """聚合某 session 的工具调用统计，供 /cost 命令展示。

        返回: {count, total_ms, errors, per_tool: {name: {count, ms, errors}}}
        """
        sql = (
            "SELECT tool_name, COUNT(*) AS c, COALESCE(SUM(latency_ms), 0) AS ms, "
            "SUM(CASE WHEN error IS NOT NULL THEN 1 ELSE 0 END) AS errs "
            "FROM tool_traces WHERE session_id = ?"
        )
        params: tuple[object, ...] = (session_id,)
        if run_id:
            sql += " AND run_id = ?"
            params = (session_id, run_id)
        sql += " GROUP BY tool_name"
        rows = self._conn.execute(sql, params).fetchall()
        per_tool: dict[str, dict] = {}
        total_count = total_ms = total_errs = 0
        for r in rows:
            per_tool[r["tool_name"]] = {"count": r["c"], "ms": r["ms"], "errors": r["errs"]}
            total_count += r["c"]
            total_ms += r["ms"]
            total_errs += r["errs"]
        return {
            "count": total_count,
            "total_ms": total_ms,
            "errors": total_errs,
            "per_tool": per_tool,
        }

    # ---- llm traces (Phase 2.5 观测：LLM 调用成本) -----------------------

    def record_llm_trace(
        self,
        session_id: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        cost_usd: float | None,
        latency_ms: int,
        payload: object = None,
    ) -> None:
        """写一行 LLM 调用 trace 到 llm_traces。payload 可选（存 prompt/响应摘要）。"""
        import json

        def _dumps(v: object) -> str | None:
            if v is None:
                return None
            try:
                return json.dumps(v, ensure_ascii=False, default=str)
            except (TypeError, ValueError):
                return repr(v)

        from repo2resume.observability.run_context import current_run_id

        self._conn.execute(
            "INSERT INTO llm_traces"
            "(session_id, model, input_tokens, output_tokens, "
            "cost_usd, latency_ms, payload_json, run_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                session_id,
                model,
                input_tokens,
                output_tokens,
                cost_usd,
                latency_ms,
                _dumps(payload),
                current_run_id(),
            ),
        )
        self._conn.commit()

    def llm_trace_stats(self, session_id: str, run_id: str | None = None) -> dict:
        """聚合某 session 的 LLM 调用统计，供 /cost 展示成本。

        返回: {count, total_input, total_output, total_cost, total_latency_ms, per_model: {...}}
        """
        sql = (
            "SELECT model, COUNT(*) AS c, "
            "COALESCE(SUM(input_tokens), 0) AS ti, COALESCE(SUM(output_tokens), 0) AS to_, "
            "COALESCE(SUM(cost_usd), 0) AS cost, COALESCE(SUM(latency_ms), 0) AS ms "
            "FROM llm_traces WHERE session_id = ?"
        )
        params: tuple[object, ...] = (session_id,)
        if run_id:
            sql += " AND run_id = ?"
            params = (session_id, run_id)
        sql += " GROUP BY model"
        rows = self._conn.execute(sql, params).fetchall()
        per_model: dict[str, dict] = {}
        total_count = total_in = total_out = total_ms = 0
        total_cost = 0.0
        for r in rows:
            per_model[r["model"]] = {
                "count": r["c"],
                "input_tokens": r["ti"],
                "output_tokens": r["to_"],
                "cost_usd": r["cost"],
                "latency_ms": r["ms"],
            }
            total_count += r["c"]
            total_in += r["ti"]
            total_out += r["to_"]
            total_cost += r["cost"] or 0
            total_ms += r["ms"]
        return {
            "count": total_count,
            "total_input": total_in,
            "total_output": total_out,
            "total_cost": total_cost,
            "total_latency_ms": total_ms,
            "per_model": per_model,
        }

    # ---- job search prefs ------------------------------------------------

    def save_job_search_prefs(self, prefs: object) -> None:
        """Upsert 单行搜岗偏好（id=1）。"""
        import json

        if hasattr(prefs, "model_dump_json"):
            payload = prefs.model_dump_json()
        else:
            payload = json.dumps(prefs, ensure_ascii=False)
        with self._lock:
            self._conn.execute(
                "INSERT INTO job_search_prefs(id, payload_json, updated_at) "
                "VALUES (1, ?, datetime('now')) "
                "ON CONFLICT(id) DO UPDATE SET "
                "payload_json = excluded.payload_json, "
                "updated_at = datetime('now')",
                (payload,),
            )
            self._conn.commit()

    def load_job_search_prefs(self) -> object | None:
        """返回 JobSearchPrefs；尚未 set 则 None。"""
        from repo2resume.storage.models import JobSearchPrefs

        with self._lock:
            row = self._conn.execute(
                "SELECT payload_json FROM job_search_prefs WHERE id = 1"
            ).fetchone()
        if row is None:
            return None
        return JobSearchPrefs.model_validate_json(row["payload_json"])

    # ---- layered job search models ---------------------------------------

    def save_search_run(self, run: object) -> str:
        """Upsert 一次 SearchRun，并返回 run_id。"""
        import json

        from repo2resume.storage.models import SearchRun

        if isinstance(run, SearchRun):
            obj = run
        elif hasattr(run, "model_dump_json"):
            obj = SearchRun.model_validate_json(run.model_dump_json())
        else:
            obj = SearchRun.model_validate(run)
        payload = obj.model_dump_json()
        with self._lock:
            self._conn.execute(
                "INSERT INTO job_search_runs("
                "run_id, prefs_version, queries_json, sources_json, "
                "started_at, completed_at, payload_json"
                ") VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(run_id) DO UPDATE SET "
                "prefs_version=excluded.prefs_version, "
                "queries_json=excluded.queries_json, "
                "sources_json=excluded.sources_json, "
                "started_at=excluded.started_at, "
                "completed_at=excluded.completed_at, "
                "payload_json=excluded.payload_json",
                (
                    obj.run_id,
                    obj.prefs_version,
                    json.dumps(obj.queries, ensure_ascii=False),
                    json.dumps(obj.sources, ensure_ascii=False),
                    obj.started_at,
                    obj.completed_at,
                    payload,
                ),
            )
            self._conn.commit()
        return obj.run_id

    def load_latest_search_run(self) -> object | None:
        """返回最新 SearchRun；无则 None。"""
        from repo2resume.storage.models import SearchRun

        with self._lock:
            row = self._conn.execute(
                "SELECT payload_json FROM job_search_runs "
                "ORDER BY started_at DESC, run_id DESC LIMIT 1"
            ).fetchone()
        if row is None:
            return None
        return SearchRun.model_validate_json(row["payload_json"])

    def upsert_job_candidate(self, candidate: object) -> None:
        """Upsert JobCandidate。"""
        from repo2resume.storage.models import JobCandidate

        if isinstance(candidate, JobCandidate):
            obj = candidate
        elif hasattr(candidate, "model_dump_json"):
            obj = JobCandidate.model_validate_json(candidate.model_dump_json())
        else:
            obj = JobCandidate.model_validate(candidate)
        with self._lock:
            self._conn.execute(
                "INSERT INTO job_candidates("
                "candidate_id, run_id, source, url, confidence, verification_status, "
                "last_seen_at, last_verified_at, updated_at, payload_json"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), ?) "
                "ON CONFLICT(candidate_id) DO UPDATE SET "
                "run_id=excluded.run_id, source=excluded.source, url=excluded.url, "
                "confidence=excluded.confidence, "
                "verification_status=excluded.verification_status, "
                "last_seen_at=excluded.last_seen_at, "
                "last_verified_at=excluded.last_verified_at, "
                "updated_at=datetime('now'), "
                "payload_json=excluded.payload_json",
                (
                    obj.candidate_id,
                    obj.run_id,
                    obj.source,
                    obj.url,
                    obj.confidence,
                    obj.verification.status,
                    obj.last_seen_at,
                    obj.last_verified_at,
                    obj.model_dump_json(),
                ),
            )
            self._conn.commit()

    def load_job_candidate(self, candidate_id: str) -> object | None:
        """按 id 读取 JobCandidate。"""
        from repo2resume.storage.models import JobCandidate

        with self._lock:
            row = self._conn.execute(
                "SELECT payload_json FROM job_candidates WHERE candidate_id = ?",
                (candidate_id,),
            ).fetchone()
        if row is None:
            return None
        return JobCandidate.model_validate_json(row["payload_json"])

    def list_job_candidates(self, run_id: str, *, limit: int = 200) -> list[object]:
        """读取某 run 的候选职位。"""
        from repo2resume.storage.models import JobCandidate

        with self._lock:
            rows = self._conn.execute(
                "SELECT payload_json FROM job_candidates WHERE run_id = ? "
                "ORDER BY updated_at DESC LIMIT ?",
                (run_id, max(1, limit)),
            ).fetchall()
        return [JobCandidate.model_validate_json(r["payload_json"]) for r in rows]

    def save_job_snapshot(self, snapshot: object) -> None:
        """插入/覆盖 JobSnapshot。"""
        from repo2resume.storage.models import JobSnapshot

        if isinstance(snapshot, JobSnapshot):
            obj = snapshot
        elif hasattr(snapshot, "model_dump_json"):
            obj = JobSnapshot.model_validate_json(snapshot.model_dump_json())
        else:
            obj = JobSnapshot.model_validate(snapshot)
        with self._lock:
            self._conn.execute(
                "INSERT INTO job_snapshots("
                "snapshot_id, job_id, candidate_id, fetched_at, source, url, content_hash, "
                "completeness, verification_status, payload_json"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(snapshot_id) DO UPDATE SET "
                "job_id=excluded.job_id, candidate_id=excluded.candidate_id, "
                "fetched_at=excluded.fetched_at, source=excluded.source, url=excluded.url, "
                "content_hash=excluded.content_hash, completeness=excluded.completeness, "
                "verification_status=excluded.verification_status, "
                "payload_json=excluded.payload_json",
                (
                    obj.snapshot_id,
                    obj.job_id,
                    obj.candidate_id,
                    obj.fetched_at,
                    obj.source,
                    obj.url,
                    obj.content_hash,
                    obj.completeness,
                    obj.verification_status,
                    obj.model_dump_json(),
                ),
            )
            self._conn.commit()

    def load_latest_job_snapshot(self, job_id: str) -> object | None:
        """读取某 job_id 的最新 JobSnapshot。"""
        from repo2resume.storage.models import JobSnapshot

        with self._lock:
            row = self._conn.execute(
                "SELECT payload_json FROM job_snapshots WHERE job_id = ? "
                "ORDER BY fetched_at DESC, snapshot_id DESC LIMIT 1",
                (job_id,),
            ).fetchone()
        if row is None:
            return None
        return JobSnapshot.model_validate_json(row["payload_json"])

    def explain_job_origin(self, candidate_id: str) -> dict | None:
        """解释候选岗位来源：run/query/source/验证状态。"""
        with self._lock:
            crow = self._conn.execute(
                "SELECT run_id, source, confidence, verification_status, payload_json "
                "FROM job_candidates WHERE candidate_id = ?",
                (candidate_id,),
            ).fetchone()
            if crow is None:
                return None
            rrow = self._conn.execute(
                "SELECT payload_json FROM job_search_runs WHERE run_id = ?",
                (crow["run_id"],),
            ).fetchone()
        return {
            "candidate_id": candidate_id,
            "run_id": crow["run_id"],
            "source": crow["source"],
            "confidence": crow["confidence"],
            "verification_status": crow["verification_status"],
            "candidate_payload_json": crow["payload_json"],
            "run_payload_json": rrow["payload_json"] if rrow is not None else None,
        }

    # ---- resume drafts (Phase 4) -----------------------------------------

    def save_resume_draft(self, draft: object, *, job_id: str | None = None) -> int:
        """插入一条 ResumeDraft JSON，返回 row id。"""
        import json

        if hasattr(draft, "model_dump_json"):
            payload = draft.model_dump_json()
            job_id = job_id or getattr(draft, "job_id", None)
        else:
            payload = json.dumps(draft, ensure_ascii=False)
        cur = self._conn.execute(
            "INSERT INTO resume_drafts(job_id, payload_json) VALUES (?, ?)",
            (job_id, payload),
        )
        self._conn.commit()
        return int(cur.lastrowid)

    def load_latest_resume_draft(self) -> tuple[int, object] | None:
        """返回 (id, ResumeDraft)；无草稿则 None。"""
        from repo2resume.storage.models import ResumeDraft

        row = self._conn.execute(
            "SELECT id, payload_json FROM resume_drafts ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        return int(row["id"]), ResumeDraft.model_validate_json(row["payload_json"])

    # ---- repo stats bundles (简历索引 / 事实清单) ---------------------------

    def save_repo_stats_bundle(self, stats: object) -> int:
        """插入一条 RepoStatsBundle JSON，返回 row id。"""
        from repo2resume.analysis.cache import profile_cache_key

        if hasattr(stats, "model_dump_json"):
            payload = stats.model_dump_json()
            stats_hash = profile_cache_key(stats) if hasattr(stats, "repos") else None
        else:
            import json

            payload = json.dumps(stats, ensure_ascii=False)
            stats_hash = None
        cur = self._conn.execute(
            "INSERT INTO repo_stats_bundles(stats_hash, payload_json) VALUES (?, ?)",
            (stats_hash, payload),
        )
        self._conn.commit()
        return int(cur.lastrowid)

    def load_latest_repo_stats_bundle(self) -> object | None:
        """返回最新 RepoStatsBundle；无则 None。"""
        from repo2resume.storage.models import RepoStatsBundle

        row = self._conn.execute(
            "SELECT payload_json FROM repo_stats_bundles ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        return RepoStatsBundle.model_validate_json(row["payload_json"])


def open_db(path: Path) -> Database:
    """工厂：打开/创建指定路径的 SQLite 数据库并自动跑迁移。"""
    return Database(path)
