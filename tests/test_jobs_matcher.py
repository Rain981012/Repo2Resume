"""Tests for jobs/matcher.py."""

from __future__ import annotations

import pytest

from repo2resume.jobs.matcher import JobMatcher, _cosine_similarity
from repo2resume.llm.client import CompletionResult
from repo2resume.storage.models import Job, MatchScore, SkillProfile, TechStack


class FakeEmbedder:
    def __init__(self, dim: int = 4) -> None:
        self.dim = dim
        self._cache: dict[str, list[float]] = {}

    @property
    def dimension(self) -> int:
        return self.dim

    def encode(self, texts: list[str]) -> list[list[float]]:
        results = []
        for t in texts:
            if t not in self._cache:
                # 用文本哈希做确定性的伪向量
                vec = [float((hash(t) + i) % 100) / 100.0 for i in range(self.dim)]
                norm = sum(x * x for x in vec) ** 0.5
                vec = [x / norm for x in vec] if norm else vec
                self._cache[t] = vec
            results.append(self._cache[t])
        return results


class FakeLLM:
    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.calls = 0

    def complete(self, messages, *, model=None, temperature=0.0, **kwargs):
        self.calls += 1
        return CompletionResult(
            content=self.reply,
            model="fake",
            input_tokens=100,
            output_tokens=50,
            cost_usd=0.0,
        )


def test_cosine_similarity_normalized():
    a = [1.0, 0.0, 0.0]
    b = [1.0, 0.0, 0.0]
    assert _cosine_similarity(a, b) == pytest.approx(1.0)


def test_job_matcher_computes_scores():
    embedder = FakeEmbedder()
    llm = FakeLLM('{"score": 0.75, "reason": "匹配 Python 后端经验"}')
    matcher = JobMatcher(embedder, llm)

    profile = SkillProfile(
        primary_direction="Backend Engineer",
        tech_stack=TechStack(
            languages=["Python", "Go"],
            frameworks=["FastAPI"],
            tools_and_infra=["Kubernetes"],
        ),
    )
    job = Job(
        id="job-1",
        title="Backend Engineer",
        jd_text="Python backend microservices",
        skills=["Python", "Kubernetes"],
    )
    score = matcher.match(profile, job)
    assert isinstance(score, MatchScore)
    assert 0.0 <= score.overall_score <= 1.0
    assert score.vector_score is not None
    assert score.llm_score == pytest.approx(0.75)
    assert "Python" in score.reason


def test_match_all_sorts_and_limits():
    embedder = FakeEmbedder()
    # 批量路径期望 JSON 数组；空数组 → 纯向量排序仍返回 top_k
    llm = FakeLLM("[]")
    matcher = JobMatcher(embedder, llm)
    profile = SkillProfile(primary_direction="Backend Engineer")
    jobs = [
        Job(id="j1", title="A", jd_text="x", skills=[]),
        Job(id="j2", title="B", jd_text="y", skills=[]),
        Job(id="j3", title="C", jd_text="z", skills=[]),
    ]
    scores = matcher.match_all(profile, jobs, top_k=2)
    assert len(scores) == 2
    assert scores[0].overall_score >= scores[1].overall_score
    assert llm.calls == 1  # 一次批量，不再 3 次串行


def test_match_all_batch_llm_once():
    embedder = FakeEmbedder()
    llm = FakeLLM(
        '[{"job_id":"j1","score":0.9,"reason":"很匹配"},'
        '{"job_id":"j2","score":0.2,"reason":"不太匹配"}]'
    )
    matcher = JobMatcher(embedder, llm)
    profile = SkillProfile(primary_direction="Backend Engineer")
    jobs = [
        Job(id="j1", title="Backend", jd_text="Python", skills=["Python"]),
        Job(id="j2", title="Frontend", jd_text="React", skills=["React"]),
        Job(id="j3", title="Other", jd_text="Go", skills=["Go"]),
    ]
    scores = matcher.match_all(profile, jobs, top_k=3, llm_top_n=2)
    assert llm.calls == 1
    by_id = {s.job_id: s for s in scores}
    assert by_id["j1"].llm_score == pytest.approx(0.9)
    assert "匹配" in by_id["j1"].reason
    # j3 不在 LLM top-2 → 无 llm_score
    assert by_id["j3"].llm_score is None
