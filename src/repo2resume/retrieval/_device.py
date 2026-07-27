"""Device resolution helper: cuda > mps > cpu, with lazy torch import.

为什么单独放一个模块？
  - embedder.py 和 rerank.py 都需要选 device，但不想让它们互相依赖。
  - 这个模块用懒导入 torch（只在 resolve_device 被调用时才 import torch），
    所以 import 这个模块本身不会触发 torch 加载——让 LLM rerank 的测试
    可以在没装 torch 的环境里跑。
"""

from __future__ import annotations


def resolve_device(device: str | None) -> str:
    """Pick the best available torch device: cuda > mps > cpu.

    为什么不只用 cuda？
      - Apple Silicon（M1-M4）没有 CUDA，但有 MPS（Metal Performance Shaders）GPU 加速。
      - 只检测 cuda 会让 Mac 用户落到 CPU，浪费 GPU。
    """
    if device:
        return device
    import torch

    if torch.cuda.is_available():
        return "cuda"
    if torch.backends.mps.is_available():
        return "mps"
    return "cpu"
