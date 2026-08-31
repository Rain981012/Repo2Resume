#!/usr/bin/env python3
"""跑 hybrid / LLM rerank / cross-encoder 三方对照，写入 evals/results/rerank_ab.json。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from repo2resume.evals.rerank_bench import run_rerank_ab, write_rerank_ab  # noqa: E402


def _try_llm(*, skip: bool):
    if skip:
        return None, "skipped by --skip-llm"
    try:
        from repo2resume.config import load_config
        from repo2resume.llm.client import LLMClient
        from repo2resume.retrieval.rerank import LLMReranker

        cfg = load_config()
        llm = LLMClient(cfg, cache=None)
        return LLMReranker(llm), None
    except Exception as exc:  # noqa: BLE001
        return None, f"LLM reranker unavailable: {exc}"


def _try_cross_encoder(*, skip: bool, model_id: str):
    if skip:
        return None, "skipped by --skip-cross-encoder"
    try:
        from repo2resume.retrieval.rerank import CrossEncoderReranker

        return CrossEncoderReranker(model_id=model_id), None
    except Exception as exc:  # noqa: BLE001
        return None, f"cross-encoder unavailable: {exc}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-llm", action="store_true")
    parser.add_argument("--skip-cross-encoder", action="store_true")
    parser.add_argument("--cross-encoder-model", default="BAAI/bge-reranker-v2-m3")
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="default: evals/results/rerank_ab.json",
    )
    args = parser.parse_args()

    llm, llm_reason = _try_llm(skip=args.skip_llm)
    ce, ce_reason = _try_cross_encoder(
        skip=args.skip_cross_encoder, model_id=args.cross_encoder_model
    )
    reasons = {}
    if llm is None:
        reasons["llm_rerank"] = llm_reason or "unavailable"
    if ce is None:
        reasons["cross_encoder"] = ce_reason or "unavailable"

    payload = run_rerank_ab(
        llm_reranker=llm,
        cross_encoder=ce,
        skip_reasons=reasons,
    )
    path = write_rerank_ab(payload, args.out)
    print(json_summary(payload, path))
    return 0


def json_summary(payload: dict, path: Path) -> str:
    import json

    lines = [f"wrote {path}", json.dumps(payload["setup"], ensure_ascii=False)]
    for name, body in payload["variants"].items():
        lines.append(f"{name}: {json.dumps(body, ensure_ascii=False)}")
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
