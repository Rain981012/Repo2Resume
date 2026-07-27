"""本地 transformers 嵌入模型封装（Qwen3-Embedding-4B 等）。

【AI 生成】模块：依赖 `transformers` + `torch`，按配置 `local:{model_id}` 直接加载。
对外只暴露统一的 `Embedder` Protocol：`encode(texts) -> list[list[float]]` + `dimension`。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from repo2resume.retrieval._device import resolve_device

if TYPE_CHECKING:
    import torch

logger = logging.getLogger(__name__)


@runtime_checkable
class Embedder(Protocol):
    """统一嵌入接口：向量库不关心底层是 OpenAI、Ollama 还是 transformers。"""

    @property
    def dimension(self) -> int:
        """嵌入维度，用于 ChromaDB collection 初始化。"""
        ...

    def encode(self, texts: list[str]) -> list[list[float]]:
        """返回与输入顺序一致的稠密向量列表。"""
        ...


def _mean_pooling(last_hidden_state: torch.Tensor, attention_mask: torch.Tensor) -> torch.Tensor:
    """Mean pooling with attention mask, ignoring padding tokens."""
    import torch

    mask = attention_mask.unsqueeze(-1).expand(last_hidden_state.size()).float()
    summed = torch.sum(last_hidden_state * mask, dim=1)
    counts = torch.clamp(mask.sum(dim=1), min=1e-9)
    return summed / counts


def _normalize(vectors: torch.Tensor) -> torch.Tensor:
    """L2 normalize so cosine similarity = dot product."""
    import torch

    return torch.nn.functional.normalize(vectors, p=2, dim=1)


class LocalTransformersEmbedder:
    """本地 transformers 嵌入模型：懒加载 + batch encode + 自动设备选择。"""

    def __init__(
        self,
        model_id_or_path: str,
        *,
        device: str | None = None,
        batch_size: int = 8,
        max_length: int = 512,
        trust_remote_code: bool = True,
    ) -> None:
        self.model_id_or_path = model_id_or_path
        self.device = resolve_device(device)
        self.batch_size = max(1, batch_size)
        self.max_length = max_length
        self.trust_remote_code = trust_remote_code
        self._tokenizer: Any | None = None
        self._model: Any | None = None
        self._dimension: int | None = None

    def _load(self) -> None:
        """Lazy load model and tokenizer once."""
        if self._model is not None:
            return
        import torch
        from transformers import AutoModel, AutoTokenizer

        logger.info("Loading local embedding model: %s on %s", self.model_id_or_path, self.device)
        self._tokenizer = AutoTokenizer.from_pretrained(
            self.model_id_or_path,
            trust_remote_code=self.trust_remote_code,
        )
        self._model = AutoModel.from_pretrained(
            self.model_id_or_path,
            trust_remote_code=self.trust_remote_code,
        )
        self._model.to(self.device)
        self._model.eval()
        # Prefer hidden_size from config; fallback to a dummy forward pass.
        hidden_size = getattr(self._model.config, "hidden_size", None)
        if hidden_size is None:
            with torch.no_grad():
                inputs = self._tokenizer(
                    "probe", return_tensors="pt", truncation=True, max_length=self.max_length
                )
                inputs = {k: v.to(self.device) for k, v in inputs.items()}
                outputs = self._model(**inputs)
                hidden_size = outputs.last_hidden_state.size(-1)
        self._dimension = int(hidden_size)
        logger.info("Embedding dimension: %d", self._dimension)

    @property
    def dimension(self) -> int:
        self._load()
        return self._dimension or 0

    def encode(self, texts: list[str]) -> list[list[float]]:
        """Encode a list of texts into normalized dense vectors."""
        if not texts:
            return []
        self._load()
        all_embeddings: list[torch.Tensor] = []
        for i in range(0, len(texts), self.batch_size):
            batch = texts[i : i + self.batch_size]
            all_embeddings.extend(self._encode_batch(batch))
        return [v.tolist() for v in all_embeddings]

    def _encode_batch(self, texts: Sequence[str]) -> list[torch.Tensor]:
        """Encode a single batch, returning a list of 1-D tensor vectors."""
        import torch

        encoded = self._tokenizer(
            list(texts),
            padding=True,
            truncation=True,
            max_length=self.max_length,
            return_tensors="pt",
        )
        encoded = {k: v.to(self.device) for k, v in encoded.items()}
        with torch.no_grad():
            outputs = self._model(**encoded)
        vectors = _mean_pooling(outputs.last_hidden_state, encoded["attention_mask"])
        vectors = _normalize(vectors)
        return [vectors[i] for i in range(vectors.size(0))]


def build_embedder(
    model_spec: str,
    *,
    device: str | None = None,
    batch_size: int = 8,
) -> Embedder:
    """Construct an embedder from a config string.

    Supported specs:
      - "local:Qwen3-Embedding-4B" -> LocalTransformersEmbedder
      - bare model id like "Qwen3-Embedding-4B" -> treated as local
    """
    if model_spec.startswith("local:"):
        model_id = model_spec.split(":", 1)[1]
        return LocalTransformersEmbedder(model_id, device=device, batch_size=batch_size)
    if model_spec.startswith(("openai/", "ollama/", "text-embedding")):
        raise NotImplementedError(
            f"Non-local embedder '{model_spec}' is not implemented in Phase 3; "
            "use local:MODEL_ID with transformers."
        )
    return LocalTransformersEmbedder(model_spec, device=device, batch_size=batch_size)
