# 提示词调优日志（Phase 5）

纪律：一次只改一个变量；改完必跑 `repo2resume evals run`；记下动机与分数。

## 基线（Round 0）— 2026-07-30

命令：`repo2resume evals run --suite all --no-llm --save-baseline`

| suite | metric | value |
| ------- | -------- | ------- |
| retrieval | recall@5 | 0.9500 |
| retrieval | mrr | 1.0000 |
| resume_prog | pass_rate | 1.0000 |
| resume_judge | — | skipped (--no-llm) |

产物：`evals/results/baseline.json`

## Round 1 — Writer 归属反例

- **动机：** 真跑仍易写出「完整用户体系」类整站表述；给 Writer 明确反例/正例。
- **改动文件：** `src/repo2resume/prompts/resume/write_experience.j2`（仅规则 5）
- **改动摘要：** 增加禁止反例「构建了完整的用户体系与消息链路」与正例「实现 inbox 模块的 CRUD…」。
- **跑分：** `evals run --no-llm`（离线套件不测 Writer 生成，预期数值不变）

| metric | before | after | Δ |
|--------|--------|-------|---|
| retrieval.recall@5 | 0.9500 | 0.9500 | 0 |
| resume_prog.pass_rate | 1.0000 | 1.0000 | 0 |

说明：程序化/检索 fixture 不经过 Writer；本轮收益体现在真实 `generate_resume` 与后续带 `--` 去掉 `--no-llm` 的 judge。

## Round 2 — Critic 归属越权

- **动机：** Critic 对「负责整个后端」类含糊主导表述偏松；收紧 must 指引。
- **改动文件：** `src/repo2resume/prompts/resume/critic_review.j2`（仅事实节归属越权）
- **改动摘要：** 禁词示例加「从零搭建整套」；存疑时宁可 must 收窄。
- **跑分：** 同上，离线套件持平。

| metric | before | after | Δ |
|--------|--------|-------|---|
| retrieval.recall@5 | 0.9500 | 0.9500 | 0 |
| resume_prog.pass_rate | 1.0000 | 1.0000 | 0 |

## Round 3 — 检索评测接上真实 hybrid + 重排对照脚手架（2026-08-20）

- **动机：** 简历写了 Recall@K 和「两套重排对照实验」，但 runner 走的是词重叠 fixture；cross-encoder 从未加载真模型。
- **改动：**
  - `evals run --backend hybrid` 把 fixture chunks 灌进临时 VectorStore，走 `hybrid_search`（BM25 + 向量 + RRF）。
  - `scripts/bench_rerank.py`：每个 query **先冻结** hybrid top-20，三变体只重排这份列表；LLM 路径 `use_cache=False`。
- **跑分：**

```bash
repo2resume evals run --suite retrieval --backend fixture
repo2resume evals run --suite retrieval --backend hybrid
python scripts/bench_rerank.py --skip-llm --skip-cross-encoder
```

| 路径 | recall@5 | mrr | 证明跑的是哪条代码 |
| ------ | ---------- | ----- | ------------------- |
| fixture（词重叠） | 0.9500 | 1.0000 | `sample_source=fixture` |
| hybrid（真实 RRF） | 0.9500 | 1.0000 | `sample_source=hybrid` |
| rerank hybrid 基线（冻结 top-20 的 top-5） | 0.9500 | 1.0000 | `evals/results/rerank_ab.json` |

- **结论：** 评测现在打到真实混合检索了，但 **当前 golden 太容易**，hybrid 和 fixture 都饱和在 0.95 / MRR 1.0，**测不出重排有没有提升**。本机 HuggingFace 缓存里没有 `bge-reranker-v2-m3`（约 2.2GB），本次未下载、未跑 LLM rerank。
- **下一步：** 换更难的 query（或加大负例 chunk）后再跑：

```bash
python scripts/bench_rerank.py
# 去掉 --skip-* 会加载真 cross-encoder / 调 LLM；首次 CE 冷启动耗时请记进 load_ms
```

## Round 4 — 岗位 LLM 精排默认打开 + 20s 超时降级（2026-08-20）

- **动机：** chat 路径曾显式 `use_llm=False`，简历「批量打分」与线上不一致；zai 45–60s 超时是关打分的原因。
- **改动：** 批量 LLM 超时跟 `config.llm_timeout_s`（默认 60s，弱模型可再调大）；`search_jobs` 对向量 top-10 开一次批量 LLM；`TimeoutError` / 坏 JSON 退回纯向量顺序，不抛给 agent。
- **验收：** `pytest tests/test_jobs_matcher.py tests/test_job_prefs.py -q`（超时路径与 `use_llm=False` 的 job_id 序、overall 一致）。

```bash
repo2resume evals run --suite resume_judge   # 需 API key，看 judge_avg
repo2resume evals run --baseline evals/results/baseline.json
```

## Round 5 — 真跑 LLM rerank vs bge-reranker-v2-m3（2026-08-20）

- **动机：** Round 3 只跑了 hybrid 基线（`--skip-llm --skip-cross-encoder`）。模型已下载到 HF 缓存后，去掉 skip 做三方对照。
- **命令：** `python scripts/bench_rerank.py`
- **约束：** 冻结 hybrid top-20；LLM `use_cache=False`；embedder 仍是评测用 `eval-hash-token`（不是线上 Qwen3）。

