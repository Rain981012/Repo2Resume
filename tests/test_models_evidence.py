from __future__ import annotations

from repo2resume.storage.models import EvidenceRef, LanguageShare, SkillProfile


def test_evidence_coerces_from_string() -> None:
    row = LanguageShare(name="Python", share=0.5, evidence="summary.overall_language_share")
    assert isinstance(row.evidence, EvidenceRef)
    assert row.evidence.source == "summary.overall_language_share"


def test_skill_profile_migrates_flat_tech_stack() -> None:
    profile = SkillProfile.model_validate(
        {
            "primary_direction": "Backend",
            "coding_language": [],
            "tech_stack": ["Python", "FastAPI"],
            "gaps_or_cautions": ["low share"],
        }
    )
    assert profile.tech_stack.other == ["Python", "FastAPI"]
    assert profile.caution == ["low share"]


def test_skill_profile_coerces_caution_objects() -> None:
    profile = SkillProfile.model_validate(
        {
            "primary_direction": "Backend",
            "coding_language": [],
            "caution": [
                {
                    "repo": "NLP_GAME",
                    "reason": "author_share 为 0.111，低于 0.15",
                }
            ],
        }
    )
    assert profile.caution == ["NLP_GAME: author_share 为 0.111，低于 0.15"]
