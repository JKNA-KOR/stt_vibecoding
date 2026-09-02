"""분석 라우트 (FR-T-010).

권한과 감사는 `AnalysisService` 가 판단한다. 여기서는 HTTP 만 다룬다.
"""

from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.dependencies.common import csrf_protected, db_session, rate_limited, settings_dep
from app.api.schemas.analysis import AnalysisResponse
from app.api.schemas.jobs import JobResponse
from app.auth.principal import Principal
from app.core.config import Settings
from app.llm.service import AnalysisService
from app.storage.transcript import TranscriptStore

router = APIRouter(prefix="/jobs/{job_id}/analysis", tags=["analysis"])


def analysis_service(
    session: Session = Depends(db_session),
    settings: Settings = Depends(settings_dep),
) -> AnalysisService:
    return AnalysisService(
        session, settings=settings, transcript_store=TranscriptStore(settings)
    )


@router.get("", response_model=AnalysisResponse)
def get_analysis(
    job_id: str,
    principal: Principal = Depends(rate_limited),
    service: AnalysisService = Depends(analysis_service),
) -> AnalysisResponse:
    """분석 결과를 조회한다. 아직 없으면 404 다."""
    view = service.get_analysis(principal, job_id)
    return AnalysisResponse(**asdict(view))


@router.post("", response_model=JobResponse)
def request_analysis(
    job_id: str,
    principal: Principal = Depends(csrf_protected),
    service: AnalysisService = Depends(analysis_service),
) -> JobResponse:
    """분석을 요청한다. 이미 결과가 있으면 새 결과로 대체된다."""
    return JobResponse.model_validate(service.request_analysis(principal, job_id))
