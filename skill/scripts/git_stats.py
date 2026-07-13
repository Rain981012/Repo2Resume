#!/usr/bin/env python3
"""git_stats.py — 遍历本地 git 仓库，输出结构化贡献统计 JSON。

仅依赖 Python 标准库和 git 命令行。这份 JSON 是简历生成流程的「事实清单」
(fact sheet)：下游 LLM 只能引用其中的数字，不得编造。

用法:
    python3 git_stats.py REPO [REPO ...] [--author PATTERN ...]
                         [--since YYYY-MM-DD] [--top-commits N] [--output FILE]

    --author       按作者过滤（匹配 git author name/email 子串；可多次传入，
                   用于同一人多个邮箱）。不传则统计全部作者。
    --since        只统计该日期之后的 commit。
    --top-commits  每仓库保留的最近 commit 标题数（默认 30）。
    --output       写入文件；缺省打印到 stdout。

示例:
    python3 git_stats.py ~/code/proj-a ~/code/proj-b \\
        --author me@example.com --author "Rain" --output stats.json

输出要点（供 Skill / 下游消费）:
    - summary.overall_language_share  跨仓语言占比（已排除 Markdown 等非代码）
    - repos[].author_commits / author_share  个人贡献量与占比
    - repos[].warning / 顶层 errors         过滤未命中或分析失败时的信号
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections import Counter, defaultdict
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# 常量：扩展名 → 语言、噪声过滤、Conventional Commits
# ---------------------------------------------------------------------------

# 路径扩展名到展示用语言名；未列出的扩展名在统计中忽略
EXT_TO_LANG = {
    ".py": "Python", ".ipynb": "Jupyter", ".js": "JavaScript", ".jsx": "JavaScript",
    ".ts": "TypeScript", ".tsx": "TypeScript", ".vue": "Vue", ".svelte": "Svelte",
    ".go": "Go", ".rs": "Rust", ".java": "Java", ".kt": "Kotlin", ".scala": "Scala",
    ".c": "C", ".h": "C/C++ Header", ".cpp": "C++", ".cc": "C++", ".hpp": "C/C++ Header",
    ".cs": "C#", ".rb": "Ruby", ".php": "PHP", ".swift": "Swift", ".m": "Objective-C",
    ".dart": "Dart", ".lua": "Lua", ".r": "R", ".jl": "Julia", ".ex": "Elixir",
    ".exs": "Elixir", ".erl": "Erlang", ".hs": "Haskell", ".ml": "OCaml",
    ".sh": "Shell", ".bash": "Shell", ".zsh": "Shell", ".ps1": "PowerShell",
    ".sql": "SQL", ".proto": "Protobuf", ".graphql": "GraphQL",
    ".html": "HTML", ".css": "CSS", ".scss": "CSS", ".less": "CSS",
    ".yaml": "YAML", ".yml": "YAML", ".toml": "TOML", ".json": "JSON",
    ".md": "Markdown", ".rst": "reStructuredText", ".tex": "TeX",
    ".tf": "Terraform", ".dockerfile": "Docker",
}

# 计入 languages 明细，但不参与 overall / share 分母（避免 README 刷高「语言占比」）
NON_CODE_LANGS = {"Markdown", "reStructuredText", "JSON", "YAML", "TOML", "TeX"}

# 依赖目录、构建产物：不计入 lines_added
SKIP_PATH_PARTS = {
    "node_modules", "vendor", "dist", "build", ".venv", "venv",
    "__pycache__", "third_party", "generated",
}
SKIP_FILENAMES = {
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock",
    "uv.lock", "Cargo.lock", "Gemfile.lock", "composer.lock", "go.sum",
}
SKIP_SUFFIXES = (".min.js", ".min.css", ".map", ".svg", ".lock")

# Conventional Commits：feat(scope): msg → 归入 feat；否则记为 other
CONVENTIONAL_RE = re.compile(
    r"^(feat|fix|refactor|perf|test|docs|build|ci|chore|style|revert)(\(.+?\))?!?:",
    re.IGNORECASE,
)

# 由 @manifest 装饰器填充：文件名 → 解析函数
MANIFEST_PARSERS: dict[str, Callable[[Path], list[str]]] = {}


def manifest(filename: str):
    """注册「仓库根目录某依赖清单文件」的解析器。"""
    def deco(fn):
        MANIFEST_PARSERS[filename] = fn
        return fn
    return deco


# ---------------------------------------------------------------------------
# Git 辅助
# ---------------------------------------------------------------------------

def run_git(repo: Path, *args: str) -> str:
    """在 repo 目录执行 git 子命令，成功返回 stdout；失败抛 RuntimeError。"""
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, timeout=300,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} 失败: {result.stderr.strip()[:200]}")
    return result.stdout


def author_args(authors: list[str]) -> list[str]:
    """把作者列表转成 git 的 --author= 参数。

    多个 --author 在 git 里是 OR：适合同一人学校邮箱 + GitHub noreply。
    """
    return [f"--author={a}" for a in authors]


def should_skip(path: str) -> bool:
    """是否跳过该路径的行数统计（依赖树、lock、压缩资源等）。"""
    parts = path.split("/")
    if any(p in SKIP_PATH_PARTS for p in parts):
        return True
    name = parts[-1]
    return name in SKIP_FILENAMES or name.endswith(SKIP_SUFFIXES)


def lang_of(path: str) -> str | None:
    """由文件路径推断语言；无法识别则返回 None（不计入语言统计）。"""
    name = path.rsplit("/", 1)[-1]
    if name == "Dockerfile":
        return "Docker"
    dot = name.rfind(".")
    if dot <= 0:
        return None
    return EXT_TO_LANG.get(name[dot:].lower())


# ---------------------------------------------------------------------------
# 依赖清单解析（尽力而为：缺文件或解析失败 → 空列表，不中断主流程）
# ---------------------------------------------------------------------------

@manifest("requirements.txt")
def _parse_requirements(p: Path) -> list[str]:
    deps = []
    for line in p.read_text(errors="ignore").splitlines():
        line = line.strip()
        if line and not line.startswith(("#", "-")):
            # 去掉版本约束：django>=4.0 → django
            deps.append(re.split(r"[<>=!~\[; ]", line)[0])
    return deps


@manifest("pyproject.toml")
def _parse_pyproject(p: Path) -> list[str]:
    try:
        import tomllib  # Python 3.11+
        data = tomllib.loads(p.read_text(errors="ignore"))
    except Exception:
        return []
    # PEP 621 project.dependencies + Poetry tool.poetry.dependencies
    deps = list(data.get("project", {}).get("dependencies", []))
    poetry = data.get("tool", {}).get("poetry", {}).get("dependencies", {})
    deps += [k for k in poetry if k.lower() != "python"]
    return [re.split(r"[<>=!~\[; ]", d)[0] for d in deps]


@manifest("package.json")
def _parse_package_json(p: Path) -> list[str]:
    try:
        data = json.loads(p.read_text(errors="ignore"))
    except Exception:
        return []
    return list(data.get("dependencies", {})) + list(data.get("devDependencies", {}))


@manifest("go.mod")
def _parse_go_mod(p: Path) -> list[str]:
    deps = []
    for line in p.read_text(errors="ignore").splitlines():
        # 匹配 require 块里的 module 路径（简化版）
        m = re.match(r"\s*([\w.\-/]+\.[\w.\-/]+)\s+v[\d.]", line)
        if m:
            deps.append(m.group(1))
    return deps


@manifest("Cargo.toml")
def _parse_cargo(p: Path) -> list[str]:
    try:
        import tomllib
        data = tomllib.loads(p.read_text(errors="ignore"))
    except Exception:
        return []
    return list(data.get("dependencies", {}))


@manifest("Gemfile")
def _parse_gemfile(p: Path) -> list[str]:
    return re.findall(r"^\s*gem\s+['\"]([\w\-]+)['\"]", p.read_text(errors="ignore"), re.M)


@manifest("composer.json")
def _parse_composer(p: Path) -> list[str]:
    try:
        data = json.loads(p.read_text(errors="ignore"))
    except Exception:
        return []
    return [k for k in data.get("require", {}) if k != "php"]


def collect_dependencies(repo: Path) -> dict[str, list[str]]:
    """扫描仓库根目录已知清单文件，返回 {文件名: 去重后的依赖名列表}。

    每种清单最多保留 80 个包名，避免 JSON 过大。
    """
    found = {}
    for fname, parser in MANIFEST_PARSERS.items():
        f = repo / fname
        if f.is_file():
            try:
                deps = parser(f)
            except Exception:
                deps = []
            if deps:
                found[fname] = sorted(set(deps))[:80]
    return found


# ---------------------------------------------------------------------------
# 单仓库分析
# ---------------------------------------------------------------------------

def count_commits(repo: Path, filters: list[str]) -> int:
    """统计 commit 数。

    使用 --no-merges，与下方 git log --numstat 口径一致，避免 merge 把计数抬高。
    filters 通常含 --author= / --since=。
    """
    out = run_git(repo, "rev-list", "--count", "--no-merges", "HEAD", *filters)
    return int(out.strip() or 0)


def analyze_repo(repo: Path, authors: list[str], since: str | None, top_commits: int) -> dict:
    """分析单个仓库，返回可写入 JSON 的 dict。

    若指定了 authors 且命中 0 条 commit，仍返回基础字段 + warning，不抛错，
    方便 Skill 提示用户核对 --author。
    """
    filters = author_args(authors)
    if since:
        filters.append(f"--since={since}")

    # total：仓内全部（可带 since）；my：再叠加 author 过滤
    total_commits = count_commits(repo, [f"--since={since}"] if since else [])
    my_commits = count_commits(repo, filters) if authors else total_commits

    info: dict = {
        "name": repo.name,
        "path": str(repo),
        "head_commit": run_git(repo, "rev-parse", "--short", "HEAD").strip(),
        "total_commits": total_commits,
        "author_commits": my_commits,
        "author_share": round(my_commits / total_commits, 3) if total_commits else 0,
        "contributors": len(run_git(repo, "shortlog", "-sn", "HEAD").splitlines()),
    }
    try:
        info["remote"] = run_git(repo, "remote", "get-url", "origin").strip()
    except RuntimeError:
        info["remote"] = None

    if my_commits == 0:
        info["warning"] = "该作者在此仓库没有 commit(检查 --author 拼写或邮箱)"
        return info

    # 一次 log：用 @@ 分隔 commit；每段首行 header，后接 numstat（增删行\\t路径）
    log = run_git(
        repo, "log", *filters, "--date=format:%Y-%m", "--no-merges",
        "--pretty=format:@@%H|%ad|%s", "--numstat",
    )

    monthly: Counter = Counter()
    type_counts: Counter = Counter()
    subjects: list[str] = []
    lang_lines: Counter = Counter()          # 语言 → 新增行数累计
    lang_files: defaultdict = defaultdict(set)
    dir_lines: Counter = Counter()           # 顶层目录 → 新增行数（看贡献热点）
    first_month = last_month = None

    for block in log.split("@@"):
        if not block.strip():
            continue
        header, *stat_lines = block.strip().splitlines()
        try:
            _sha, month, subject = header.split("|", 2)
        except ValueError:
            continue

        monthly[month] += 1
        # git log 默认新→旧：第一次见到的是 last_month，最后一次是 first_month
        last_month = last_month or month
        first_month = month

        m = CONVENTIONAL_RE.match(subject)
        type_counts[m.group(1).lower() if m else "other"] += 1
        if len(subjects) < top_commits:
            subjects.append(subject[:120])

        for line in stat_lines:
            parts = line.split("\t")
            # numstat：added\\tdeleted\\tpath；二进制文件 added/deleted 为 "-"
            if len(parts) != 3 or parts[0] == "-":
                continue
            added, _deleted, fpath = parts
            # rename：取新路径（支持 dir/{old => new}/file 与 old => new）
            if "=>" in fpath:
                fpath = re.sub(r"\{[^{}]* => ([^{}]*)\}", r"\1", fpath)
                fpath = fpath.split(" => ")[-1] if " => " in fpath else fpath
                fpath = fpath.replace("//", "/")
            if should_skip(fpath):
                continue
            lang = lang_of(fpath)
            if not lang:
                continue
            n = int(added)
            lang_lines[lang] += n
            lang_files[lang].add(fpath)
            top_dir = fpath.split("/")[0] if "/" in fpath else "(root)"
            dir_lines[top_dir] += n

    # share 只对「代码语言」归一化；文档类 share 为 null
    code_total = sum(v for k, v in lang_lines.items() if k not in NON_CODE_LANGS)
    info["languages"] = {
        lang: {
            "lines_added": lines,
            "files_touched": len(lang_files[lang]),
            "share": round(lines / code_total, 3) if code_total and lang not in NON_CODE_LANGS else None,
        }
        for lang, lines in lang_lines.most_common()
    }
    info["active_from"] = first_month
    info["active_to"] = last_month
    info["active_months"] = len(monthly)
    info["monthly_commits"] = dict(sorted(monthly.items()))
    info["commit_types"] = dict(type_counts.most_common())
    info["top_directories"] = dict(dir_lines.most_common(10))
    info["recent_commit_subjects"] = subjects
    info["dependencies"] = collect_dependencies(repo)

    for readme in ("README.md", "README.rst", "readme.md", "README"):
        f = repo / readme
        if f.is_file():
            # 截断避免把整篇长文档塞进 LLM 上下文
            info["readme_excerpt"] = f.read_text(errors="ignore")[:800]
            break

    return info


# ---------------------------------------------------------------------------
# 跨仓汇总与 CLI 入口
# ---------------------------------------------------------------------------

def build_summary(repos: list[dict]) -> dict:
    """跨仓库汇总：仓数、个人总 commit、整体语言占比。"""
    lang_totals: Counter = Counter()
    for r in repos:
        for lang, d in r.get("languages", {}).items():
            if lang not in NON_CODE_LANGS:
                lang_totals[lang] += d["lines_added"]
    total = sum(lang_totals.values())
    return {
        "repo_count": len(repos),
        "total_author_commits": sum(r.get("author_commits", 0) for r in repos),
        "overall_language_share": {
            lang: round(v / total, 3) for lang, v in lang_totals.most_common()
        } if total else {},
    }


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("repos", nargs="+", help="git 仓库路径（可多个）")
    ap.add_argument("--author", action="append", default=[], help="作者 name/email 子串，可多次")
    ap.add_argument("--since", default=None, help="只统计该日期后的 commit，如 2022-01-01")
    ap.add_argument("--top-commits", type=int, default=30)
    ap.add_argument("--output", default=None, help="输出 JSON 文件路径；缺省 stdout")
    args = ap.parse_args()

    results, errors = [], []
    for raw in args.repos:
        repo = Path(raw).expanduser().resolve()
        if not (repo / ".git").exists():
            errors.append({"path": str(repo), "error": "不是 git 仓库"})
            continue
        try:
            results.append(analyze_repo(repo, args.author, args.since, args.top_commits))
        except Exception as e:
            # 单仓失败不拖垮整批：记入 errors，其余仓继续
            errors.append({"path": str(repo), "error": str(e)[:300]})

    doc = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "author_filters": args.author or None,
        "since": args.since,
        "summary": build_summary(results),
        "repos": results,
        "errors": errors,
    }
    out = json.dumps(doc, ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).write_text(out)
        print(f"已写入 {args.output}（{len(results)} 个仓库，{len(errors)} 个错误）")
    else:
        print(out)
    if errors:
        print(
            f"警告：{len(errors)} 个仓库分析失败，详见输出中的 errors 字段",
            file=sys.stderr,
        )


if __name__ == "__main__":
    main()
