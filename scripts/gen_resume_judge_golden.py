#!/usr/bin/env python3
"""Expand resume_judge_golden.json to 50 polarity probes (not a human-labeled set)."""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "evals" / "datasets" / "resume_judge_golden.json"


def bullet(label: str, body: str, source: str, detail: str = "") -> dict:
    return {
        "label": label,
        "body": body,
        "evidence": {"source": source, "detail": detail},
    }


def case(
    cid: str,
    *,
    polarity: str,
    jd: str,
    notes: str,
    project: str,
    oneliner: str,
    bullets: list[dict],
    title: str | None = None,
) -> dict:
    draft: dict = {
        "locale": "zh-CN",
        "projects": [
            {
                "project_name": project,
                "one_liner": oneliner,
                "bullets": bullets,
            }
        ],
    }
    if title:
        draft["job_title"] = title
    return {
        "id": cid,
        "polarity": polarity,
        "jd_text": jd,
        "rubric_notes": notes,
        "draft": draft,
    }


SEED = [
    case(
        "judge_backend_jd",
        polarity="grounded",
        jd="Python 后端工程师：FastAPI、Redis、可观测性、能讲清模块边界与协作贡献。",
        notes="应扣分：若写成整站独立完成；应加分：模块级 + 有 evidence。",
        title="Python 后端工程师",
        project="社交平台后端",
        oneliner="分布式社交 API。",
        bullets=[
            bullet(
                "认证模块",
                "负责 signup/login 与 JWT 校验模块；补单元测试覆盖登录失败路径。",
                "social-dist",
                "auth module",
            ),
            bullet(
                "Inbox",
                "实现 inbox CRUD；处理跨节点拉取帖子时的签名校验。",
                "social-dist",
                "inbox",
            ),
        ],
    ),
    case(
        "judge_overclaim_should_low",
        polarity="overclaim",
        jd="寻求诚实描述团队项目贡献的候选人。",
        notes="故意贪功稿：judge 的「不贪功」维度应偏低。",
        project="大型平台",
        oneliner="全公司核心系统。",
        bullets=[
            bullet(
                "主导",
                "独立完成整个前后端与基础设施，从零搭建整套微服务与数据平台。",
                "tiny-share",
                "1 commit",
            )
        ],
    ),
    case(
        "judge_slogan_low_spec",
        polarity="slogan",
        jd="Python 后端：希望看到具体模块与技术栈，而不是口号。",
        notes="口号稿：specificity 应偏低。",
        project="后端服务",
        oneliner="公司核心系统。",
        bullets=[bullet("开发", "参与后端开发，完成相关工作。", "misc", "n/a")],
    ),
    case(
        "judge_jd_mismatch_frontend",
        polarity="mismatch",
        jd="招聘 Python 后端工程师，要求 FastAPI、PostgreSQL、消息队列。",
        notes="JD 是后端，稿子只写 CSS 动画：jd_relevance 应偏低。",
        title="Python 后端工程师",
        project="营销落地页",
        oneliner="活动页动画。",
        bullets=[
            bullet(
                "CSS 动效",
                "用 CSS 动画实现按钮悬停与轮播；没有后端接口或数据库。",
                "web-ui",
                "css",
            )
        ],
    ),
    case(
        "judge_fastapi_grounded",
        polarity="grounded",
        jd="Python 后端：FastAPI、Redis、JWT。",
        notes="模块级 + evidence：groundedness / jd_relevance 应偏高。",
        project="演示 API",
        oneliner="FastAPI 微服务与鉴权。",
        bullets=[
            bullet(
                "JWT 鉴权",
                "实现 signup/login 与 JWT 校验；用 Redis 缓存会话，补登录失败路径单测。",
                "demo-api",
                "jwt redis",
            ),
            bullet(
                "限流中间件",
                "为 webhook 增加 rate limiter，保护 postgres 连接池不被突发请求打满。",
                "demo-api",
                "rate limiter",
            ),
        ],
    ),
    case(
        "judge_missing_evidence",
        polarity="no_evidence",
        jd="要求经历可溯源。",
        notes="evidence.source 为空：groundedness 应偏低。",
        project="订单服务",
        oneliner="订单中台。",
        bullets=[
            bullet("下单接口", "实现下单与库存扣减接口。", "", ""),
        ],
    ),
    case(
        "judge_k8s_ops",
        polarity="grounded",
        jd="容器与集群：Kubernetes、Helm、Terraform。",
        notes="应对上 k8s 工具链。",
        project="预发集群工具",
        oneliner="面向 staging 的编排与交付。",
        bullets=[
            bullet(
                "Helm 发布",
                "用 Helm chart 管理预发工作负载；Terraform 管理配套云资源，避免手工改集群。",
                "k8s-ops",
                "helm terraform",
            )
        ],
    ),
    case(
        "judge_agent_loop_ok",
        polarity="grounded",
        jd="Agent 工程：tool-use 循环、可观测、防卡死。",
        notes="模块级 harness 描述应加分。",
        project="简历 CLI",
        oneliner="本地仓库分析生成 Markdown 简历。",
        bullets=[
            bullet(
                "Agent Loop",
                "实现 tool-use 循环、权限钩子与重复调用熔断；工具失败回填错误文本让模型自纠。",
                "resume-cli",
                "agent/loop.py",
            )
        ],
    ),
]


