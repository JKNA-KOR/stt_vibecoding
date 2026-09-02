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
    stt_engine: Literal["faster-whisper", "mock"] = "faster-whisper"
    stt_model_name: str = "medium"
    stt_model_path: str = ""
    stt_device: Literal["cpu", "cuda", "auto"] = "cpu"
    stt_compute_type: str = "int8"
    stt_language: str = "ko"
    stt_beam_size: Annotated[int, Field(ge=1, le=10)] = 5
    stt_vad_enabled: bool = True
    stt_cpu_threads: Annotated[int, Field(ge=0, le=128)] = 4
    stt_allow_model_download: bool = False

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
    enable_llm_correction: bool = False
    enable_diarization: bool = False

    # --- LLM 후처리 분석 (Harness §2.1 / §8.2 / §13 / §54) ---
    #
    # `enable_llm_correction` 과는 다른 기능이다. 교정은 Transcript 본문을 바꾸지만,
    # 분석은 요약·분류·키워드만 만들고 원본은 건드리지 않는다. 그래서 교정은 여전히
    # 범위 밖이고 분석만 구현되어 있다.
    enable_llm_analysis: bool = False
    llm_provider: Literal["ollama", "mock"] = "ollama"
    # 로컬에서 도는 LLM 만 기본으로 허용한다. 녹취 본문을 외부로 보내는 것은 SEC-021
    # 위반이므로, 외부 Provider 는 아래 승인 플래그 없이는 선택 자체가 거부된다.
    llm_base_url: str = "http://127.0.0.1:11434"
    llm_model_name: str = "gemma3:latest"
    llm_timeout_seconds: Annotated[int, Field(ge=10, le=3600)] = 300
    # 프롬프트에 실어 보낼 Transcript 길이 상한. 넘으면 잘라 보내고 그 사실을 기록한다.
    llm_max_transcript_chars: Annotated[int, Field(ge=1000, le=200000)] = 20000
    # 외부 LLM 전송 승인. 켜려면 개인정보 영향평가와 위탁 계약이 선행되어야 한다 (§8.2).
    allow_external_llm: bool = False

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

        if self.enable_llm_correction or self.enable_diarization:
            # Harness §2.1: Transcript 본문을 고쳐 쓰는 교정과 화자분리는 아직 없다.
            # 원본을 바꾸지 않는 분석(`enable_llm_analysis`)은 구현되어 있으며 별개다.
            raise ConfigurationError(
                "LLM 교정 / 화자분리는 본 버전 범위 밖이다. 해당 Feature Flag 를 끄고 기동한다. "
                "요약·분류가 필요하면 ENABLE_LLM_ANALYSIS 를 쓴다."
            )

        if self.enable_llm_analysis and self.llm_provider == "mock" and self.is_production:
            # 가짜 분석 결과가 운영 화면에 표시되는 것은 조용한 실패 중 최악이다 (§4.3).
            raise ConfigurationError(
                "prod 환경에서 LLM_PROVIDER='mock' 은 허용되지 않는다 (Harness §35)"
            )

        if not self.allow_external_llm and self.llm_base_url and not _is_local_url(
            self.llm_base_url
        ):
            # 녹취 본문이 사내 경계를 벗어나는 것은 명시적 승인 없이는 막는다 (SEC-021).
            raise ConfigurationError(
                f"LLM_BASE_URL 이 외부 주소({self.llm_base_url})다. 녹취 본문을 외부로 "
                "보내려면 ALLOW_EXTERNAL_LLM=true 로 명시 승인해야 한다 (Harness §8.2)"
            )

        return self


def _is_local_url(url: str) -> bool:
    """루프백 또는 사설망 호스트인지 판별한다.

    완벽한 경계 판별은 아니다 — DNS 이름 뒤에 외부 주소가 있을 수 있다. 목적은
    `api.openai.com` 같은 명백한 외부 주소를 실수로 설정하는 것을 막는 것이고,
    실제 경계 통제는 네트워크 정책이 한다.
    """
    from urllib.parse import urlparse

    host = (urlparse(url).hostname or "").lower()
    if host in ("localhost", "127.0.0.1", "::1", "host.docker.internal"):
        return True
    # 컨테이너 네트워크의 서비스 이름처럼 점이 없는 이름은 내부로 본다.
    if "." not in host:
        return True
    return host.startswith(("10.", "192.168.", "172.16.", "172.17.", "172.18."))


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """프로세스 단위로 캐시된 설정 인스턴스를 반환한다."""
    return Settings()
