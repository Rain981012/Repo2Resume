"""文件扩展名 → 编程语言 映射表，以及路径跳过规则。

本模块被 `git_miner`（统计代码行数按语言归类）和 `tech_detector`（识别技术栈）共享，
统一管理「哪些文件算代码、算什么语言、哪些路径要忽略」的规则，避免两处各写一份漂移。
"""

from __future__ import annotations

EXT_TO_LANG = {
    ".py": "Python",
    ".ipynb": "Jupyter",
    ".js": "JavaScript",
    ".jsx": "JavaScript",
    ".ts": "TypeScript",
    ".tsx": "TypeScript",
    ".vue": "Vue",
    ".svelte": "Svelte",
    ".go": "Go",
    ".rs": "Rust",
    ".java": "Java",
    ".kt": "Kotlin",
    ".scala": "Scala",
    ".c": "C",
    ".h": "C/C++ Header",
    ".cpp": "C++",
    ".cc": "C++",
    ".hpp": "C/C++ Header",
    ".cs": "C#",
    ".rb": "Ruby",
    ".php": "PHP",
    ".swift": "Swift",
    ".m": "Objective-C",
    ".dart": "Dart",
    ".lua": "Lua",
    ".r": "R",
    ".jl": "Julia",
    ".ex": "Elixir",
    ".exs": "Elixir",
    ".erl": "Erlang",
    ".hs": "Haskell",
    ".ml": "OCaml",
    ".sh": "Shell",
    ".bash": "Shell",
    ".zsh": "Shell",
    ".ps1": "PowerShell",
    ".sql": "SQL",
    ".proto": "Protobuf",
    ".graphql": "GraphQL",
    ".html": "HTML",
    ".css": "CSS",
    ".scss": "CSS",
    ".less": "CSS",
    ".yaml": "YAML",
    ".yml": "YAML",
    ".toml": "TOML",
    ".json": "JSON",
    ".md": "Markdown",
    ".rst": "reStructuredText",
    ".tex": "TeX",
    ".tf": "Terraform",
    ".dockerfile": "Docker",
}

NON_CODE_LANGS = {"Markdown", "reStructuredText", "JSON", "YAML", "TOML", "TeX"}

SKIP_PATH_PARTS = {
    "node_modules",
    "vendor",
    "dist",
    "build",
    ".venv",
    "venv",
    "__pycache__",
    "third_party",
    "generated",
}
SKIP_FILENAMES = {
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "poetry.lock",
    "uv.lock",
    "Cargo.lock",
    "Gemfile.lock",
    "composer.lock",
    "go.sum",
}
SKIP_SUFFIXES = (".min.js", ".min.css", ".map", ".svg", ".lock")


def should_skip(path: str) -> bool:
    """判断某文件路径是否应被排除统计（依赖目录 / lock 文件 / 压缩产物）。"""
    parts = path.split("/")
    if any(p in SKIP_PATH_PARTS for p in parts):
        return True
    name = parts[-1]
    return name in SKIP_FILENAMES or name.endswith(SKIP_SUFFIXES)


def lang_of(path: str) -> str | None:
    """按文件名扩展名推断语言；Dockerfile 无扩展名单独识别；无法识别返回 None。"""
    name = path.rsplit("/", 1)[-1]
    if name == "Dockerfile":
        return "Docker"
    dot = name.rfind(".")
    if dot <= 0:
        return None
    return EXT_TO_LANG.get(name[dot:].lower())