def extra_cases() -> list[dict]:
    grounded = [
        (
            "judge_g_django_inbox",
            "Python 后端：Django REST、跨节点消息。",
            "社交 inbox",
            "去中心化内容分发。",
            [
                bullet(
                    "Inbox CRUD",
                    "实现 inbox 收发与请求签名校验；用 Redis 缓存热门时间线，降低跨节点拉取延迟。",
                    "social-dist",
                    "inbox",
                )
            ],
        ),
        (
            "judge_g_celery_retry",
            "Python 后端：异步任务、失败重试。",
            "出站投递",
            "面向联邦协议的投递队列。",
            [
                bullet(
                    "投递重试",
                    "用 Celery 做出站投递与指数退避；失败批次写入死信队列，避免静默丢件。",
                    "social-dist",
                    "celery retry",
                )
            ],
        ),
        (
            "judge_g_grpc_auth",
            "后端：gRPC、服务间鉴权。",
            "内部 API",
            "服务网格内的鉴权旁路。",
            [
                bullet(
                    "gRPC 鉴权",
                    "为内部 gRPC 接口加 JWT 拦截器；拒绝无租户声明的调用，并补失败路径单测。",
                    "demo-api",
                    "grpc jwt",
                )
            ],
        ),
        (
            "judge_g_kafka_outbox",
            "后端：Kafka、事务消息。",
            "订单出站",
            "订单状态变更的可靠广播。",
            [
                bullet(
                    "Outbox 投递",
                    "订单事务提交后写 outbox，再由 worker 投递 Kafka；用去重键避免重复扣库存。",
                    "order-svc",
                    "outbox kafka",
                )
            ],
        ),
        (
            "judge_g_spark_etl",
            "数据工程：Spark ETL、Parquet schema。",
            "数仓入仓",
            "日批入仓作业。",
            [
                bullet(
                    "日批 ETL",
                    "实现 Spark 日批入仓；修复 parquet schema drift，避免下游列类型静默错位。",
                    "etl-spark",
                    "daily etl",
                )
            ],
        ),
        (
            "judge_g_react_list",
            "前端：React、TypeScript、列表性能。",
            "评审列表",
            "课程 PDF 评审前端。",
            [
                bullet(
                    "虚拟列表",
                    "用 React + TS 做评论列表虚拟滚动；图片上传失败时保留本地草稿，避免刷新丢失。",
                    "pdf-ui",
                    "virtual list",
                )
            ],
        ),
        (
            "judge_g_prom_metrics",
            "可观测：Prometheus、延迟分位。",
            "搜岗观测",
            "职位搜索耗时拆分。",
            [
                bullet(
                    "阶段耗时",
                    "把搜岗拆成 cache/recall/score 计时；把 p95 打进 Prometheus，避免只看到总耗时。",
                    "resume-cli",
                    "jobs spans",
                )
            ],
        ),
        (
            "judge_g_hybrid_rrf",
            "检索：混合检索、可溯源素材。",
            "简历 RAG",
            "项目素材召回。",
            [
                bullet(
                    "RRF 融合",
                    "实现 FTS5 与向量双路召回，用 RRF 融合；生成路径关闭 LLM 重排以控制延迟。",
                    "resume-cli",
                    "hybrid.py",
                )
            ],
        ),
        (
            "judge_g_sqlite_fts",
            "后端：SQLite FTS、本地检索。",
            "关键词路",
            "本地 BM25 检索。",
            [
                bullet(
                    "FTS5 查询",
                    "用 SQLite FTS5 做关键词检索；查询串做特殊字符消毒，避免 MATCH 语法错误。",
                    "resume-cli",
                    "fts5",
                )
            ],
        ),
        (
            "judge_g_go_gateway",
            "后端：Go、API 网关、限流。",
            "边缘网关",
            "对外流量入口。",
            [
                bullet(
                    "令牌桶",
                    "在 Go 网关实现按租户令牌桶；超限返回 429 并打点，避免打满上游连接池。",
                    "edge-gw",
                    "ratelimit",
                )
            ],
        ),
    ]
    overclaim = [
        (
            "judge_o_fullstack",
            "寻求模块级贡献描述。",
            "独立完成整个前后端与 DevOps，从零搭建整套平台并上线。",
        ),
        (
            "judge_o_from_scratch",
            "团队项目，请写清边界。",
            "从零搭建整套微服务与数据平台，覆盖注册登录消息与推荐全链路。",
        ),
        (
            "judge_o_all_infra",
            "基础设施岗位，强调分工。",
            "一个人负责公司全部 Kubernetes、监控、CI 与值班体系。",
        ),
        (
            "judge_o_owned_ml",
            "机器学习应用，不要夸成研究负责人。",
            "独立设计并训练公司全部推荐模型，效果提升数倍。",
        ),
        (
            "judge_o_led_team",
            "应届岗位，避免虚高职级。",
            "带领数十人团队完成核心交易系统重构并独立决策架构。",
        ),
        (
            "judge_o_complete_user_sys",
            "写清模块即可。",
            "构建了完整的用户体系与消息链路，覆盖全部前后端模块并独立上线。",
        ),
        (
            "judge_o_pipeline",
            "CLI 工具经历，勿写成整条商业闭环。",
            "独立开发 CLI agent，实现 git 分析到简历生成到投递录用的完整 pipeline。",
        ),
    ]
    slogan = [
        ("judge_s_backend", "Python 后端，要具体技术动作。", "参与后端开发，完成相关工作。"),
        ("judge_s_frontend", "前端要写组件与性能，不要空话。", "负责前端开发，优化用户体验。"),
        ("judge_s_data", "数据岗需要作业与指标，不要口号。", "参与大数据处理，提升数据质量。"),
        ("judge_s_ml", "ML 应用要写数据与评估，不要空泛。", "完成模型训练，取得良好效果。"),
        ("judge_s_qa", "测试岗要写用例与失败路径。", "负责测试工作，保障系统稳定。"),
        ("judge_s_pm_dev", "开发岗不要写成项目管理套话。", "积极沟通协作，推动项目按时交付。"),
        ("judge_s_ops", "运维要写故障与自动化。", "保障线上稳定，做好运维支持。"),
    ]
    mismatch = [
        (
            "judge_m_css_for_backend",
            "Python 后端：FastAPI、PostgreSQL。",
            "CSS 动效",
            "用 CSS 做按钮动画与轮播，无 API。",
            "web-ui",
        ),
        (
            "judge_m_k8s_for_frontend",
            "前端：React、无障碍、组件库。",
            "Helm 发布",
            "编写 Helm chart 发布预发集群工作负载。",
            "k8s-ops",
        ),
        (
            "judge_m_spark_for_android",
            "Android：Kotlin、列表与离线缓存。",
            "Spark 入仓",
            "实现 Spark 日批，把日志写入数仓。",
            "etl-spark",
        ),
        (
            "judge_m_ios_for_data",
            "数据工程：Airflow、Spark。",
            "Swift 动画",
            "用 SwiftUI 做启动页动画与手势。",
            "ios-app",
        ),
        (
            "judge_m_hardware",
            "Python 后端校招。",
            "FPGA 时序",
            "做 FPGA 时序约束与板级调试验证。",
            "chip-lab",
        ),
        (
            "judge_m_algo_paper",
            "业务后端：CRUD 与稳定性。",
            "论文复现",
            "复现 Transformer 论文指标，未接入任何在线服务。",
            "ml-lab",
        ),
        (
            "judge_m_design",
            "后端工程师：消息队列与存储。",
            "品牌视觉",
            "输出海报与配色规范，没有服务端代码。",
            "design",
        ),
    ]
    no_ev = [
        ("judge_n_order", "经历必须可溯源。", "下单接口", "实现下单与库存扣减。"),
        ("judge_n_auth", "请给出仓库或模块依据。", "登录", "做了 JWT 登录。"),
        ("judge_n_cache", "可溯源优先。", "缓存", "加了 Redis 缓存。"),
        ("judge_n_ui", "前端经历也要 source。", "列表页", "实现了评论列表。"),
        ("judge_n_k8s", "基础设施经历要能对上仓。", "发布", "用 Helm 发过版。"),
        ("judge_n_agent", "Agent 项目请给文件级依据。", "循环", "写了 tool-use 循环。"),
        ("judge_n_etl", "数据作业请标明仓名。", "入仓", "跑过 Spark job。"),
    ]
    mixed = [
        (
            "judge_x_good_plus_overclaim",
            "Python 后端，模块级 + 诚实边界。",
            "实现 inbox CRUD 与签名校验；同时独立完成整个社交平台前后端。",
            "social-dist",
            "inbox",
            "半句模块 + 半句贪功：no_overclaim 应被拉低。",
        ),
        (
            "judge_x_metric_invented",
            "后端：不要虚构百分比。",
            "实现限流中间件；QPS 提升 1000%，用户量破亿。",
            "demo-api",
            "ratelimit",
            "有模块名但数字像编的：groundedness 应偏低。",
        ),
        (
            "judge_x_share_in_body",
            "不要把内部占比写进投递稿。",
            "实现关注写路径；本人贡献占比约 0.1，author_share 很低。",
            "social-dist",
            "follow",
            "泄漏内部元数据：应扣 groundedness / 专业性。",
        ),
        (
            "judge_x_jd_half",
            "要 FastAPI 与 PostgreSQL。",
            "实现 React 组件库主题切换；顺带提了一句用过 FastAPI。",
            "web-ui",
            "theme",
            "主体跑题，关键词点缀：jd_relevance 应偏低。",
        ),
    ]

    out: list[dict] = []
    for cid, jd, project, oneliner, bullets in grounded:
        out.append(
            case(
                cid,
                polarity="grounded",
                jd=jd,
                notes="模块级 + evidence：groundedness / jd_relevance 应偏高。",
                project=project,
                oneliner=oneliner,
                bullets=bullets,
            )
        )
    for cid, jd, body in overclaim:
        out.append(
            case(
                cid,
                polarity="overclaim",
                jd=jd,
                notes="故意贪功：no_overclaim 应偏低。",
                project="核心系统",
                oneliner="公司平台。",
                bullets=[bullet("全栈", body, "tiny-share", "1 commit")],
            )
        )
    for cid, jd, body in slogan:
        out.append(
            case(
                cid,
                polarity="slogan",
                jd=jd,
                notes="口号稿：specificity 应偏低。",
                project="业务系统",
                oneliner="内部平台。",
                bullets=[bullet("工作", body, "misc", "n/a")],
            )
        )
    for cid, jd, label, body, src in mismatch:
        out.append(
            case(
                cid,
                polarity="mismatch",
                jd=jd,
                notes="与 JD 方向明显不符：jd_relevance 应偏低。",
                project="不匹配项目",
                oneliner="与目标岗无关的产出。",
                bullets=[bullet(label, body, src, "mismatch")],
            )
        )
    for cid, jd, label, body in no_ev:
        out.append(
            case(
                cid,
                polarity="no_evidence",
                jd=jd,
                notes="evidence.source 为空：groundedness 应偏低。",
                project="未标注仓",
                oneliner="缺少溯源。",
                bullets=[bullet(label, body, "", "")],
            )
        )
    for cid, jd, body, src, detail, notes in mixed:
        out.append(
            case(
                cid,
                polarity="mixed",
                jd=jd,
                notes=notes,
                project="混合稿",
                oneliner="部分具体、部分越界。",
                bullets=[bullet("模块", body, src, detail)],
            )
        )
    return out


def main() -> None:
    rows = list(SEED) + extra_cases()
    if len(rows) != 50:
        raise SystemExit(f"expected 50 cases, got {len(rows)}")
    ids = [r["id"] for r in rows]
    if len(ids) != len(set(ids)):
        raise SystemExit("duplicate ids")
    OUT.write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {len(rows)} cases -> {OUT}")


if __name__ == "__main__":
    main()
