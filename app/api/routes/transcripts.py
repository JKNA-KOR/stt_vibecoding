"""Transcript 조회·다운로드 라우트 (FR-T-003 ~ FR-T-008, SEC-033).

응답에 저장 경로를 담지 않는다. 본문은 Untrusted Data 이므로 실행 가능한 문서로
내보내지 않으며, 다운로드 응답은 항상 첨부파일로 처리한다 (Harness §13 / §44).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Response

from app.api.dependencies.common import job_service, rate_limited
from app.api.schemas.jobs import (
    TranscriptDetailResponse,
    TranscriptSegmentResponse,
)
from app.auth.principal import Principal
from app.jobs.service import JobService
from app.stt.formats import parse_format
from app.stt.schemas import TranscriptKind

router = APIRouter(prefix="/jobs/{job_id}/transcript", tags=["transcripts"])


@router.get("", response_model=TranscriptDetailResponse)
def get_transcript(
    job_id: str,
    kind: TranscriptKind = Query(default=TranscriptKind.NORMALIZED),
    principal: Principal = Depends(rate_limited),
    jobs: JobService = Depends(job_service),
) -> TranscriptDetailResponse:
    """Transcript 본문을 세그먼트 단위로 돌려준다.

    기본값이 NORMALIZED 인 것은 화면 표시용이기 때문이다. 원본이 필요하면 `kind=RAW`
    를 명시한다 — 두 산출물은 별도로 보관된다 (Harness §50).
    """
    segments = jobs.read_transcript(principal, job_id, kind)
    return TranscriptDetailResponse(
        job_id=job_id,
        kind=kind,
        segments=[
            TranscriptSegmentResponse(
                index=segment.index, start=segment.start, end=segment.end, text=segment.text
            )
            for segment in segments
        ],
    )


@router.get("/download")
def download_transcript(
    job_id: str,
    fmt: str = Query(default="txt", alias="format"),
    kind: TranscriptKind = Query(default=TranscriptKind.NORMALIZED),
    principal: Principal = Depends(rate_limited),
    jobs: JobService = Depends(job_service),
) -> Response:
    """TXT / SRT / VTT / JSON 으로 내보낸다 (FR-T-004)."""
    payload = jobs.download_transcript(principal, job_id, kind, parse_format(fmt))
    return Response(
        content=payload.content,
        media_type=payload.media_type,
        headers={
            # 파일명은 서버가 만든 id 로만 구성되므로 헤더 인젝션 여지가 없다.
            "Content-Disposition": f'attachment; filename="{payload.filename}"',
            # 브라우저가 내용으로 타입을 추측해 실행 가능한 문서로 다루지 못하게 한다.
            "X-Content-Type-Options": "nosniff",
        },
    )
