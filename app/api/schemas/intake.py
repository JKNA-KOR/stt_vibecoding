"""JSON 연동 스키마 (SEC-001, Harness §6 / §11).

multipart 를 쓰기 어려운 외부 시스템을 위한 입출력 형식이다. 입력 검증은 multipart
경로와 같은 코드를 지나므로, 이 스키마는 "어떻게 담겨 오는가"만 정의한다.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator


class JsonAudioUploadRequest(BaseModel):
    """base64 로 감싼 음성 업로드.

    `audio_base64` 는 원본보다 약 33% 크다. 서버는 `JSON_UPLOAD_MAX_MB` 를 **디코딩된
    원본 크기** 기준으로 적용하며, 요청 본문 자체의 상한은 그보다 넉넉하게 잡는다.
    """

    # 표시 전용이다. 저장 경로 계산에는 절대 쓰이지 않는다 (Harness §6).
    filename: str = Field(min_length=1, max_length=255)
    audio_base64: str = Field(min_length=1)
    # 참고용. 실제 형식 판정은 파일 시그니처로 한다 — 클라이언트 선언을 믿지 않는다.
    content_type: str | None = Field(default=None, max_length=100)
    # 같은 키로 두 번 보내도 Job 이 하나만 생긴다 (Harness §26).
    idempotency_key: str | None = Field(default=None, max_length=128)
    # 전사 언어. 생략하면 서버 기본값을 쓴다.
    language: str | None = Field(default=None, max_length=16)

    @field_validator("audio_base64")
    @classmethod
    def _strip_whitespace(cls, value: str) -> str:
        """줄바꿈으로 감싼 base64 도 받아들인다.

        MIME 관례상 76자마다 줄바꿈이 들어오는 경우가 흔하다. 공백을 제거해 두면
        디코딩 단계에서 엄격 검증(`validate=True`)을 그대로 쓸 수 있다.
        """
        return "".join(value.split())


class JsonUploadResponse(BaseModel):
    """접수 결과. 전사는 비동기이므로 상태는 QUEUED 로 시작한다."""

    job_id: str
    status: str
    analysis_status: str
    original_filename: str
    audio_duration_seconds: float | None
    audio_size_bytes: int
    audio_sha256: str


class ExportSegment(BaseModel):
    index: int
    start: float
    end: float
    text: str


class ExportTranscript(BaseModel):
    kind: str
    language: str | None
    processor: str
    processor_version: str
    segment_count: int
    char_count: int
    # 전문. 세그먼트를 빼도 본문은 남도록 별도로 담는다.
    text: str
    segments: list[ExportSegment] | None = None


class ExportAnalysis(BaseModel):
    summary: list[str]
    keywords: list[str]
    action_items: list[str]
    customer_reaction: str
    contact_classification: str
    sentiment: str
    opinion: str
    provider: str
    model_name: str
    prompt_version: str
    transcript_truncated: bool
    warnings: list[str]
    created_at: datetime


class ExportJob(BaseModel):
    """Job 메타데이터와 재현 정보 (Harness §20). 저장 경로는 담지 않는다 (§44)."""

    id: str
    status: str
    analysis_status: str
    original_filename: str
    audio_sha256: str
    audio_size_bytes: int
    audio_duration_seconds: float | None
    created_at: datetime
    completed_at: datetime | None
    engine: str
    model_name: str
    model_version: str
    language: str | None
    detected_language: str | None
    stt_config_version: str
    preprocessor_version: str
    normalizer_version: str
    application_version: str
    real_time_factor: float | None


class ExportBundle(BaseModel):
    """Job · Transcript · 분석을 한 문서로 묶은 결과.

    `schema_version` 은 소비하는 쪽이 형식 변화를 감지할 수 있게 한다. 필드를 빼거나
    의미를 바꾸면 올린다 (Harness §36).
    """

    schema_version: str
    exported_at: datetime
    job: ExportJob
    transcript: ExportTranscript | None = None
    analysis: ExportAnalysis | None = None

    def model_dump_json_safe(self) -> dict[str, Any]:
        return self.model_dump(mode="json")
