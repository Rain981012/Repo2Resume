from __future__ import annotations

from repo2resume.storage.models import EvidenceRef, FactEntry, FactSheet, RepoStatsBundle


def build_fact_sheet(stats: RepoStatsBundle) -> FactSheet:
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
