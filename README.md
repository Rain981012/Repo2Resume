# Repo2Resume

CLI agent: analyze local git repos → skill profile → matching jobs → tailored Markdown resume.

> Status: Phase 0 skeleton. Phase A skill prototype lives under `skill/`.

## Requirements

- Python 3.11+
- Docker (optional, for Redis). Without Redis the CLI falls back to SQLite cache.

## Install

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

## Quick start

```bash
# Optional: Redis for primary cache
docker compose up -d

# Config wizard (API key, name, email, model…)
repo2resume init

# Or non-interactive (reads REPO2RESUME_* env vars)
export REPO2RESUME_LLM_API_KEY=...
repo2resume init --non-interactive
```

Default model: `zai/glm-5.2` with fallback `zai/glm-4.5-flash` (quota exhausted → free tier).
Config: `~/.repo2resume/config.toml`.

```toml
llm_model = "zai/glm-5.2"
llm_fallback_model = "zai/glm-4.5-flash"
```

## Analyze (Phase 1)

Paths are **local git directories** (not GitHub URLs). With no args, scans `./local_repos/`.
Authors are discovered from git history — you pick from a list (or pass `--author` for scripts).

```bash
# Interactive: list authors → select → analyze
repo2resume analyze --stats-only
repo2resume analyze -o profile.json

# Non-interactive (CI / scripts)
repo2resume analyze --author you@email.com --stats-only
```

## Commands

| Command | Phase |
|---------|-------|
| `repo2resume init` | 0 |
| `repo2resume analyze` | 1 |
| `repo2resume chat` | 2 |
| `repo2resume jobs` | 3 |
| `repo2resume resume` / `export` | 4 |
| `repo2resume evals run` | 5 |

## Dev

```bash
ruff check src tests
ruff format src tests
pytest
```

## Design

See [`docs/design_docs/DESIGN.md`](docs/design_docs/DESIGN.md) and [`docs/design_docs/MVP_PLAN.md`](docs/design_docs/MVP_PLAN.md).
