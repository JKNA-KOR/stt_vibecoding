"""ORM 모델 (Harness §9.2 요구사항, §64 / §65 스키마 예시 준수).

설계 원칙:
  * Transcript 본문과 음성은 DB 가 아니라 파일 저장소에 두고, DB 에는 메타데이터와
    참조 키만 둔다. 파일 경로는 API 응답에 노출하지 않는다 (Harness §44).
  * `audit_event` 는 Append-only 다. ORM 레벨에서 UPDATE/DELETE 를 시도하지 않으며,
    DB 트리거로 이중 차단한다 (Harness §19, migrations 참조).
  * 개인정보는 감사에 필요한 최소 식별자만 둔다 (Harness §46).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from app.audit.events import AuditEventType, AuditResult
from app.auth.roles import UserRole
from app.jobs.state import JobStatus
from app.storage.database import Base
from app.stt.schemas import TranscriptKind

# PostgreSQL 에서는 JSONB, 테스트용 SQLite 에서는 JSON 으로 동작하도록 variant 를 쓴다.
# none_as_null: 값이 없을 때 JSON 리터럴 'null' 이 아니라 SQL NULL 을 저장해야
# `IS NULL` 조회가 기대대로 동작한다.
JsonType = JSON(none_as_null=True).with_variant(JSONB(none_as_null=True), "postgresql")

# SQLite 는 "INTEGER PRIMARY KEY" 만 rowid 별칭으로 취급하므로 BIGINT PK 에는 자동증가가
# 걸리지 않는다. 운영 DB(PostgreSQL)는 BIGINT 를 그대로 쓰고 테스트 방언에서만 좁힌다.
AutoPkType = BigInteger().with_variant(Integer, "sqlite")


def utcnow() -> datetime:
    return datetime.now(UTC)


def ensure_utc(value: datetime | None) -> datetime | None:
    """DB 에서 읽은 시각을 UTC aware 로 맞춘다.

    PostgreSQL 의 timestamptz 는 tz 를 보존하지만 SQLite 방언은 naive 로 돌려준다.
    저장하는 값은 항상 `utcnow()` 이므로 naive 값에 UTC 를 붙이는 것이 안전하며,
    이렇게 해 두지 않으면 naive/aware 혼합 연산이 런타임에 터진다.
    """
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)


def new_job_id() -> str:
    return f"stt-{uuid.uuid4().hex}"


def new_uuid() -> str:
    return uuid.uuid4().hex


class User(Base):
    """서비스 사용자.

    Harness §46 에 따라 STT 처리에 불필요한 개인정보(주민번호·전화번호·주소 등)는
    컬럼 자체를 두지 않는다.
    """

    __tablename__ = "app_user"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_uuid)
    username: Mapped[str] = mapped_column(String(150), unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(150), nullable=False, default="")
    role: Mapped[UserRole] = mapped_column(String(20), nullable=False, default=UserRole.USER)
    # 외부 IdP 사용자는 password_hash 가 비어 있다 (Harness §10 SSO 확장 대비).
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    auth_provider: Mapped[str] = mapped_column(String(32), nullable=False, default="local")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    # Harness §32: 반복 로그인 실패 탐지 및 계정 잠금.
    failed_login_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )

    jobs: Mapped[list[Job]] = relationship(back_populates="owner")

    __table_args__ = (
        CheckConstraint(
            "role IN ('USER','REVIEWER','ADMIN','AUDITOR')", name="ck_app_user_role"
        ),
    )


class Job(Base):
    """STT 변환 작업 단위. Harness §65 의 Job Metadata 필드를 모두 보유한다."""

    __tablename__ = "stt_job"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_job_id)
    created_by: Mapped[str] = mapped_column(
        String(64), ForeignKey("app_user.id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[JobStatus] = mapped_column(
        String(20), nullable=False, default=JobStatus.CREATED, index=True
    )

    # --- 입력 음성 (Harness §6 / §45) ---
    # original_filename 은 표시 전용이며 저장 경로 계산에 절대 쓰이지 않는다.
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    # 저장 루트 기준 상대경로만 보관한다. 절대경로를 두면 응답 직렬화 사고 시
    # 내부 경로가 노출될 수 있다 (Harness §44).
    audio_relpath: Mapped[str | None] = mapped_column(String(512), nullable=True)
    audio_sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    audio_size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    audio_duration_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    audio_content_type: Mapped[str] = mapped_column(String(100), nullable=False, default="")

    # --- Provenance (Harness §5.3 / §20) ---
    engine: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    model_name: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    model_version: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    model_artifact_hash: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    language: Mapped[str | None] = mapped_column(String(16), nullable=True)
    detected_language: Mapped[str | None] = mapped_column(String(16), nullable=True)
    beam_size: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    vad_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    compute_type: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    device_type: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    preprocessor_version: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    normalizer_version: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    application_version: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    stt_config_version: Mapped[str] = mapped_column(String(32), nullable=False, default="")

    # --- 처리 이력 / 성능 지표 (Harness §30) ---
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, index=True
    )
    queued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    queue_wait_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    processing_duration_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    real_time_factor: Mapped[float | None] = mapped_column(Float, nullable=True)

    # --- 보관 정책 (Harness §21) ---
    audio_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    audio_deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    owner: Mapped[User] = relationship(back_populates="jobs")
    transcripts: Mapped[list[Transcript]] = relationship(
        back_populates="job", cascade="all, delete-orphan"
    )

    __table_args__ = (
        # 동일 사용자·동일 키의 중복 Job 생성을 DB 레벨에서 막는다 (Harness §26).
        UniqueConstraint("created_by", "idempotency_key", name="uq_job_owner_idempotency"),
        Index("ix_stt_job_owner_created", "created_by", "created_at"),
    )


class Transcript(Base):
    """Job 산출물 메타데이터. 본문은 파일 저장소에 있다 (Harness §44)."""

    __tablename__ = "stt_transcript"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_uuid)
    job_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("stt_job.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kind: Mapped[TranscriptKind] = mapped_column(String(20), nullable=False)

    storage_relpath: Mapped[str] = mapped_column(String(512), nullable=False)
    char_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    segment_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    language: Mapped[str | None] = mapped_column(String(16), nullable=True)

    # --- Lineage (Harness §51) ---
    processor: Mapped[str] = mapped_column(String(64), nullable=False)
    processor_version: Mapped[str] = mapped_column(String(32), nullable=False)
    # 어떤 상위 Transcript 로부터 파생되었는지. RAW 는 NULL 이다.
    input_transcript_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("stt_transcript.id", ondelete="SET NULL"), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    job: Mapped[Job] = relationship(back_populates="transcripts")

    __table_args__ = (
        UniqueConstraint("job_id", "kind", name="uq_transcript_job_kind"),
        CheckConstraint(
            "kind IN ('RAW','NORMALIZED','LLM_CORRECTED')", name="ck_transcript_kind"
        ),
    )


class AuditEvent(Base):
    """Append-only 감사 이벤트 (Harness §18 / §19 / §64).

    이 테이블에 대한 UPDATE/DELETE 는 애플리케이션 계정 권한과 DB 트리거로 이중 차단된다.
    `prev_hash` / `record_hash` 는 변조 탐지를 위한 해시 체인이다 (Harness §19 SHOULD).
    """

    __tablename__ = "audit_event"

    id: Mapped[int] = mapped_column(AutoPkType, primary_key=True, autoincrement=True)
    event_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, index=True
    )
    event_type: Mapped[AuditEventType] = mapped_column(String(64), nullable=False, index=True)
    actor_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    actor_role: Mapped[str] = mapped_column(String(20), nullable=False)
    action: Mapped[str] = mapped_column(String(128), nullable=False)
    target_type: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    target_id: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    result: Mapped[AuditResult] = mapped_column(String(16), nullable=False)
    request_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    job_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    source_ip_masked: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    application_version: Mapped[str] = mapped_column(String(32), nullable=False, default="")

    # STT 처리 이벤트 전용 추가 필드 (Harness §18).
    audio_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    model_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    model_version: Mapped[str | None] = mapped_column(String(128), nullable=True)
    stt_config_version: Mapped[str | None] = mapped_column(String(32), nullable=True)

    # 개인정보와 Secret 을 저장하지 않는다 (Harness §64).
    metadata_json: Mapped[dict | None] = mapped_column(JsonType, nullable=True)

    prev_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    record_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")

    __table_args__ = (
        Index("ix_audit_event_type_time", "event_type", "event_time"),
        Index("ix_audit_actor_time", "actor_id", "event_time"),
    )


class IdempotencyRecord(Base):
    """Job 생성 재시도 중복 방지 (Harness §26)."""

    __tablename__ = "idempotency_record"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    actor_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    endpoint: Mapped[str] = mapped_column(String(128), nullable=False)
    # 같은 키로 다른 내용을 보냈는지 판별하기 위한 요청 지문(파일 해시 등). 원문은 담지 않는다.
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    job_id: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )


class ConfigChange(Base):
    """운영 설정 변경 이력 (Harness §37).

    Secret 값 자체는 저장하지 않으며, 변경된 키가 Secret 인 경우 값 대신 마스킹 표식을 둔다.
    """

    __tablename__ = "config_change"

    id: Mapped[int] = mapped_column(AutoPkType, primary_key=True, autoincrement=True)
    changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, index=True
    )
    changed_by: Mapped[str] = mapped_column(String(64), nullable=False)
    config_key: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    previous_value: Mapped[str] = mapped_column(Text, nullable=False, default="")
    new_value: Mapped[str] = mapped_column(Text, nullable=False, default="")
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    request_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")


class RuntimeConfig(Base):
    """런타임에 변경 가능한 설정의 현재 값.

    환경변수는 기동 시점의 기본값이며, 이 테이블에 값이 있으면 그것이 우선한다.
    변경 가능한 키는 `AUDITED_CONFIG_KEYS` 로 제한된다 (Harness §37).
    """

    __tablename__ = "runtime_config"

    config_key: Mapped[str] = mapped_column(String(128), primary_key=True)
    config_value: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )
    updated_by: Mapped[str] = mapped_column(String(64), nullable=False, default="")
