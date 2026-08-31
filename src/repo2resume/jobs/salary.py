"""薪资抽取：把 JD 文本解析为结构化 SalaryInfo。"""

from __future__ import annotations

import re

from repo2resume.storage.models import SalaryInfo

_SEP = r"[-~～到至]"
_NUM = r"\d+(?:\.\d+)?"

# 带单位：20-30k / 1.5-2万 / 8-12千
_SALARY_UNIT_RE = re.compile(rf"(?P<lo>{_NUM})\s*{_SEP}\s*(?P<hi>{_NUM})\s*(?P<unit>[kK]|万|千)")
# 元为单位：20000-30000元
_SALARY_YUAN_RE = re.compile(rf"(?P<lo>\d{{4,6}})\s*{_SEP}\s*(?P<hi>\d{{4,6}})\s*元")
# 薪资关键词紧邻的裸数字：薪资 20-30
_SALARY_LABELED_RE = re.compile(
    rf"(?:月薪|年薪|薪资|工资|待遇|salary)\D{{0,4}}(?P<lo>{_NUM})\s*{_SEP}\s*(?P<hi>{_NUM})",
    flags=re.I,
)
_UNIT_TO_K = {"k": 1.0, "万": 10.0, "千": 1.0}


def extract_salary_info(text: str) -> SalaryInfo:
    """统一折算成 K。

    单位是必需的：`5-10年经验`、`3-5人团队` 这类数字区间不是薪资，
    早先的可选单位会把它们当成 5-10K，进而按薪资误杀岗位。
    """
    raw_text = text or ""

    hit = _SALARY_UNIT_RE.search(raw_text)
    if hit is not None:
        factor = _UNIT_TO_K[(hit.group("unit") or "k").lower()]
        lo, hi = float(hit.group("lo")) * factor, float(hit.group("hi")) * factor
        return SalaryInfo(min=lo, max=hi, raw=hit.group(0).replace(" ", ""), confidence=0.95)

    hit = _SALARY_YUAN_RE.search(raw_text)
    if hit is not None:
        lo, hi = float(hit.group("lo")) / 1000.0, float(hit.group("hi")) / 1000.0
        return SalaryInfo(min=lo, max=hi, raw=hit.group(0).replace(" ", ""), confidence=0.95)

    hit = _SALARY_LABELED_RE.search(raw_text)
    if hit is not None:
        lo, hi = float(hit.group("lo")), float(hit.group("hi"))
        return SalaryInfo(min=lo, max=hi, raw=hit.group(0).replace(" ", ""), confidence=0.8)

    return SalaryInfo(raw=None, confidence=0.0)


def salary_mid_k(info: SalaryInfo) -> float | None:
    if not info.known:
        return None
    if info.min is None or info.max is None:
        return None
    return (float(info.min) + float(info.max)) / 2.0
