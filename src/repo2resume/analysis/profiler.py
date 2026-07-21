"""LLM structured skill-profile builder with validate + retry."""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape
from pydantic import ValidationError

from repo2resume.llm.client import LLMClient
from repo2resume.storage.models import FactSheet, RepoStatsBundle, SkillProfile, TechStack

logger = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"


def _extract_json(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        return text[start : end + 1]
    return text


class Profiler:
    def __init__(self, llm: LLMClient, *, max_retries: int = 2) -> None:
        self._llm = llm
        self._max_retries = max_retries
        self._env = Environment(
            loader=FileSystemLoader(str(PROMPTS_DIR)),
            autoescape=select_autoescape(enabled_extensions=()),
        )

    def build_profile(
        self,
        stats: RepoStatsBundle,
        *,
        tech_hints: TechStack | None = None,
        fact_sheet: FactSheet | None = None,
    ) -> SkillProfile:
        template = self._env.get_template("analyzer/build_skill_profile.j2")
        stats_json = stats.model_dump_json(indent=2)
        tech_json = tech_hints.model_dump_json(indent=2) if tech_hints is not None else "{}"
        facts_block = fact_sheet.as_prompt_block() if fact_sheet is not None else "(empty)"
        system = template.render(
            stats_json=stats_json,
            tech_hints_json=tech_json,
            fact_sheet_block=facts_block,
        )

        messages: list[dict[str, str]] = [
            {"role": "system", "content": system},
            {
                "role": "user",
                "content": (
                    "根据 system 中的 stats / tech_hints / fact_sheet 输出技能画像 JSON。"
                    "只输出 JSON 对象，不要 Markdown。"
                ),
            },
        ]

        last_error: str | None = None
        for attempt in range(self._max_retries + 1):
            result = self._llm.complete(messages, temperature=0.2, use_cache=attempt == 0)
            try:
                payload = _extract_json(result.content)
                profile = SkillProfile.model_validate_json(payload)
                profile.created_at = datetime.now(UTC).isoformat(timespec="seconds")
                return profile
            except (ValidationError, json.JSONDecodeError, ValueError) as exc:
                last_error = str(exc)
                logger.warning("profiler validate failed (attempt %s): %s", attempt + 1, exc)
                messages.append({"role": "assistant", "content": result.content})
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "上一次输出无法通过 schema 校验：\n"
                            f"{last_error}\n"
                            "请修复并只输出合法 JSON。"
                        ),
                    }
                )

        raise ValueError(f"Failed to build SkillProfile after retries: {last_error}")
