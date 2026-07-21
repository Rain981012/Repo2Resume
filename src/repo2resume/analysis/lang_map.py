"""Extension → language map and path skip rules (shared by miner / detector)."""

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
    parts = path.split("/")
    if any(p in SKIP_PATH_PARTS for p in parts):
        return True
    name = parts[-1]
    return name in SKIP_FILENAMES or name.endswith(SKIP_SUFFIXES)


def lang_of(path: str) -> str | None:
    name = path.rsplit("/", 1)[-1]
    if name == "Dockerfile":
        return "Docker"
    dot = name.rfind(".")
    if dot <= 0:
        return None
    return EXT_TO_LANG.get(name[dot:].lower())
