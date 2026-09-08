"""QA 라우트.

권한과 감사는 `QAService` 가 판단한다. 여기서는 HTTP 만 다룬다.

경로가 두 갈래인 이유는 화면 구성이 그렇기 때문이다.
  * `/jobs/{id}/qa`  — 상담 하나의 평가 (상담 상세 화면)
  * `/qa/...`        — 여러 상담을 가로지르는 목록·집계 (상담 점수 / 컴플라이언스 화면)
"""

from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.dependencies.common import csrf_protected, db_session, rate_limited, settings_dep
from app.api.schemas.common import PageMeta
from app.api.schemas.jobs import JobResponse
from app.api.schemas.qa import (
    QAListResponse,
    QAResponse,
    QAStatsResponse,
    QASummaryResponse,
)
from app.auth.principal import Principal
from app.core.config import Settings
from app.qa.service import QAService
from app.storage.transcript import TranscriptStore

job_router = APIRouter(prefix="/jobs/{job_id}/qa", tags=["qa"])
router = APIRouter(prefix="/qa", tags=["qa"])


def qa_service(
    session: Session = Depends(db_session),
    settings: Settings = Depends(settings_dep),
) -> QAService:
    return QAService(session, settings=settings, transcript_store=TranscriptStore(settings))


@job_router.get("", response_model=QAResponse)
def get_evaluation(
    job_id: str,
    principal: Principal = Depends(rate_limited),
    service: QAService = Depends(qa_service),
) -> QAResponse:
    """평가 결과를 조회한다. 아직 없으면 404 다."""
    return QAResponse(**asdict(service.get_evaluation(principal, job_id)))


@job_router.post("", response_model=JobResponse)
def request_evaluation(
    job_id: str,
    principal: Principal = Depends(csrf_protected),
    service: QAService = Depends(qa_service),
) -> JobResponse:
    """평가를 요청한다. 이미 결과가 있으면 새 결과로 대체된다."""
    return JobResponse.model_validate(service.request_evaluation(principal, job_id))


@router.get("/evaluations", response_model=QAListResponse)
def list_evaluations(
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    only_violations: bool = Query(default=False),
    principal: Principal = Depends(rate_limited),
    service: QAService = Depends(qa_service),
) -> QAListResponse:
    """평가 목록. 조회 권한 범위 안의 상담만 보인다 (SEC-011)."""
    items, total = service.list_evaluations(
        principal, limit=limit, offset=offset, only_violations=only_violations
    )
    return QAListResponse(
        items=[QASummaryResponse(**asdict(item)) for item in items],
        page=PageMeta(total=total, limit=limit, offset=offset),
    )


@router.get("/stats", response_model=QAStatsResponse)
def qa_stats(
    principal: Principal = Depends(rate_limited),
    service: QAService = Depends(qa_service),
) -> QAStatsResponse:
    """QA 집계. 화면 상단 요약 상자가 쓴다."""
    return QAStatsResponse(**asdict(service.stats(principal)))
