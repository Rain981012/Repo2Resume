"""Tests for retrieval/embedder.py."""

from __future__ import annotations

import pytest

pytest.importorskip("torch")
pytest.importorskip("transformers")

import numpy as np
import torch

from repo2resume.retrieval.embedder import LocalTransformersEmbedder, build_embedder


class _FakeTokenizer:
    def __init__(self, hidden_size: int) -> None:
        self.hidden_size = hidden_size

    def __call__(
        self, texts, *, padding=True, truncation=True, max_length=512, return_tensors="pt"
    ):
        batch_size = len(texts) if isinstance(texts, list) else 1
        return {
            "input_ids": torch.zeros(batch_size, 4, dtype=torch.long),
            "attention_mask": torch.ones(batch_size, 4, dtype=torch.long),
        }


class _FakeTokenizerClass:
    @classmethod
    def from_pretrained(cls, *args, **kwargs):
        return _FakeTokenizer(8)


class _FakeModel:
    class config:
        hidden_size = 8

    def __init__(self) -> None:
        self.config = _FakeModel.config

    def to(self, device):
        return self

    def eval(self):
        return self

    def __call__(self, **kwargs):
        batch_size = kwargs["input_ids"].size(0)
        return type(
            "Outputs",
            (),
            {
                "last_hidden_state": torch.ones(batch_size, 4, self.config.hidden_size),
            },
        )()


class _FakeModelClass:
    @classmethod
    def from_pretrained(cls, *args, **kwargs):
        return _FakeModel()


def _patch_embedder(monkeypatch):
    monkeypatch.setattr("transformers.AutoTokenizer", _FakeTokenizerClass)
    monkeypatch.setattr("transformers.AutoModel", _FakeModelClass)


def test_build_embedder_parses_local_prefix(monkeypatch):
    _patch_embedder(monkeypatch)
    emb = build_embedder("local:Qwen3-Embedding-4B", device="cpu", batch_size=2)
    assert emb.dimension == 8
    vectors = emb.encode(["hello", "world"])
    assert len(vectors) == 2
    assert len(vectors[0]) == 8


def test_normalize_and_batching(monkeypatch):
    _patch_embedder(monkeypatch)
    emb = LocalTransformersEmbedder("any-model", device="cpu", batch_size=2)
    texts = ["a", "b", "c"]
    vectors = emb.encode(texts)
    assert len(vectors) == 3
    for v in vectors:
        assert len(v) == 8
        norm = np.linalg.norm(v)
        assert pytest.approx(norm, 0.01) == 1.0


def test_empty_encode_returns_empty(monkeypatch):
    _patch_embedder(monkeypatch)
    emb = LocalTransformersEmbedder("any-model", device="cpu")
    assert emb.encode([]) == []
