"""Job / Transcript API 스키마 (SEC-001 / SEC-033).

저장 경로(`audio_relpath`, `storage_relpath`)는 어떤 응답에도 포함하지 않는다
(Harness §44, FR-T-003).
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.api.schemas.common import PageMeta
from app.jobs.state import JobStatus
from app.stt.schemas import TranscriptKind


class JobResponse(BaseModel):
    """Job 상태 응답.

    `from_attributes` 로 ORM 객체를 직접 받되, 노출 필드를 여기서 명시적으로 고정한다.
    모델에 컬럼이 추가되어도 응답에 자동으로 새지 않는다.
    """

    model_config = ConfigDict(from_attributes=True)

    id: str
    status: JobStatus
    original_filename: str
    audio_duration_seconds: float | None
    audio_size_bytes: int
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    error_code: str | None
    retry_count: int

    # 결과 재현에 필요한 정보 (Harness §20). 모델 아티팩트 해시까지는 노출하지 않는다.
    engine: str
    model_name: str
    language: str | None
    detected_language: str | None

    # 성능 지표 (Harness §30).
    queue_wait_seconds: float | None
    processing_duration_seconds: float | None
    real_time_factor: float | None

    audio_deleted_at: datetime | None


class JobListResponse(BaseModel):
    items: list[JobResponse]
    page: PageMeta


class TranscriptSummary(BaseModel):
    """Transcript 메타데이터. 본문은 별도 엔드포인트에서 받는다."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    kind: TranscriptKind
    language: str | None
    segment_count: int
    char_count: int
    processor: str
    processor_version: str
    created_at: datetime


class TranscriptSegmentResponse(BaseModel):
    index: int
    start: float
    end: float
    text: str


class TranscriptDetailResponse(BaseModel):
    job_id: str
    kind: TranscriptKind
    segments: list[TranscriptSegmentResponse]


class JobCreatedResponse(BaseModel):
    job: JobResponse


class JobListQuery(BaseModel):
    """목록 조회 파라미터. 상한은 서버가 정한다 (Harness §24)."""

    status: JobStatus | None = None
    limit: int = Field(default=20, ge=1, le=100)
    offset: int = Field(default=0, ge=0)