| 变体 | recall@5 | mrr | p95 延迟 | 其它 |
| ------ | ---------- | ----- | ---------- | ------ |
| hybrid（冻结 top-20 的 top-5） | 0.95 | 1.0 | — | 基线 |
| LLM rerank | **1.00** | 1.0 | **18.1 s** | 10 次 API |
| cross-encoder `bge-reranker-v2-m3` | 0.95 | 1.0 | **1.27 s** | 冷启动 `load_ms=16.6 s` |

- **质量：** 10 条里 hybrid 只漏 1 条相关文档：query「Python backend microservices FastAPI」的 `repo:demo-api:commits` 在冻结列表第 8 名，进不了 top-5（该 query recall=0.5 → 全集 0.95）。LLM 把它捞回 top-5（recall 1.0）；CE 没有。其余 9 条三方都已满分，MRR 全是 1.0（第一条相关始终在 rank 1）。
- **结论：** 对照实验本身跑通了（真模型 + 真 LLM，结果在 `evals/results/rerank_ab.json`）。**当前 golden 几乎饱和，测不出 CE 有没有比 hybrid 更准**；LLM 的 +0.05 来自捞回那一条 commits，但 p95 18s，不适合叠在 Writer-Critic 前面。CE 热路径 ~1.3s、冷启动 ~17s，质量与 hybrid 打平。

## Round 6 — 子代理 return_after_tools 可切换（2026-08-20）

- **动机：** 两个专家都曾写死 `return_after_tools=True`，子 loop 调完第一个工具就回传原文，多步推理从未发生。
- **改动：**
  - `AppConfig.subagent_return_after_tools`（默认 `false`）只作用于 `repo_analyst`；`job_scout` 仍默认直传，以免丢掉 `【job_scout已完成】`。
  - `subagent_repeat_limit`：直传时阈值 2，多步时阈值 3。阈值=1 且关闭直传会在第一次工具后误判卡死（单测覆盖）。
  - `SubAgentRunner.last_trace` 记录 `llm_calls` / `tool_names` / `elapsed_ms` / `outcome`。
- **验收：** `pytest tests/test_agent_subagent.py tests/test_config.py -q`
  - 直传：1 次 LLM，`outcome=tool_passthrough`
  - 多步：2 次 LLM，`outcome=llm_summary`
- **线上：** 重启 chat 后分析仓库会走二次总结。若胡编/超时，在 `~/.repo2resume/config.toml` 设 `subagent_return_after_tools = true`。
- **实测（glm-5.2，2026-08-20）：** `python scripts/bench_subagent_mode.py`
  - `outcome=llm_summary`，`llm_calls=2`，只调了一次 `analyze_repo`，未卡死
  - 墙钟 **158s**（含 5 仓 git 挖掘 + Profiler 画像 + 子代理两轮 LLM）
  - 总结保留了 5 仓 / 87 次提交 / 低贡献仓警告，未见明显胡编
  - **二次总结丢掉了 `【repo_analyst已完成】`**；已改为：提示词要求保留 + runner 在缺失时补回标记（主 chat 靠它展示方向）
  - 产物：`evals/results/subagent_mode.json`

## Round 7 — 接通评测闭环（T8 / T9 / T10，2026-08-26）

- **动机：** 简历「可量化调优闭环」不成立：程序化用 `issubset` 测不出误报；hybrid 与 fixture 同分；改 `.j2` 时离线 Δ=0；baseline 与日志对不上。
- **改动：**
  - T10a：`expect_must_categories` 改为与触发类别**集合相等**；补 4 条合规正例；负例补全实际会触发的类别。
  - T8：`retrieval_fixture_chunks.json` 增加 8 条关键词堆砌负例。fixture/hybrid **Recall@5 仍为 0.95**（golden 仍偏易），**MRR 0.80 vs 0.85**，两路不再打平。
  - T9：新增 `resume_e2e` 套件（固定 JD+素材 → `writer_critic_loop` → 程序化断言）。CI 用 Playback LLM；缺 evidence 的稿 `prog_pass_rate` 必降。`--no-llm` 跳过。
  - T10b：`resume_judge_golden.json` 2 → 8 → **50** 条极性探针（grounded / overclaim / slogan / mismatch / no_evidence / mixed）。仍无人工分，不能算 judge 准确率。
  - T10c：`evals run --update-baseline`（`--save-baseline` 别名）；重刷 `evals/results/baseline.json`（fixture，`--no-llm`）。
- **离线跑分：**

```bash
repo2resume evals run --suite all --no-llm --backend fixture --save-baseline
repo2resume evals run --suite retrieval --no-llm --backend hybrid
```

| 路径 | recall@5 | mrr | 其它 |
| ------ | ---------- | ----- | ------ |
| fixture | 0.9500 | **0.8000** | `run_planb_fixture.json` / `baseline.json` |
| hybrid | 0.9500 | **0.8500** | `run_planb_hybrid.json` |
| resume_prog | — | — | count=14，pass_rate=1.0 |
| resume_judge / resume_e2e | — | — | skipped (--no-llm) |

- **自检：** `pytest tests/test_evals_runner.py`：误报用例 pass_rate=0；e2e 有 evidence → prog=1.0；无 evidence → prog<1.0。
- **未做：** 真模型跑 `resume_e2e` / judge（需 API）；judge 三次取中位数；把偏好合并/熔断搬进 runner（仍由 pytest 覆盖）。
