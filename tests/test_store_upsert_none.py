"""VectorStore.upsert_profile_materials 在 stats=None 时不应崩。"""

from __future__ import annotations

from repo2resume.retrieval.store import VectorStore
from repo2resume.storage.db import Database
from repo2resume.storage.models import ProjectOneLiner, SkillProfile, TechStack


class _FakeEmbedder:
    model_id_or_path = "fake-embed"
    dimension = 8

    def encode(self, texts: list[str]) -> list[list[float]]:
        return [[0.01] * self.dimension for _ in texts]


def test_upsert_profile_materials_accepts_none_stats(data_dir):
    db = Database(data_dir / "store.db")
    store = VectorStore(db, _FakeEmbedder())
    profile = SkillProfile(
        primary_direction="Python backend",
        tech_stack=TechStack(languages=["Python"]),
        project_one_liners=[ProjectOneLiner(repo="demo", summary="built an agent loop")],
    )
    n = store.upsert_profile_materials(profile, None)
    assert n >= 1
