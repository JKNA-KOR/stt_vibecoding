"""애플리케이션 설정 단일 출처.

Harness §5.2 / §34 에 따라 모든 운영 파라미터는 환경변수로 외부화한다.
소스 코드에 환경별 값을 하드코딩하지 않으며, 설정 접근은 항상 `get_settings()` 를 거친다.
"""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Harness §37 에 따라 변경 시 감사 대상이 되는 설정 키.
# 런타임 변경 API 는 이 목록에 포함된 키만 수정할 수 있다.
AUDITED_CONFIG_KEYS: frozenset[str] = frozenset(
    {
        "stt_model_name",
        "stt_language",
        "stt_beam_size",
        "stt_vad_enabled",
        "stt_max_audio_minutes",
        "stt_max_upload_mb",
        "stt_max_concurrent_jobs",
        "audio_retention_days",
        "transcript_retention_days",
        "audit_retention_days",
        # LLM 분석은 결과 해석을 바꾸므로 변경 이력을 남긴다 (Harness §37).
        "enable_llm_analysis",
        # QA 는 상담원 평가에 쓰인다. 언제 켜고 껐는지가 남아야 한다 (Harness §37).
        "enable_qa",
        "qa_auto_run",
        # 마이크 접근을 여는 결정이다. 언제 켜고 껐는지가 남아야 한다 (Harness §37).
        "enable_realtime_stt",
        # 후처리는 화면에 보이는 문장을 바꾼다. 언제부터 적용되었는지가 남아야 한다.
        "enable_llm_correction",
        "enable_diarization",
        "llm_model_name",
    }
)

# STT 결과 재현성(Harness §20)을 좌우하는 설정 키.
# 이 조합의 해시가 `stt_config_version` 이 되어 Job 메타데이터와 Audit 에 기록된다.
_STT_CONFIG_VERSION_KEYS: tuple[str, ...] = (
    "stt_engine",
    "stt_model_name",
    "stt_device",
    "stt_compute_type",
    "stt_language",
    "stt_beam_size",
    "stt_vad_enabled",
    # 이 값이 바뀌면 같은 음성에서 다른 세그먼트 수가 나온다 (Harness §20).
    "stt_min_segment_confidence",
)


class Environment(StrEnum):
    LOCAL = "local"
    DEV = "dev"
    TEST = "test"
    PROD = "prod"


class ConfigurationError(RuntimeError):
    """설정이 Harness 규칙을 위반해 기동할 수 없는 상태.

    설정 오류는 런타임에 조용히 넘어가면 안 되는 종류이므로 (Harness §4.3)
    애플리케이션 기동 시점에 명시적으로 실패시킨다.
    """


