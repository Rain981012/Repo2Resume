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

## Embedding 模型选型

Repo2Resume 用 embedding 把项目素材切片编码进本地向量库（ChromaDB），供混合检索 + rerank 使用。
默认 `embed_model = "local:Qwen/Qwen3-Embedding-0.6B"`，可按需切换。

### 可选配置

| 配置 | 模型大小 | 首次下载 | 后续建索引 | 内存 | 适用场景 |
|---|---|---|---|---|---|
| `local:Qwen/Qwen3-Embedding-0.6B`（默认） | ~1.2GB | 1–3 分钟 | 10–30 秒 | 2–3GB | 中英 + 代码项目；32K 上下文不截断；甜点档 |
| `local:Qwen3-Embedding-4B`（高精度档） | ~8GB | 10–25 分钟 | 30 秒–5 分钟 | 8–16GB | 大仓库 / 有 GPU / 学习对比实验 |
| `openai/text-embedding-3-small`（未实现，见 `embedder.py` 的 `build_embedder`） | 0 | 0 | <1 秒 | 0 | 不想下载模型、有 OpenAI key、不在意代码上传 |

### 为什么默认是 Qwen3-Embedding-0.6B？

本项目检索对象是 git 仓库素材（commit message、diff、tech_stack、readme），有三个刚需决定了默认选择：

- **中文支持**：项目里有大量中文（简历、复盘文档、commit message、README）。Qwen3-0.6B 原生支持 100+ 语言，CMTEB-R 中文检索 71.02 分——纯英文模型（如 bge-small-en）对中文基本是「瞎编码」，不适合本项目。
- **代码语义**：这是 git 仓库分析工具，检索对象含大量代码标识符（`FastAPI`、`Kubernetes`、`asyncio`）。Qwen3-0.6B 在 MTEB-Code 拿 75.41，专门优化过代码语义。
- **上下文长度**：Qwen3-0.6B 的 32K 上下文能完整编码一个项目的 README + 多个 commit summary 拼起来的长文本，不用担心被截断。

而它的代价（1–3 分钟下载、2–3GB 内存）对 fork 用户完全可接受——比 Qwen3-4B 的 20 分钟 + 16GB 内存轻 7×，换来的是「中文 + 代码 + 长上下文」三个刚需维度的覆盖。

### 怎么切换

```bash
# 临时（环境变量）
export REPO2RESUME_EMBED_MODEL=local:Qwen3-Embedding-4B      # 高精度

# 持久（写进 config.toml）
repo2resume init  # 交互式向导里选
# 或直接编辑 ~/.repo2resume/config.toml：
# embed_model = "local:Qwen3-Embedding-4B"
```

### 什么时候完全不需要 embedding？

仓库很小（<30 个切片）且只投一两份 JD 时，可以跳过向量检索，只用 SQLite FTS5 关键词检索（零模型下载、<1 秒建索引）。代价是失去语义匹配（"高并发"匹配不到"QPS/压测"）。后续可加一个 `--no-vector` 开关走纯 BM25 降级路径。

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
