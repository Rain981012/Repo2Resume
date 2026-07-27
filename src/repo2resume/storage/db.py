"""SQLite 持久化：技能画像、简历草稿、chat 会话、LLM trace 的统一存储。

用版本化迁移（`MIGRATIONS` dict）建表，`schema_migrations` 表记录已应用版本，
`Database.migrate` 启动时自动补齐未应用的迁移。chat 会话相关方法供 agent `chat` 命令做会话持久化。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_VERSION = 4

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
}


class Database:
    """SQLite 连接 + 版本化迁移 + 会话读写。row_factory=Row 方便按列名取值。"""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self.migrate()

    @property
    def conn(self) -> sqlite3.Connection:
        """暴露底层连接，供 pipeline 等直接写表（如 skill_profiles）。"""
        return self._conn

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

        self._conn.execute(
            "INSERT INTO tool_traces"
            "(session_id, tool_name, args_json, result_json, error, latency_ms) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                session_id,
                tool_name,
                _dumps(args),
                _dumps(result) if error is None else None,
                str(error) if error is not None else None,
                latency_ms,
            ),
        )
        self._conn.commit()

    def tool_trace_stats(self, session_id: str) -> dict:
        """聚合某 session 的工具调用统计，供 /cost 命令展示。

        返回: {count, total_ms, errors, per_tool: {name: {count, ms, errors}}}
        """
        rows = self._conn.execute(
            "SELECT tool_name, COUNT(*) AS c, COALESCE(SUM(latency_ms), 0) AS ms, "
            "SUM(CASE WHEN error IS NOT NULL THEN 1 ELSE 0 END) AS errs "
            "FROM tool_traces WHERE session_id = ? GROUP BY tool_name",
            (session_id,),
        ).fetchall()
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

        self._conn.execute(
            "INSERT INTO llm_traces"
            "(session_id, model, input_tokens, output_tokens, cost_usd, latency_ms, payload_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                session_id,
                model,
                input_tokens,
                output_tokens,
                cost_usd,
                latency_ms,
                _dumps(payload),
            ),
        )
        self._conn.commit()

    def llm_trace_stats(self, session_id: str) -> dict:
        """聚合某 session 的 LLM 调用统计，供 /cost 展示成本。

        返回: {count, total_input, total_output, total_cost, total_latency_ms, per_model: {...}}
        """
        rows = self._conn.execute(
            "SELECT model, COUNT(*) AS c, "
            "COALESCE(SUM(input_tokens), 0) AS ti, COALESCE(SUM(output_tokens), 0) AS to_, "
            "COALESCE(SUM(cost_usd), 0) AS cost, COALESCE(SUM(latency_ms), 0) AS ms "
            "FROM llm_traces WHERE session_id = ? GROUP BY model",
            (session_id,),
        ).fetchall()
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


def open_db(path: Path) -> Database:
    """工厂：打开/创建指定路径的 SQLite 数据库并自动跑迁移。"""
    return Database(path)