class Settings(BaseSettings):
    """환경변수에서 로드되는 애플리케이션 설정."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- 환경 ---------------------------------------------------------------
    app_env: Environment = Environment.LOCAL
    app_name: str = "stt-service"
    app_version: str = "1.0.0"
    git_commit: str = "unknown"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    debug: bool = False

    # --- 서버 ---------------------------------------------------------------
    host: str = "127.0.0.1"
    port: int = 8000
    cors_allow_origins: str = ""

    # --- 인증 / 세션 ---------------------------------------------------------
    auth_provider: Literal["local", "oidc", "ldap"] = "local"
    session_secret: SecretStr = SecretStr("")
    session_max_age_seconds: Annotated[int, Field(ge=60, le=86400)] = 28800
    session_cookie_secure: bool = False
    # bcrypt 비용 계수. 하드웨어가 빨라지면 올려야 하는 값이므로 설정으로 둔다 (Harness §5.2).
    # 낮추면 로그인이 빨라지는 대신 오프라인 크래킹 저항이 약해진다 — prod 하한을 강제한다.
    bcrypt_rounds: Annotated[int, Field(ge=4, le=16)] = 12
    login_max_failed_attempts: Annotated[int, Field(ge=1, le=100)] = 5
    login_lock_seconds: Annotated[int, Field(ge=0)] = 900

    bootstrap_admin_username: str = ""
    bootstrap_admin_password: SecretStr = SecretStr("")

    # --- 저장소 -------------------------------------------------------------
    database_url: str = "postgresql+psycopg://stt_app:stt_app@127.0.0.1:5432/stt"
    # 마이그레이션 전용 접속. 런타임 계정은 DDL 권한을 갖지 않아야 하므로(SEC-026)
    # 스키마 소유자 계정을 따로 쓴다. 비워 두면 `database_url` 을 그대로 사용한다.
    migration_database_url: str = ""
    redis_url: str = "redis://127.0.0.1:6379/0"
    # Harness §2.2 / NFR-004: 큐 백엔드는 교체 가능하다. inline 은 브로커 없이 호출 스레드에서
    # 즉시 실행하므로 테스트·로컬 전용이며, 아래 검증이 운영 환경에서의 선택을 막는다.
    queue_backend: Literal["celery", "inline"] = "celery"
    storage_root: Path = Path("./data")
    temp_dir: Path = Path("./data/tmp")

    # --- STT 엔진 (Harness §5.2) --------------------------------------------
    #   faster-whisper : 로컬 모델로 전사한다. 음성이 사내를 벗어나지 않는다
    #   groq-whisper   : Groq 등 OpenAI 호환 전사 API 를 호출한다.
    #                    **음성 원본이 외부로 나간다** — ALLOW_EXTERNAL_STT 승인 필요
    #   external       : 타사 STT 솔루션. 우리 전문 규격(docs/INTERFACE.md)을 따르는
    #                    엔드포인트를 호출한다. 역시 음성이 밖으로 나간다
    #   mock           : 테스트용. prod 선택 시 기동 거부
    stt_engine: Literal["faster-whisper", "groq-whisper", "external", "mock"] = (
        "faster-whisper"
    )
    stt_model_name: str = "medium"
    stt_model_path: str = ""
    stt_device: Literal["cpu", "cuda", "auto"] = "cpu"
    stt_compute_type: str = "int8"
    stt_language: str = "ko"
    stt_beam_size: Annotated[int, Field(ge=1, le=10)] = 5
    stt_vad_enabled: bool = True
    stt_cpu_threads: Annotated[int, Field(ge=0, le=128)] = 4
    stt_allow_model_download: bool = False
    # 정규화 단계에서 이 신뢰도 미만인 세그먼트를 버린다. 0 이면 끄기(기본).
    #
    # 무음 구간 환각("감사합니다", "자막제공자" 등)을 겨냥한다 — 실측에서 정상 발화는
    # 0.82, 환각은 0.20 이었다. **발화를 지우는 동작이므로 기본은 꺼짐이고**, 임계값을
    # 높게 잡으면 진짜 발화까지 사라진다. 0.3~0.4 를 권한다.
    #
    # RAW Transcript 는 그대로 남으므로 지워진 내용은 언제든 확인할 수 있다 (§50).
    stt_min_segment_confidence: Annotated[float, Field(ge=0.0, le=1.0)] = 0.0

    # --- 외부 전사 API (STT_ENGINE=groq-whisper) -----------------------------
    #
    # 로컬 엔진과 결정적으로 다른 점은 **음성 원본 자체가 사내 경계를 벗어난다**는 것이다.
    # 분석(LLM)은 전사된 텍스트만 내보내지만 이쪽은 녹취 파일을 그대로 올린다. 그래서
    # `ALLOW_EXTERNAL_LLM` 과 별개의 승인 플래그를 둔다 — 두 결정의 위험 크기가 다르다.
    #
    # 주소는 OpenAI 호환 전사 엔드포인트를 가리킨다. `/audio/transcriptions` 를 붙여 쓴다.
    #   Groq   : https://api.groq.com/openai/v1  (모델 whisper-large-v3 등)
    #   OpenAI : https://api.openai.com/v1       (모델 whisper-1 등)
    stt_api_base_url: str = "https://api.groq.com/openai/v1"
    # Bearer 토큰으로만 전달된다. 로그·응답·오류 메시지 어디에도 나가지 않는다 (§9 / §15).
    stt_api_key: SecretStr = SecretStr("")
    stt_api_timeout_seconds: Annotated[int, Field(ge=10, le=3600)] = 600
    # 엔드포인트가 받아 주는 파일 크기 상한(MB). 넘는 파일은 호출하지 않고 즉시 거절한다.
    # Groq 무료 등급 25MB / 유료 100MB, OpenAI 25MB (2026-09 기준).
    stt_api_max_upload_mb: Annotated[int, Field(ge=1, le=5000)] = 25
    # 음성 원본 외부 전송 승인. 켜기 전에 개인정보 영향평가와 위탁 계약이 선행되어야 한다.
    allow_external_stt: bool = False

    # --- 자원 보호 (Harness §24) --------------------------------------------
    stt_max_upload_mb: Annotated[int, Field(ge=1, le=5000)] = 500
    stt_max_audio_minutes: Annotated[int, Field(ge=1, le=1440)] = 180
    stt_max_concurrent_jobs: Annotated[int, Field(ge=1, le=64)] = 2
    stt_queue_max_length: Annotated[int, Field(ge=1, le=100000)] = 100
    stt_job_timeout_seconds: Annotated[int, Field(ge=10, le=86400)] = 3600
    stt_max_retries: Annotated[int, Field(ge=0, le=10)] = 2
    api_rate_limit_per_minute: Annotated[int, Field(ge=1, le=100000)] = 60
    upload_rate_limit_per_minute: Annotated[int, Field(ge=1, le=100000)] = 10
    allowed_audio_extensions: str = "wav,mp3,m4a,flac,ogg,webm,mp4"

    # --- 보관 정책 (Harness §21) --------------------------------------------
    audio_retention_days: Annotated[int, Field(ge=1, le=36500)] = 30
    transcript_retention_days: Annotated[int, Field(ge=1, le=36500)] = 365
    audit_retention_days: Annotated[int, Field(ge=1, le=36500)] = 1825
    temp_file_max_age_hours: Annotated[int, Field(ge=1, le=8760)] = 24

    # --- Feature Flag (Harness §54) -----------------------------------------
    enable_transcript_download: bool = True
    enable_audio_download: bool = False
    enable_ffmpeg_preprocess: bool = False
    # --- Transcript 후처리 (Harness §50 / §51) ---
    #
    # 원본을 **덮어쓰지 않는다.** RAW 와 NORMALIZED 는 그대로 두고 `LLM_CORRECTED` 라는
    # 별도 종류로 한 벌 더 만든다. 그래서 보정이 잘못돼도 원본 전사가 온전하고, 두 결과를
    # 나란히 비교할 수 있다 (§50 — 파생 결과는 원본을 대체하지 않는다).
    #
    #   enable_llm_correction : 문맥과 용어사전에 맞춰 문장을 다듬는다
    #   enable_diarization    : 발화자를 상담원/고객으로 나눈다
    #
    # 둘은 **한 번의 LLM 호출**에서 함께 처리된다. 같은 문장을 두 번 읽히면 비용이 두 배가
    # 되고, 보정된 문장과 화자 판단이 서로 다른 입력을 보게 된다.
    #
    # **화자분리는 음향 기반이 아니다.** 문맥으로 추정하므로 정확한 화자 식별이 아니며,
    # 그 한계를 화면에도 표기한다 (§4.3).
    enable_llm_correction: bool = False
    enable_diarization: bool = False

    # --- LLM 후처리 분석 (Harness §2.1 / §8.2 / §13 / §54) ---
    #
    # 후처리(`enable_llm_correction`)와는 다른 기능이다. 후처리는 문장을 다듬은 사본을
    # 만들고, 분석은 요약·분류·키워드만 만든다. 둘 다 원본은 건드리지 않는다.
    enable_llm_analysis: bool = False

    # --- 상담 품질 평가 (QA) ---
    #
    # 분석과 같은 Provider 를 쓰지만 호출은 별개다. 켜면 상담당 LLM 호출이 2회로 늘어난다.
    # `qa_auto_run` 이 꺼져 있으면 화면의 버튼으로만 평가한다 — 전체 상담을 다 평가할
    # 필요는 없고, 표본만 보는 운영도 흔하다.
    enable_qa: bool = False
    qa_auto_run: bool = False

    # --- 실시간 STT (Harness §8.1 / §24 / §54) ---
    #
    # 마이크 입력을 WebSocket 으로 받아 조각 단위로 전사한다. **진짜 스트리밍 ASR 이
    # 아니다** — 일정 길이로 잘라 배치 엔진에 넣는 준실시간 방식이며, 그래서 조각 경계에서
    # 문맥이 끊긴다. 이 한계를 감춘 채 "실시간"이라고만 부르면 결과 품질을 오해하게 된다.
    #
    # 켜면 브라우저 마이크 권한(Permissions-Policy)이 이 출처에 열린다. 그래서 기본은
    # 꺼짐이고, 켜는 것이 명시적 결정이 되게 한다.
    enable_realtime_stt: bool = False
    # 조각 길이. 짧을수록 화면에 빨리 뜨지만 문맥이 더 자주 끊기고 호출 횟수가 늘어난다.
    # 외부 API 를 쓰면 이 값이 곧 호출 빈도이자 비용이다.
    realtime_segment_seconds: Annotated[int, Field(ge=2, le=60)] = 6
    # 한 세션의 최대 길이. 잊고 켜 둔 탭이 자원을 계속 먹는 것을 막는다 (Harness §24).
    realtime_max_session_seconds: Annotated[int, Field(ge=30, le=14400)] = 1800
    # 동시 세션 수. 세션마다 전사가 돌므로 워커 자원과 직결된다.
    realtime_max_sessions: Annotated[int, Field(ge=1, le=64)] = 4

    # --- JSON 연동 (외부 시스템 통합용) ---
    #
    # multipart 를 쓰기 어려운 클라이언트(레거시 ESB, 일부 RPA 등)를 위한 경로다.
    # base64 는 원본보다 약 33% 커지므로 multipart 보다 낮은 상한을 따로 둔다.
    # 검증(확장자·시그니처·크기·재생시간)은 multipart 경로와 **같은 코드**를 지난다 —
    # 입구가 둘이어도 규칙이 갈라지면 약한 쪽이 우회로가 된다 (Harness §6).
    enable_json_upload: bool = False
    json_upload_max_mb: Annotated[int, Field(ge=1, le=200)] = 50
    # 업로드를 제외한 일반 JSON 요청 본문 상한. 메모리 고갈을 막는다 (Harness §24).
    json_body_max_kb: Annotated[int, Field(ge=16, le=8192)] = 512

    enable_json_export: bool = True
    # 내보내기 기본값. 요청 파라미터로 덮어쓸 수 있다.
    json_export_include_transcript: bool = True
    json_export_include_segments: bool = True
    json_export_include_analysis: bool = True

    # 접속 정보는 전부 설정에서 온다. 새 LLM 을 붙이는 데 코드 변경이 필요 없다 (§5.2).
    #   ollama            : Ollama 고유 API (/api/chat)
    #   openai-compatible : OpenAI 형식 /chat/completions 를 말하는 모든 엔드포인트
    #                       (OpenAI, Groq, vLLM, LM Studio, Ollama 의 /v1 등)
    #   openrouter        : OpenRouter 경유. GLM 등 추론 모델의 응답 특성을 함께 다룬다
    #   mock              : 테스트용. prod 선택 시 기동 거부
    llm_provider: Literal["ollama", "openai-compatible", "openrouter", "mock"] = "ollama"
    llm_base_url: str = "http://127.0.0.1:11434"
    llm_model_name: str = "gemma3:latest"
    # Bearer 토큰으로 전달된다. 로그·응답·오류 메시지 어디에도 나가지 않는다 (§9 / §15).
    llm_api_key: SecretStr = SecretStr("")
    # 응답 형식 강제 방식. 엔드포인트가 무엇을 지원하는지에 따라 고른다.
    #   json_schema : 스키마를 그대로 강제한다. 지원하면 이쪽이 낫다
    #   json_object : JSON 이라는 것만 강제한다. 구형 엔드포인트 대비
    llm_json_mode: Literal["json_schema", "json_object"] = "json_schema"

    # --- 추론(reasoning) 모델 대응 (LLM_PROVIDER=openrouter) ---
    #
    # OpenRouter 는 OpenAI 호환이지만, GLM 같은 추론 모델을 라우팅하면 두 가지가 달라진다.
    #   * 추론 토큰이 max_tokens 를 함께 소비한다. 낮게 잡으면 본문이 비거나 잘린다.
    #   * 응답에 코드펜스나 앞뒤 설명이 섞여 나오는 모델이 있다 (Provider 가 걷어낸다).
    llm_max_output_tokens: Annotated[int, Field(ge=256, le=200000)] = 32768
    # 빈 값이면 모델 기본값을 쓴다. 낮출수록 빠르고 싸지만 분석 품질이 떨어진다.
    llm_reasoning_effort: Literal["", "low", "medium", "high"] = ""
    # OpenRouter 순위표 노출용 선택 헤더. 값이 없으면 헤더 자체를 보내지 않는다.
    llm_app_name: str = ""
    llm_site_url: str = ""
    llm_timeout_seconds: Annotated[int, Field(ge=10, le=3600)] = 300
    # 프롬프트에 실어 보낼 Transcript 길이 상한. 넘으면 잘라 보내고 그 사실을 기록한다.
    llm_max_transcript_chars: Annotated[int, Field(ge=1000, le=200000)] = 20000
    # 외부 LLM 전송 승인. 녹취 본문이 사내 경계를 벗어나므로, 켜기 전에 개인정보
    # 영향평가와 위탁 계약이 선행되어야 한다 (SEC-021 / §8.2).
    allow_external_llm: bool = False

    # --- 녹취서버 수집 (수신 전문, docs/INTERFACE.md) ------------------------
    #
    #   off    : 수집하지 않는다 (기본)
    #   folder : 공유 폴더에 떨어진 파일을 집어 온다. 폐쇄망에서 가장 흔하다
    #   http   : 녹취서버 REST API 를 조회해 내려받는다
    #
    # 수집된 건은 업로드와 **같은 검증**을 지난다 — 확장자·시그니처·크기·재생시간.
    # 입구가 늘어도 규칙이 갈라지면 약한 쪽이 우회로가 된다 (Harness §6).
    recording_source: Literal["off", "folder", "http"] = "off"
    # 수집한 Job 의 소유자가 될 계정. 없는 계정이면 수집 시점에 실패한다.
    recording_ingest_username: str = ""
    # 한 번의 수집에서 처리할 최대 건수. 한 번에 수천 건이 들어와 큐를 막는 것을 막는다.
    recording_batch_limit: Annotated[int, Field(ge=1, le=500)] = 20

    # folder 방식
    recording_inbox_dir: Path = Path("./data/recordings/inbox")
    # 처리한 파일을 옮길 곳. 지우지 않고 옮기는 이유는, 수집이 잘못되었을 때 원본이
    # 남아 있어야 다시 넣을 수 있기 때문이다.
    recording_processed_dir: Path = Path("./data/recordings/processed")
    recording_failed_dir: Path = Path("./data/recordings/failed")

    # http 방식
    recording_api_base_url: str = ""
    recording_api_key: SecretStr = SecretStr("")
    recording_api_timeout_seconds: Annotated[int, Field(ge=5, le=600)] = 60
    # 이미 운영 중인 녹취서버를 우리 규격에 맞춰 고치라고 할 수 없는 경우가 많다.
    # 필드 이름만 여기서 맞춘다 (docs/INTERFACE.md 의 수신 전문 참고).
    recording_field_id: str = "id"
    recording_field_filename: str = "filename"
    recording_field_download_url: str = "download_url"
    recording_field_recorded_at: str = "recorded_at"
    recording_list_items_key: str = "items"

    # --- 결과 송신 (송신 전문, docs/INTERFACE.md) ----------------------------
    #
    # 전사·분석·QA 결과를 외부 솔루션(CRM 등)으로 보낸다. **녹취 본문이 사내 경계를
    # 벗어날 수 있으므로** 주소가 외부면 ALLOW_EXTERNAL_OUTBOUND 승인이 필요하다.
    outbound_enabled: bool = False
    outbound_url: str = ""
    outbound_api_key: SecretStr = SecretStr("")
    outbound_timeout_seconds: Annotated[int, Field(ge=5, le=600)] = 30
    outbound_max_attempts: Annotated[int, Field(ge=1, le=10)] = 3
    # 무엇을 실어 보낼지. 필요 없는 것을 빼면 전송량과 노출 범위가 함께 줄어든다.
    outbound_include_transcript: bool = True
    outbound_include_analysis: bool = True
    outbound_include_qa: bool = True
    allow_external_outbound: bool = False

    ffmpeg_binary: str = "/usr/bin/ffmpeg"
    ffmpeg_timeout_seconds: Annotated[int, Field(ge=1, le=7200)] = 600

    # --- 파생 값 ------------------------------------------------------------

    @property
    def migration_url(self) -> str:
        """마이그레이션에 쓸 접속 문자열. 지정되지 않으면 런타임 접속을 쓴다."""
        return self.migration_database_url or self.database_url

    @property
    def is_production(self) -> bool:
        return self.app_env is Environment.PROD

    @property
    def allowed_extensions(self) -> frozenset[str]:
        """소문자 · 점 없는 확장자 allowlist (Harness §6)."""
        return frozenset(
            ext.strip().lower().lstrip(".")
            for ext in self.allowed_audio_extensions.split(",")
            if ext.strip()
        )

    @property
    def cors_origins(self) -> list[str]:
        return [o.strip() for o in self.cors_allow_origins.split(",") if o.strip()]

    @property
    def max_upload_bytes(self) -> int:
        return self.stt_max_upload_mb * 1024 * 1024

    @property
    def max_audio_seconds(self) -> float:
        return float(self.stt_max_audio_minutes * 60)

    @property
    def audio_dir(self) -> Path:
        return self.storage_root / "audio"

    @property
    def transcript_dir(self) -> Path:
        return self.storage_root / "transcripts"

    @property
    def stt_model_reference(self) -> str:
        """엔진에 전달할 모델 식별자. 사전 반입 경로가 있으면 그것을 우선한다 (Harness §42)."""
        return self.stt_model_path or self.stt_model_name

    def stt_config_version(self) -> str:
        """STT 설정 조합의 안정적 해시 (Harness §20 / DAT-011).

        모델·언어·beam_size·VAD 등이 하나라도 바뀌면 값이 달라지므로,
        어떤 설정에서 생성된 Transcript 인지 사후 추적할 수 있다.
        """
        payload = {key: getattr(self, key) for key in _STT_CONFIG_VERSION_KEYS}
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]

    # --- 검증 ---------------------------------------------------------------

    @field_validator("storage_root", "temp_dir")
    @classmethod
    def _resolve_paths(cls, value: Path) -> Path:
        # 상대경로를 그대로 두면 실행 위치에 따라 저장 위치가 달라져
        # Path Traversal 검사(Harness §6)의 기준점이 흔들린다. 항상 절대경로로 고정한다.
        return value.expanduser().resolve()

    @model_validator(mode="after")
    def _enforce_harness_rules(self) -> Settings:
        """Harness 위반 설정으로는 기동하지 못하게 막는다."""
        if self.is_production:
            # Harness §35: Production Debug 금지.
            if self.debug:
                raise ConfigurationError(
                    "prod 환경에서 DEBUG=true 는 허용되지 않는다 (Harness §35)"
                )
            if self.log_level == "DEBUG":
                raise ConfigurationError(
                    "prod 환경에서 LOG_LEVEL=DEBUG 는 허용되지 않는다 (Harness §35)"
                )
            # Harness §9: Secret 은 기본값/빈 값으로 운영될 수 없다.
            if len(self.session_secret.get_secret_value()) < 32:
                raise ConfigurationError(
                    "prod 환경에서는 32자 이상의 SESSION_SECRET 이 필요하다 (Harness §9)"
                )
            if not self.session_cookie_secure:
                raise ConfigurationError(
                    "prod 환경에서는 SESSION_COOKIE_SECURE=true 여야 한다 (Harness §11)"
                )
            # Harness §61-5: 부트스트랩 자격증명이 운영 환경에 남아 있으면 안 된다.
            if self.bootstrap_admin_password.get_secret_value():
                raise ConfigurationError(
                    "prod 환경에 BOOTSTRAP_ADMIN_PASSWORD 가 남아 있다 (Harness §9)"
                )
            if "*" in self.cors_origins:
                raise ConfigurationError(
                    "prod 환경에서 CORS 와일드카드는 허용되지 않는다 (Harness §11)"
                )

        if self.is_production and self.bcrypt_rounds < 12:
            raise ConfigurationError(
                "prod 환경에서 BCRYPT_ROUNDS 는 12 이상이어야 한다 (Harness §9)"
            )

        if self.is_production and self.queue_backend != "celery":
            # Inline 큐는 업로드 요청을 전사 완료까지 블로킹한다 (Harness §24).
            raise ConfigurationError(
                "prod 환경에서 QUEUE_BACKEND='inline' 은 허용되지 않는다 (Harness §24)"
            )

        if self.temp_file_max_age_hours <= 0:
            raise ConfigurationError("임시파일 무기한 보관은 허용되지 않는다 (Harness §21)")

        if self.auth_provider != "local":
            # Harness §4.3: 미구현 기능을 조용히 우회하지 않고 명시적으로 실패시킨다.
            raise ConfigurationError(
                f"AUTH_PROVIDER='{self.auth_provider}' 는 본 버전에서 구현되지 않았다. "
                "사내 SSO 연동은 app/auth/providers 의 어댑터 구현 후 활성화한다."
            )

        if (self.enable_llm_correction or self.enable_diarization) and not (
            self.enable_llm_analysis
        ):
            # 후처리는 분석과 같은 LLM Provider 를 쓴다. 분석이 꺼진 채로 후처리만 켜면
            # Provider 설정 검증(주소·키·외부 승인)을 건너뛴 채 외부 호출이 나간다.
            raise ConfigurationError(
                "ENABLE_LLM_CORRECTION / ENABLE_DIARIZATION 은 "
                "ENABLE_LLM_ANALYSIS=true 를 전제로 한다. 같은 LLM 설정을 쓴다"
            )

        if self.stt_engine in ("groq-whisper", "external"):
            # 음성 원본이 사내를 벗어난다. 설정 실수로 그렇게 되는 일은 없어야 한다 (SEC-021).
            if not self.allow_external_stt:
                raise ConfigurationError(
                    f"STT_ENGINE='{self.stt_engine}' 는 음성 원본을 외부로 전송한다. "
                    "ALLOW_EXTERNAL_STT=true 로 명시 승인해야 한다 (Harness §8.2)"
                )
            if not self.stt_api_key.get_secret_value().strip():
                # 키 없이 떠 있다가 첫 전사에서 401 로 드러나는 것보다 낫다 (Harness §4.3).
                raise ConfigurationError(
                    f"STT_ENGINE='{self.stt_engine}' 이면 STT_API_KEY 를 지정해야 한다"
                )
            if not self.stt_api_base_url.strip():
                raise ConfigurationError(
                    f"STT_ENGINE='{self.stt_engine}' 이면 STT_API_BASE_URL 을 지정해야 한다"
                )
            if self.stt_api_base_url.startswith("http://") and not _is_local_url(
                self.stt_api_base_url
            ):
                # 평문 구간을 지나면 녹취 음성이 그대로 노출된다 (Harness §8.2).
                raise ConfigurationError(
                    "외부 STT_API_BASE_URL 은 https 여야 한다. 평문 http 로는 음성을 보내지 않는다"
                )

        if self.enable_realtime_stt and self.stt_engine == "mock" and self.is_production:
            # 실시간 화면에 가짜 전사가 흐르는 것은 조용한 실패 중 최악이다 (§4.3 / §35).
            raise ConfigurationError(
                "prod 환경에서 실시간 STT 와 STT_ENGINE='mock' 을 함께 쓸 수 없다"
            )

        if self.recording_source == "http" and not self.recording_api_base_url.strip():
            raise ConfigurationError(
                "RECORDING_SOURCE='http' 이면 RECORDING_API_BASE_URL 을 지정해야 한다"
            )

        if self.recording_source != "off" and not self.recording_ingest_username.strip():
            # 소유자 없는 Job 은 만들 수 없다. 수집 시점에 알기보다 기동 시점에 막는다.
            raise ConfigurationError(
                "RECORDING_SOURCE 를 쓰려면 RECORDING_INGEST_USERNAME 을 지정해야 한다"
            )

        if self.outbound_enabled:
            if not self.outbound_url.strip():
                raise ConfigurationError(
                    "OUTBOUND_ENABLED=true 이면 OUTBOUND_URL 을 지정해야 한다"
                )
            if not self.allow_external_outbound and not _is_local_url(self.outbound_url):
                # 전사·분석 결과가 사내를 벗어난다. 명시 승인 없이는 막는다 (SEC-021).
                raise ConfigurationError(
                    f"OUTBOUND_URL 이 외부 주소({self.outbound_url})다. 전사 결과를 "
                    "외부로 보내려면 ALLOW_EXTERNAL_OUTBOUND=true 로 승인해야 한다"
                )
            if self.outbound_url.startswith("http://") and not _is_local_url(
                self.outbound_url
            ):
                raise ConfigurationError(
                    "외부 OUTBOUND_URL 은 https 여야 한다. 평문으로는 결과를 보내지 않는다"
                )

        if self.enable_qa and not self.enable_llm_analysis:
            # QA 는 분석과 같은 LLM Provider 를 쓴다. 분석이 꺼진 채로 QA 만 켜면
            # Provider 설정 검증(주소·키·외부 승인)을 건너뛴 채 외부 호출이 나간다.
            raise ConfigurationError(
                "ENABLE_QA=true 는 ENABLE_LLM_ANALYSIS=true 를 전제로 한다. "
                "QA 는 분석과 같은 LLM 설정을 쓴다"
            )

        if self.qa_auto_run and not self.enable_qa:
            raise ConfigurationError(
                "QA_AUTO_RUN=true 이면 ENABLE_QA=true 여야 한다"
            )

        if self.enable_llm_analysis and self.llm_provider == "mock" and self.is_production:
            # 가짜 분석 결과가 운영 화면에 표시되는 것은 조용한 실패 중 최악이다 (§4.3).
            raise ConfigurationError(
                "prod 환경에서 LLM_PROVIDER='mock' 은 허용되지 않는다 (Harness §35)"
            )

        if self.enable_llm_analysis and not self.llm_base_url.strip():
            raise ConfigurationError(
                "ENABLE_LLM_ANALYSIS=true 이면 LLM_BASE_URL 을 지정해야 한다"
            )

        if self.enable_llm_analysis and not self.llm_model_name.strip():
            raise ConfigurationError(
                "ENABLE_LLM_ANALYSIS=true 이면 LLM_MODEL_NAME 을 지정해야 한다"
            )

        if (
            self.enable_llm_analysis
            and self.llm_provider == "openrouter"
            and not self.llm_api_key.get_secret_value().strip()
        ):
            # 키 없이 기동하면 첫 분석에서야 401 로 드러난다. 기동 시점에 막는다 (§4.3).
            raise ConfigurationError(
                "LLM_PROVIDER='openrouter' 이면 LLM_API_KEY 를 지정해야 한다. "
                "키는 OpenRouter 대시보드에서 발급한다"
            )

        if not self.allow_external_llm and self.llm_base_url and not _is_local_url(
            self.llm_base_url
        ):
            # 녹취 본문이 사내 경계를 벗어나는 것은 명시적 승인 없이는 막는다 (SEC-021).
            raise ConfigurationError(
                f"LLM_BASE_URL 이 외부 주소({self.llm_base_url})다. 녹취 본문을 외부로 "
                "보내려면 ALLOW_EXTERNAL_LLM=true 로 명시 승인해야 한다 (Harness §8.2)"
            )

        if self.llm_base_url.startswith("http://") and not _is_local_url(self.llm_base_url):
            # 외부 구간을 평문으로 지나면 녹취 본문이 그대로 노출된다 (Harness §8.2).
            raise ConfigurationError(
                "외부 LLM_BASE_URL 은 https 여야 한다. 평문 http 로는 녹취를 보내지 않는다"
            )

        return self


# 사내망에서 흔히 쓰는 도메인 접미사. 이 이름들은 외부 승인 없이 허용한다.
_INTERNAL_SUFFIXES: tuple[str, ...] = (
    ".internal",
    ".local",
    ".lan",
    ".intranet",
    ".corp",
    ".home",
    ".svc",
    ".cluster.local",
)


def _is_local_url(url: str) -> bool:
    """사내 경계 안의 주소로 볼 수 있는지 판별한다.

    **완벽한 판별은 불가능하다.** DNS 이름 뒤에 무엇이 있는지는 여기서 알 수 없고,
    실제 경계 통제는 네트워크 정책이 한다. 이 함수의 목적은 `api.openai.com` 같은
    명백한 외부 주소를 실수로 설정하는 것을 막는 것이다. 판별이 애매하면 외부로 보고,
    운영자가 `ALLOW_EXTERNAL_LLM` 으로 명시 승인하게 한다 — 안전한 쪽으로 틀린다.
    """
    from contextlib import suppress
    from ipaddress import ip_address
    from urllib.parse import urlparse

    host = (urlparse(url).hostname or "").lower()
    if not host:
        return False

    # IP 주소면 표준 판정을 쓴다. 접두사 비교는 172.16/12 대역을 놓친다.
    # 호스트명이면 ValueError 가 나며, 아래 이름 규칙으로 넘어간다.
    with suppress(ValueError):
        address = ip_address(host)
        return address.is_loopback or address.is_private or address.is_link_local

    if host in ("localhost", "host.docker.internal"):
        return True
    # 점이 없는 이름은 컨테이너·사내 호스트명으로 본다 (예: ollama, llm-server).
    if "." not in host:
        return True
    return host.endswith(_INTERNAL_SUFFIXES)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """프로세스 단위로 캐시된 설정 인스턴스를 반환한다."""
    return Settings()
