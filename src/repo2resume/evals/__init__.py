"""Evaluation utilities: retrieval metrics, LLM-as-judge, runner."""

from __future__ import annotations

from repo2resume.evals.retrieval_metrics import compute_mrr, compute_recall_at_k
from repo2resume.evals.runner import (
    SuiteResult,
    compare_runs,
    run_all,
    run_resume_e2e_suite,
    save_run,
)

__all__ = [
    "compute_recall_at_k",
    "compute_mrr",
    "SuiteResult",
    "run_all",
    "run_resume_e2e_suite",
    "save_run",
    "compare_runs",
]
