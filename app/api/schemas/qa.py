"""QA API 스키마 (SEC-001 / SEC-033).

루브릭 본문은 평가 결과 응답에 담지 않는다. 어떤 기준이었는지는 해시(`rubric_version`)로
드러내고, 본문은 관리 화면의 설정 조회로만 나간다 — 일반 사용자 응답에 평가 기준 전문이
실릴 이유가 없다 (Harness §44).
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from app.api.schemas.common import PageMeta


class ScoreItemResponse(BaseModel):
    category: str
    score: float
    max_score: float
    comment: str


class ViolationResponse(BaseModel):
    rule: str
    severity: str
    # 근거 발화는 녹취 원문의 인용이다. Confidential 이며 화면에서 textContent 로만 넣는다.
    evidence: str
    comment: str


class QAResponse(BaseModel):
    """상담 품질 평가 결과."""

    job_id: str
    original_filename: str
    overall_score: float
    grade: str
    compliance_score: float
    has_critical_violation: bool
    violation_count: int
    score_items: list[ScoreItemResponse]
    violations: list[ViolationResponse]
    strengths: list[str]
    improvements: list[str]
    summary: str
    provider: str
    model_name: str
    prompt_version: str
    # 어떤 기준으로 매긴 점수인지. 기준이 바뀌면 값이 달라진다 (Harness §20).
    rubric_version: str
    compliance_version: str
    transcript_truncated: bool
    warnings: list[str]
    created_at: datetime


class QASummaryResponse(BaseModel):
    job_id: str
    original_filename: str
    overall_score: float
    grade: str
    compliance_score: float
    has_critical_violation: bool
    violation_count: int
    created_at: datetime


class QAListResponse(BaseModel):
    items: list[QASummaryResponse]
    page: PageMeta


class QAStatsResponse(BaseModel):
    """대시보드 집계.

    평균이 `None` 이면 "평가 없음"이다. 0 과 구분되어야 한다 — 화면이 0점으로 그리면
    운영자가 품질이 바닥이라고 읽는다 (Harness §4.3).
    """

    evaluated_count: int
    average_score: float | None
    average_compliance: float | None
    critical_count: int
    grade_counts: dict[str, int]
    severity_counts: dict[str, int]


class RubricProfileResponse(BaseModel):
    """관리 화면에서 고를 수 있는 기본 원칙 묶음."""

    key: str
    label: str
    description: str
    rubric: str = Field(max_length=40000)
    compliance: str = Field(max_length=40000)
