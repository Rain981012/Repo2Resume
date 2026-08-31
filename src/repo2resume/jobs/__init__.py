"""Job search + matching layer."""

from __future__ import annotations

from repo2resume.jobs.salary import extract_salary_info, salary_mid_k
from repo2resume.jobs.search import (
    BochaJobSource,
    JobSource,
    LiepinMcpSource,
    MockJobSource,
    TavilyJobSource,
    explain_job_candidate,
    search_jobs,
)

__all__ = [
    "BochaJobSource",
    "JobSource",
    "LiepinMcpSource",
    "MockJobSource",
    "TavilyJobSource",
    "explain_job_candidate",
    "search_jobs",
    "extract_salary_info",
    "salary_mid_k",
]
