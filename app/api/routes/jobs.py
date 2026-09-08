"""Job 라우트 (FR-U-*, FR-J-*, SEC-010 / SEC-011 / SEC-031 / SEC-035).

권한 판단과 상태 전이는 `JobService` 가 한다. 여기서는 HTTP 만 다룬다 — 라우트가
직접 소유자를 비교하기 시작하면 규칙이 두 곳으로 갈라진다.
"""

from __future__ import annotations

from collections.abc import Iterator

from fastapi import APIRouter, Depends, File, Header, Query, Response, UploadFile, status

from app.api.dependencies.common import (
    csrf_protected,
    job_service,
    rate_limited,
    upload_rate_limited,
)
from app.api.schemas.common import PageMeta
from app.api.schemas.jobs import (
    BulkDeleteRequest,
    BulkDeleteResponse,
    JobCreatedResponse,
    JobListResponse,
    JobResponse,
    TranscriptSummary,
)
from app.auth.principal import Principal
from app.jobs.service import JobService, validate_idempotency_key
from app.jobs.state import JobStatus

router = APIRouter(prefix="/jobs", tags=["jobs"])

# 업로드 스트림을 읽는 단위. 전체를 메모리에 올리지 않는다 (Harness §24).
_READ_CHUNK_BYTES = 1024 * 1024


def _stream(upload: UploadFile) -> Iterator[bytes]:
    """UploadFile 을 청크 반복자로 바꾼다.

    파일 전체를 `read()` 하면 500MB 업로드가 그대로 메모리에 올라간다.
    """
    while chunk := upload.file.read(_READ_CHUNK_BYTES):
        yield chunk


@router.post("", response_model=JobCreatedResponse, status_code=status.HTTP_201_CREATED)
def create_job(
    file: UploadFile = File(...),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    principal: Principal = Depends(upload_rate_limited),
    jobs: JobService = Depends(job_service),
) -> JobCreatedResponse:
    """음성을 업로드하고 STT Job 을 생성한다.

    파일명은 신뢰하지 않는다. 확장자·시그니처·크기·재생시간 검증은 서비스 계층이
    수행하며, 저장 경로는 서버가 만든 UUID 로만 구성된다 (Harness §6).
    """
    job = jobs.create_job(
        principal,
        original_filename=file.filename or "unnamed",
        chunks=_stream(file),
        idempotency_key=validate_idempotency_key(idempotency_key),
    )
    return JobCreatedResponse(job=JobResponse.model_validate(job))


@router.get("", response_model=JobListResponse)
def list_jobs(
    job_status: JobStatus | None = Query(default=None, alias="status"),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    principal: Principal = Depends(rate_limited),
    jobs: JobService = Depends(job_service),
) -> JobListResponse:
    """조회 권한 범위 안의 Job 목록. USER 는 본인 것만 본다 (SEC-011)."""
    page = jobs.list_jobs(principal, status=job_status, limit=limit, offset=offset)
    return JobListResponse(
        items=[JobResponse.model_validate(job) for job in page.items],
        page=PageMeta(total=page.total, limit=page.limit, offset=page.offset),
    )


@router.get("/{job_id}", response_model=JobResponse)
def get_job(
    job_id: str,
    principal: Principal = Depends(rate_limited),
    jobs: JobService = Depends(job_service),
) -> JobResponse:
    """단건 조회. 권한이 없는 Job 은 존재 여부도 알려주지 않는다 (Harness §44)."""
    return JobResponse.model_validate(jobs.get_job(principal, job_id))


@router.get("/{job_id}/transcripts", response_model=list[TranscriptSummary])
def list_transcripts(
    job_id: str,
    principal: Principal = Depends(rate_limited),
    jobs: JobService = Depends(job_service),
) -> list[TranscriptSummary]:
    """Job 의 Transcript 메타데이터 목록. 본문은 포함하지 않는다."""
    return [
        TranscriptSummary.model_validate(row)
        for row in jobs.list_transcripts(principal, job_id)
    ]


@router.post("/{job_id}/cancel", response_model=JobResponse)
def cancel_job(
    job_id: str,
    principal: Principal = Depends(csrf_protected),
    jobs: JobService = Depends(job_service),
) -> JobResponse:
    return JobResponse.model_validate(jobs.cancel_job(principal, job_id))


@router.post("/{job_id}/retry", response_model=JobResponse)
def retry_job(
    job_id: str,
    principal: Principal = Depends(csrf_protected),
    jobs: JobService = Depends(job_service),
) -> JobResponse:
    """실패한 Job 을 다시 큐에 넣는다. 재시도 횟수는 설정 상한을 넘지 못한다 (Harness §24)."""
    return JobResponse.model_validate(jobs.retry_job(principal, job_id))


@router.post("/bulk-delete", response_model=BulkDeleteResponse)
def bulk_delete_jobs(
    payload: BulkDeleteRequest,
    principal: Principal = Depends(csrf_protected),
    jobs: JobService = Depends(job_service),
) -> BulkDeleteResponse:
    """여러 상담을 한 번에 지운다 (목록 화면의 일괄 삭제).

    DELETE 대신 POST 를 쓴다. 삭제 대상 목록을 본문으로 보내야 하는데, DELETE 요청의
    본문은 프록시·게이트웨이가 버리는 경우가 있어 조용히 아무것도 지워지지 않는다.

    부분 실패를 허용한다 — 무엇이 왜 안 지워졌는지 돌려주어야 사용자가 다음 행동을
    정할 수 있다 (Harness §4.3).
    """
    result = jobs.delete_jobs(principal, payload.job_ids)
    return BulkDeleteResponse(**result)


@router.delete("/{job_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_job(
    job_id: str,
    principal: Principal = Depends(csrf_protected),
    jobs: JobService = Depends(job_service),
) -> None:
    """상담을 통째로 지운다. 음성·전사·분석·QA 가 함께 사라지며 되돌릴 수 없다."""
    jobs.delete_job(principal, job_id)


@router.delete("/{job_id}/audio", status_code=status.HTTP_204_NO_CONTENT)
def delete_audio(
    job_id: str,
    principal: Principal = Depends(csrf_protected),
    jobs: JobService = Depends(job_service),
) -> Response:
    """음성 원본만 삭제한다. Transcript 삭제와는 별도 이벤트를 남긴다 (FR-T-009)."""
    jobs.delete_audio(principal, job_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("/{job_id}/transcripts", status_code=status.HTTP_204_NO_CONTENT)
def delete_transcripts(
    job_id: str,
    principal: Principal = Depends(csrf_protected),
    jobs: JobService = Depends(job_service),
) -> Response:
    jobs.delete_transcripts(principal, job_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
