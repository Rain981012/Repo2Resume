"""用 PyDriller + git CLI 挖本地仓库统计，产出 `RepoStatsBundle`。

核心是 `mine_one`：对单仓库跑 `git rev-list` 数 commit、PyDriller 遍历非 merge commit
按作者/日期过滤，累加各语言新增行数、月度提交、commit 类型、活跃月份等，组装成
`ProjectSummary`。`mine_repos` 聚合多仓 + 失败隔离。作者过滤用 name|email 子串匹配，
与 Phase A1 的口径一致。
"""

from __future__ import annotations

import logging
import re
import subprocess
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from pydriller import Repository

from repo2resume.analysis.lang_map import NON_CODE_LANGS, lang_of, should_skip
from repo2resume.analysis.tech_detector import collect_dependencies
from repo2resume.storage.models import (
    LanguageStat,
    ProjectSummary,
    RepoStatsBundle,
    StatsSummary,
)

logger = logging.getLogger(__name__)

CONVENTIONAL_RE = re.compile(
    r"^(feat|fix|refactor|perf|test|docs|build|ci|chore|style|revert)(\(.+?\))?!?:",
    re.IGNORECASE,
)


@dataclass
class MineOptions:
    """挖矿参数：作者过滤列表、起始日期、保留的最近 commit subject 数量。"""

    authors: list[str] = field(default_factory=list)
    since: str | None = None  # YYYY-MM-DD
    top_commits: int = 30


@dataclass(frozen=True)
class AuthorInfo:
    """跨仓库聚合后的作者信息：名字、邮箱、commit 数、活跃仓库名集合。"""

    name: str
    email: str
    commits: int
    repos: tuple[str, ...] = ()

    @property
    def display(self) -> str:
        if self.email:
            return f"{self.name} <{self.email}>"
        return self.name

    @property
    def filter_value(self) -> str:
        """传给 git --author 的首选值：有邮箱用邮箱，否则用名字。"""
        return self.email or self.name


_SHORTLOG_RE = re.compile(r"^\s*(\d+)\s+(.+?)\s+<([^>]+)>\s*$")


def collect_authors(repos: list[Path]) -> list[AuthorInfo]:
    """Aggregate authors across repos via `git shortlog -sne`."""
    agg: dict[tuple[str, str], dict[str, object]] = {}
    for repo in repos:
        try:
            out = run_git(repo, "shortlog", "-sne", "--all", "--no-merges")
        except RuntimeError as exc:
            logger.warning("shortlog failed for %s: %s", repo, exc)
            continue
        for line in out.splitlines():
            m = _SHORTLOG_RE.match(line)
            if not m:
                continue
            count = int(m.group(1))
            name = m.group(2).strip()
            email = m.group(3).strip()
            key = (name.lower(), email.lower())
            slot = agg.get(key)
            if slot is None:
                agg[key] = {
                    "name": name,
                    "email": email,
                    "commits": count,
                    "repos": {repo.name},
                }
            else:
                slot["commits"] = int(slot["commits"]) + count
                repos_set = slot["repos"]
                assert isinstance(repos_set, set)
                repos_set.add(repo.name)

    authors = [
        AuthorInfo(
            name=str(v["name"]),
            email=str(v["email"]),
            commits=int(v["commits"]),  # type: ignore[arg-type]
            repos=tuple(sorted(v["repos"])),  # type: ignore[arg-type]
        )
        for v in agg.values()
    ]
    authors.sort(key=lambda a: (-a.commits, a.name.lower(), a.email.lower()))
    return authors


def run_git(repo: Path, *args: str) -> str:
    """在 `repo` 目录跑 `git` 子进程，失败抛 `RuntimeError`（带截断的 stderr）。"""
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()[:200]}")
    return result.stdout


def short_head(repo: Path) -> str:
    """返回仓库 HEAD 的短 commit hash，用于缓存键和统计标识。"""
    return run_git(repo, "rev-parse", "--short", "HEAD").strip()


def _parse_since(since: str | None) -> datetime | None:
    """把 `YYYY-MM-DD` 解析成 UTC datetime；空值或坏值（含模型传的 "null"）返回 None。"""
    if not since:
        return None
    try:
        return datetime.strptime(since, "%Y-%m-%d").replace(tzinfo=UTC)
    except ValueError:
        # 容错：模型可能传 "null" / 空串 / 非日期字符串；坏值当作不过滤
        return None


def _author_match(name: str, email: str, patterns: list[str]) -> bool:
    """A1-compatible: substring match on author name OR email (case-insensitive)."""
    if not patterns:
        return True
    hay = f"{name} <{email}>".lower()
    return any(p.lower() in hay for p in patterns)


def _count_commits(repo: Path, *, authors: list[str], since: str | None) -> tuple[int, int]:
    """用 `git rev-list --count` 快速返回 (总 commit 数, 作者命中 commit 数)。"""
    base = ["rev-list", "--count", "--no-merges", "HEAD"]
    since_args = [f"--since={since}"] if since else []
    total = int(run_git(repo, *base, *since_args).strip() or 0)
    if not authors:
        return total, total
    # git --author is OR across repeated flags; substring match
    author_args = [f"--author={a}" for a in authors]
    mine = int(run_git(repo, *base, *since_args, *author_args).strip() or 0)
    return total, mine


