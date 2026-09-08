"""연동 전문(message) 형식 (docs/INTERFACE.md).

**이 파일이 규격의 단일 출처다.** 문서와 코드가 갈라지면 문서가 거짓말을 하게 되므로,
전문 구조는 여기 정의된 것만 쓰고 문서는 이것을 설명한다.

전문에는 재현에 필요한 정보를 담되 **내부 구조는 담지 않는다** — 저장 경로, 프롬프트
본문, API 키는 어떤 전문에도 나가지 않는다 (Harness §44).
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

# 전문 형식 버전. 필드가 빠지거나 의미가 바뀌면 올린다. 추가만 하는 변경은 올리지 않는다
# — 소비하는 쪽이 모르는 필드를 무시하도록 규격에 적어 두었다 (Harness §36).
MESSAGE_VERSION = "1.0"


class MessageEnvelope(BaseModel):
    """모든 송신 전문의 공통 봉투.

    `message_id` 는 수신 측 멱등 처리를 위한 것이다. 재시도로 같은 전문이 두 번 도착할
    수 있으므로, 수신 측은 이 값으로 중복을 걸러야 한다 (규격서에 명시).
    """

    message_type: Literal["STT_RESULT"]
    message_version: str = MESSAGE_VERSION
    message_id: str
    sent_at: datetime
    # 재시도 회차. 0 이 최초 발송이다. 수신 측 로그 대조에 쓴다.
    attempt: int = 0


class OutboundJob(BaseModel):
    """전사 대상과 처리 결과 요약."""

    job_id: str
    original_filename: str
    status: str
    audio_sha256: str
    audio_duration_seconds: float | None
    created_at: datetime
    completed_at: datetime | None
    engine: str
    model_name: str
    detected_language: str | None
    # 모델이 스스로 매긴 확신도(0~1). 측정된 정확도가 아니다.
    transcription_confidence: float | None
    # 녹취서버에서 받은 원본 식별자. 수집 경로로 들어온 건에만 있다.
    source_reference: str | None = None


class OutboundSegment(BaseModel):
    index: int
    start: float
    end: float
    text: str
    confidence: float | None = None


class OutboundTranscript(BaseModel):
    kind: str
    language: str | None
    segment_count: int
    char_count: int
    text: str
    segments: list[OutboundSegment]


class OutboundAnalysis(BaseModel):
    summary: list[str]
    keywords: list[str]
    action_items: list[str]
    customer_reaction: str
    contact_classification: str
    sentiment: str
    opinion: str
    model_name: str
    prompt_version: str


class OutboundQA(BaseModel):
    overall_score: float
    grade: str
    compliance_score: float
    violation_count: int
    has_critical_violation: bool
    score_items: list[dict]
    violations: list[dict]
    strengths: list[str]
    improvements: list[str]
    summary: str
    # 어떤 기준으로 매긴 점수인지. 기준이 바뀌면 값이 달라진다.
    rubric_version: str
    compliance_version: str


class STTResultMessage(MessageEnvelope):
    """STT → 타 솔루션 송신 전문.

    요청하지 않았거나 아직 없는 구성요소는 **키 자체가 빠진다.** null 로 채우면 소비하는
    쪽이 "보내지 않음"과 "값이 없음"을 구분하지 못한다 (JSON 내보내기와 같은 규칙).
    """

    message_type: Literal["STT_RESULT"] = "STT_RESULT"
    job: OutboundJob
    transcript: OutboundTranscript | None = None
    analysis: OutboundAnalysis | None = None
    qa: OutboundQA | None = None


class RecordingItem(BaseModel):
    """녹취서버 → STT 수신 전문의 한 건.

    필드 이름은 설정으로 매핑할 수 있다 (`RECORDING_FIELD_*`). 이미 운영 중인 녹취서버를
    우리 규격에 맞춰 고치라고 할 수 없는 경우가 많기 때문이다.
    """

    # 녹취서버가 부여한 식별자. 같은 건을 두 번 가져오지 않기 위한 멱등 키가 된다.
    id: str = Field(min_length=1, max_length=128)
    filename: str = Field(min_length=1, max_length=255)
    # 음성을 내려받을 주소. 상대경로면 RECORDING_API_BASE_URL 기준으로 해석한다.
    download_url: str = Field(min_length=1, max_length=2000)
    recorded_at: datetime | None = None
    duration_seconds: float | None = None
    agent_id: str | None = Field(default=None, max_length=64)
    customer_ref: str | None = Field(default=None, max_length=64)


class RecordingListResponse(BaseModel):
    """녹취서버 목록 조회 응답."""

    items: list[RecordingItem]


class ExternalSTTSegment(BaseModel):
    """타사 STT 엔진 응답의 한 조각."""

    start: float
    end: float
    text: str
    confidence: float | None = None


class ExternalSTTResponse(BaseModel):
    """타사 STT 엔진 응답 전문.

    `duration` 과 `language` 가 없으면 서비스가 세그먼트에서 유추한다 — 없는 값을
    지어내지는 않는다.
    """

    segments: list[ExternalSTTSegment]
    language: str | None = None
    duration: float | None = None
