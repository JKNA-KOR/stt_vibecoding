"""Job / Transcript API 스키마 (SEC-001 / SEC-033).

저장 경로(`audio_relpath`, `storage_relpath`)는 어떤 응답에도 포함하지 않는다
(Harness §44, FR-T-003).
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.api.schemas.common import PageMeta
from app.jobs.state import JobStatus
from app.llm.state import AnalysisStatus
from app.qa.state import QAStatus
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
    # 업로더가 보낸 파일이 그대로 도착했는지 확인할 수 있게 한다. 자기 파일의 해시이며
    # 민감정보가 아니다. JSON 연동 응답과 같은 값이다.
    audio_sha256: str
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    error_code: str | None
    retry_count: int

    # 분석은 전사와 별개로 진행된다. 왜 분석이 없는지 화면에서 알 수 있어야 한다.
    analysis_status: AnalysisStatus
    analysis_error_code: str | None

    # QA 도 마찬가지다. 점수가 없는 이유가 "안 돌렸다"인지 "실패했다"인지 구분되어야 한다.
    qa_status: QAStatus
    qa_error_code: str | None

    # 결과 재현에 필요한 정보 (Harness §20). 모델 아티팩트 해시까지는 노출하지 않는다.
    engine: str
    model_name: str
    language: str | None
    detected_language: str | None

    # 성능 지표 (Harness §30).
    queue_wait_seconds: float | None
    processing_duration_seconds: float | None
    real_time_factor: float | None
    # 모델이 스스로 매긴 평균 확신도(0~1). 측정된 정확도가 아니다 (Harness §20).
    transcription_confidence: float | None

    # --- 목록 화면용 QA 요약 ---
    # 평가가 없으면 전부 None 이다. 0 으로 내려 쓰면 "0점"으로 오해된다 (Harness §4.3).
    qa_overall_score: float | None
    qa_grade: str | None
    qa_compliance_score: float | None
    qa_violation_count: int | None
    qa_has_critical_violation: bool

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
    # 모델이 매긴 확신도(0~1). 값을 주지 않는 엔진에서는 null 이다.
    confidence: float | None = None
    # 발화자. **음향 기반 화자분리가 아니라 문맥 추정**이며, 후처리를 거치지 않은
    # Transcript 에서는 null 이다 (Harness §4.3).
    speaker: str | None = None


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


class BulkDeleteRequest(BaseModel):
    """일괄 삭제 요청.

    한 번에 지울 수 있는 건수에 상한을 둔다. 목록 화면에서 실수로 전체 선택한 뒤
    누르는 사고를 한 번에 크게 만들지 않기 위해서다 (Harness §24 / §48).
    """

    job_ids: list[str] = Field(min_length=1, max_length=100)


class BulkDeleteFailure(BaseModel):
    job_id: str
    message: str


class BulkDeleteResponse(BaseModel):
    """부분 실패를 그대로 돌려준다. 성공한 척하지 않는다 (Harness §4.3)."""

    deleted: list[str]
    failed: list[BulkDeleteFailure]