def mine_one(repo: Path, options: MineOptions) -> ProjectSummary:
    """挖单个仓库：数 commit/份额 → PyDriller 遍历按作者过滤 → 累加语言/月度/类型
    → 组装 `ProjectSummary`。"""
    repo = repo.expanduser().resolve()
    if not (repo / ".git").exists() and not (repo / ".git").is_file():
        raise RuntimeError(f"not a git repository: {repo}")

    head = short_head(repo)
    total_commits, author_commits = _count_commits(
        repo, authors=options.authors, since=options.since
    )
    contributors = len(run_git(repo, "shortlog", "-sn", "HEAD").splitlines())
    try:
        remote = run_git(repo, "remote", "get-url", "origin").strip()
    except RuntimeError:
        remote = None

    info = ProjectSummary(
        name=repo.name,
        path=str(repo),
        head_commit=head,
        total_commits=total_commits,
        author_commits=author_commits,
        author_share=round(author_commits / total_commits, 3) if total_commits else 0.0,
        contributors=contributors,
        remote=remote,
        dependencies=collect_dependencies(repo),
    )

    if author_commits == 0 and options.authors:
        info.warning = "该作者在此仓库没有 commit（检查 --author 拼写或邮箱）"
        return info

    since_dt = _parse_since(options.since)
    monthly: Counter[str] = Counter()
    type_counts: Counter[str] = Counter()
    subjects: list[str] = []
    lang_lines: Counter[str] = Counter()
    lang_files: dict[str, set[str]] = defaultdict(set)
    dir_lines: Counter[str] = Counter()
    first_month = last_month = None

    # PyDriller: only_no_merge + since; author filter done manually (name|email substring)
    for commit in Repository(
        str(repo),
        since=since_dt,
        only_no_merge=True,
        num_workers=1,
    ).traverse_commits():
        name = commit.author.name or ""
        email = commit.author.email or ""
        if not _author_match(name, email, options.authors):
            continue

        month = commit.author_date.strftime("%Y-%m")
        monthly[month] += 1
        last_month = last_month or month
        first_month = month

        subject = (commit.msg or "").splitlines()[0][:120]
        m = CONVENTIONAL_RE.match(subject)
        type_counts[m.group(1).lower() if m else "other"] += 1
        if len(subjects) < options.top_commits:
            subjects.append(subject)

        for mod in commit.modified_files:
            fpath = mod.new_path or mod.old_path
            if not fpath or should_skip(fpath):
                continue
            added = mod.added_lines or 0
            if added <= 0:
                continue
            lang = lang_of(fpath)
            if not lang:
                continue
            lang_lines[lang] += added
            lang_files[lang].add(fpath)
            top_dir = fpath.split("/")[0] if "/" in fpath else "(root)"
            dir_lines[top_dir] += added

    code_total = sum(v for k, v in lang_lines.items() if k not in NON_CODE_LANGS)
    info.languages = {
        lang: LanguageStat(
            lines_added=lines,
            files_touched=len(lang_files[lang]),
            share=(
                round(lines / code_total, 3) if code_total and lang not in NON_CODE_LANGS else None
            ),
        )
        for lang, lines in lang_lines.most_common()
    }
    info.active_from = first_month
    info.active_to = last_month
    info.active_months = len(monthly)
    info.monthly_commits = dict(sorted(monthly.items()))
    info.commit_types = dict(type_counts.most_common())
    info.top_directories = dict(dir_lines.most_common(10))
    info.recent_commit_subjects = subjects

    for readme in ("README.md", "README.rst", "readme.md", "README"):
        f = repo / readme
        if f.is_file():
            info.readme_excerpt = f.read_text(errors="ignore")[:800]
            break

    return info


def build_summary(repos: list[ProjectSummary]) -> StatsSummary:
    """跨仓库汇总：仓库数、作者总 commit 数、按新增行数加权的全局语言份额。"""
    lang_totals: Counter[str] = Counter()
    for r in repos:
        for lang, stat in r.languages.items():
            if lang not in NON_CODE_LANGS:
                lang_totals[lang] += stat.lines_added
    total = sum(lang_totals.values())
    return StatsSummary(
        repo_count=len(repos),
        total_author_commits=sum(r.author_commits for r in repos),
        overall_language_share=(
            {lang: round(v / total, 3) for lang, v in lang_totals.most_common()} if total else {}
        ),
    )


def mine_repos(paths: list[Path], options: MineOptions) -> RepoStatsBundle:
    """逐仓库调 `mine_one`，单仓失败记进 errors 不中断，最后聚合成 `RepoStatsBundle`。"""
    results: list[ProjectSummary] = []
    errors: list[dict[str, str]] = []
    total = len(paths)
    for i, raw in enumerate(paths, 1):
        repo = Path(raw).expanduser().resolve()
        try:
            from repo2resume.agent.progress import emit_progress

            emit_progress(f"正在分析仓库 [{i}/{total}] {repo.name}…")
        except ImportError:  # pragma: no cover
            pass
        try:
            results.append(mine_one(repo, options))
        except Exception as exc:  # noqa: BLE001 — per-repo isolation
            logger.warning("mine failed for %s: %s", repo, exc)
            errors.append({"path": str(repo), "error": str(exc)[:300]})

    return RepoStatsBundle(
        generated_at=datetime.now(UTC).isoformat(timespec="seconds"),
        author_filters=list(options.authors),
        since=options.since,
        summary=build_summary(results),
        repos=results,
        errors=errors,
    )
