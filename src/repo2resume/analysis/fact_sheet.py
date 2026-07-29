"""Fact Sheet 构建器：从 `RepoStatsBundle` 抽取可核验的事实条目，约束 LLM 不幻觉。

每条 `FactEntry` 都带 `EvidenceRef` 指向统计来源，画像里「主语言 Python 99.5%」「在 api
提交了 12 次」这类声明都必须能对上这里的某条事实。这是 Phase 1 手写的反幻觉核心。
"""

from __future__ import annotations

import re

from repo2resume.storage.models import (
    EvidenceRef,
    FactEntry,
    FactSheet,
    RepoStatsBundle,
    SkillProfile,
)

# 简历默认不选 author_share 低于此阈值的仓（仍可通过 caution 提示；避免 1 commit 仓整页贪功）
RESUME_EXCLUDE_SHARE_BELOW = 0.12


def authorship_hints_from_profile(profile: SkillProfile) -> dict[str, float | None]:
    """从 caution / one_liners 推断仓库贡献份额。

    返回 repo → share（能解析出数字）或 None（仅标记为低贡献、无精确份额）。
    """
    hints: dict[str, float | None] = {}
    known_repos = {p.repo for p in profile.project_one_liners} | {
        h.repo for h in profile.highlights_pool
    }

    share_re = re.compile(
        r"(?P<repo>[A-Za-z0-9_.\-]+).{0,40}?"
        r"(?:author_share|贡献占比|份额|share)\s*[=:<>≈]?\s*(?P<share>0?\.\d+|\d+\.\d+)",
        re.IGNORECASE,
    )
    low_re = re.compile(
        r"(?P<repo>[A-Za-z0-9_.\-]+).{0,60}?(?:low_author_share|低贡献|贡献占比低)",
        re.IGNORECASE,
    )

    for caution in profile.caution:
        text = caution if isinstance(caution, str) else str(caution)
        matched = False
        for m in share_re.finditer(text):
            repo = m.group("repo")
            share = float(m.group("share"))
            if share > 1.0:
                share = share / 100.0
            hints[repo] = share
            matched = True
        if matched:
            continue
        for m in low_re.finditer(text):
            repo = m.group("repo")
            hints.setdefault(repo, None)
            matched = True
        if matched:
            continue
        # 「NLP_GAME: …」或 caution 直接含已知仓名
        for repo in known_repos:
            if repo and repo in text:
                if any(
                    k in text.lower()
                    for k in ("author_share", "low_author", "低贡献", "贡献占比", "0.")
                ):
                    hints.setdefault(repo, None)

    return hints


def fact_sheet_from_profile(profile: SkillProfile) -> FactSheet:
    """无完整 stats 时，用画像 caution 合成写作/审稿用的精简事实清单。"""
    entries: list[FactEntry] = []
    for repo, share in authorship_hints_from_profile(profile).items():
        if share is not None:
            entries.append(
                FactEntry(
                    key=f"{repo}.author_share",
                    value=share,
                    evidence=EvidenceRef(source=f"profile.caution:{repo}.author_share"),
                )
            )
            if share < 0.15:
                entries.append(
                    FactEntry(
                        key=f"{repo}.low_author_share",
                        value=share,
                        evidence=EvidenceRef(source=f"profile.caution:{repo}.low_author_share"),
                    )
                )
        else:
            entries.append(
                FactEntry(
                    key=f"{repo}.low_author_share",
                    value="flagged",
                    evidence=EvidenceRef(source=f"profile.caution:{repo}"),
                )
            )
    return FactSheet(entries=entries)


def build_fact_sheet(stats: RepoStatsBundle) -> FactSheet:
    """把仓库统计压成扁平的 `FactSheet`：全局语言份额 + 逐仓库贡献量/依赖/低贡献 caution。"""
    entries: list[FactEntry] = []

    # ========== 空 1：全局语言份额 ==========
    # 目的: 画像说「主语言 Python」必须能对上这里
    # 样例输入: {"Python": 0.7, "TypeScript": 0.3}
    # 期望产出两条:
    #   key="summary.overall_language_share.Python", value=0.7,
    #     evidence.source="summary.overall_language_share"
    #   key="summary.overall_language_share.TypeScript", value=0.3, ...
    for lang, share in stats.summary.overall_language_share.items():
        """
        填空: entries.append(FactEntry( key=?, value=?, evidence=? ))
        提示: key 用 f-string 拼上 lang；evidence 用 EvidenceRef(source=...)
        """
        entries.append(
            FactEntry(
                key=f"summary.overall_language_share.{lang}",
                value=share,
                evidence=EvidenceRef(source="summary.overall_language_share"),
            )
        )

    # ========== 空 2～4：逐仓库 ==========
    for repo in stats.repos:
        # ---------- 空 2：贡献量 ----------
        # 目的: 「在 api 提交了 N 次」有据可查
        # 样例 api → author_commits=12, author_share=0.6
        # 期望产出:
        #   key="api.author_commits", value=12
        #   key="api.author_share",   value=0.6
        # （tiny 同理，用 repo.name 拼 key）
        """
        填空: 追加两条 FactEntry（author_commits / author_share）
        提示: repo.name / repo.author_commits / repo.author_share
        """
        entries.append(
            FactEntry(
                key=f"{repo.name}.author_commits",
                value=repo.author_commits,
                evidence=EvidenceRef(source=f"{repo.name}.author_commits"),
            )
        )
        entries.append(
            FactEntry(
                key=f"{repo.name}.author_share",
                value=repo.author_share,
                evidence=EvidenceRef(source=f"{repo.name}.author_share"),
            )
        )

        # ---------- 空 3：依赖（有则写） ----------
        # 目的: 技术栈声明必须来自检测到的依赖文件
        # 样例 api → dependencies 非空；tiny → {} 跳过
        # 期望产出（仅 api）: key="api.dependencies"（value 随意，测试只查 key）
        if repo.dependencies:
            """
            填空: entries.append(FactEntry( key=?, value=?, evidence=? ))
            提示: key = f"{repo.name}.dependencies"
            """
            entries.append(
                FactEntry(
                    key=f"{repo.name}.dependencies",
                    value=repo.dependencies,
                    evidence=EvidenceRef(source=f"{repo.name}.dependencies"),
                )
            )

        # ---------- 空 4：低贡献 caution ----------
        # 目的: 份额过低时标记，避免夸大「主导该仓库」
        # 样例 api=0.6 跳过；tiny=0.04 < 0.15 → 要写
        # 期望产出（仅 tiny）: key="tiny.low_author_share"
        if repo.author_share < 0.15:
            """
            填空: entries.append(FactEntry( key=?, value=?, evidence=? ))
            提示: key = f"{repo.name}.low_author_share"
            """
            entries.append(
                FactEntry(
                    key=f"{repo.name}.low_author_share",
                    value=repo.author_share,
                    evidence=EvidenceRef(source=f"{repo.name}.low_author_share"),
                )
            )

    return FactSheet(entries=entries)
