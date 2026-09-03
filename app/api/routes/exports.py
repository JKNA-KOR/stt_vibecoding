"""JSON 연동 라우트 (Harness §6 / §11 / §24 / §43).

`/jobs/json` 은 base64 로 감싼 음성을 받고, `/jobs/{id}/export` 는 Job·Transcript·분석을
한 문서로 내보낸다. 둘 다 설정으로 끌 수 있다.
"""

from __future__ import annotations

from dataclasses import replace

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from app.api.dependencies.common import (
    db_session,
    job_service,
    rate_limited,
    settings_dep,
    upload_rate_limited,
)
from app.api.schemas.intake import ExportBundle, JsonAudioUploadRequest, JsonUploadResponse
from app.auth.principal import Principal
from app.core.config import Settings
from app.core.exceptions import ValidationError
from app.jobs.intake import ExportOptions, ExportService, decode_audio_chunks
from app.jobs.service import JobService, validate_idempotency_key
from app.storage.transcript import TranscriptStore
from app.stt.schemas import TranscriptKind

router = APIRouter(tags=["json"])


def export_service(
    session: Session = Depends(db_session),
    settings: Settings = Depends(settings_dep),
) -> ExportService:
    return ExportService(
        session, settings=settings, transcript_store=TranscriptStore(settings)
    )


@router.post(
    "/jobs/json", response_model=JsonUploadResponse, status_code=status.HTTP_201_CREATED
)
def create_job_from_json(
    payload: JsonAudioUploadRequest,
    principal: Principal = Depends(upload_rate_limited),
    jobs: JobService = Depends(job_service),
    settings: Settings = Depends(settings_dep),
) -> JsonUploadResponse:
    """base64 음성을 받아 Job 을 만든다.

    검증은 multipart 경로와 같은 `create_job` 을 지난다. 여기서 하는 일은 base64 를
    풀어 청크로 흘려보내는 것뿐이다 — 입구가 둘이어도 규칙은 하나여야 한다.

    Raises:
        ValidationError: 기능이 꺼져 있거나 base64 형식이 아닌 경우.
    """
    if not settings.enable_json_upload:
        raise ValidationError(
            "JSON 업로드가 비활성화되어 있습니다.",
            internal_detail="ENABLE_JSON_UPLOAD is false",
        )

    job = jobs.create_job(
        principal,
        original_filename=payload.filename,
        chunks=decode_audio_chunks(
            payload.audio_base64, max_bytes=settings.json_upload_max_mb * 1024 * 1024
        ),
        idempotency_key=validate_idempotency_key(payload.idempotency_key),
    )
    return JsonUploadResponse(
        job_id=job.id,
        status=job.status,
        analysis_status=job.analysis_status,
        original_filename=job.original_filename,
        audio_duration_seconds=job.audio_duration_seconds,
        audio_size_bytes=job.audio_size_bytes,
        audio_sha256=job.audio_sha256,
    )


@router.get(
    "/jobs/{job_id}/export",
    response_model=ExportBundle,
    # 서비스가 담지 않은 키는 응답에서도 빠진다. 기본값 None 으로 채우면 소비하는 쪽이
    # "요청하지 않음"과 "값이 없음"을 구분하지 못한다. dict 에 있는 null 은 그대로 나간다.
    response_model_exclude_unset=True,
)
def export_job(
    job_id: str,
    include_transcript: bool | None = Query(default=None),
    include_segments: bool | None = Query(default=None),
    include_analysis: bool | None = Query(default=None),
    kind: TranscriptKind = Query(default=TranscriptKind.NORMALIZED),
    principal: Principal = Depends(rate_limited),
    service: ExportService = Depends(export_service),
    settings: Settings = Depends(settings_dep),
) -> dict:
    """Job · Transcript · 분석을 한 JSON 문서로 내보낸다.

    각 구성요소는 설정 기본값을 따르고 쿼리 파라미터로 덮어쓸 수 있다. 요청하지 않은
    것은 물론, 아직 만들어지지 않은 것도 키 자체가 빠진다 — 빈 값으로 채우면 소비하는
    쪽이 "없음"과 "비어 있음"을 구분하지 못한다.
    """
    if not settings.enable_json_export:
        raise ValidationError(
            "JSON 내보내기가 비활성화되어 있습니다.",
            internal_detail="ENABLE_JSON_EXPORT is false",
        )

    options = ExportOptions.from_settings(settings)
    overrides = {"kind": kind}
    if include_transcript is not None:
        overrides["include_transcript"] = include_transcript
    if include_segments is not None:
        overrides["include_segments"] = include_segments
    if include_analysis is not None:
        overrides["include_analysis"] = include_analysis

    return service.export(principal, job_id, replace(options, **overrides))
