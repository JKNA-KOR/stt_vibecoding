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

    # 접속 정보는 전부 설정에서 온다. 새 LLM 을 붙이는 데 코드 변경이 필요 없다 (§5.2).
    #   ollama            : Ollama 고유 API (/api/chat)
    #   openai-compatible : OpenAI 형식 /chat/completions 를 말하는 모든 엔드포인트
    #                       (OpenAI, Groq, vLLM, LM Studio, Ollama 의 /v1 등)
    #   mock              : 테스트용. prod 선택 시 기동 거부
    llm_provider: Literal["ollama", "openai-compatible", "mock"] = "ollama"
    llm_base_url: str = "http://127.0.0.1:11434"
    llm_model_name: str = "gemma3:latest"
    # Bearer 토큰으로 전달된다. 로그·응답·오류 메시지 어디에도 나가지 않는다 (§9 / §15).
    llm_api_key: SecretStr = SecretStr("")
    # 응답 형식 강제 방식. 엔드포인트가 무엇을 지원하는지에 따라 고른다.
    #   json_schema : 스키마를 그대로 강제한다. 지원하면 이쪽이 낫다
    #   json_object : JSON 이라는 것만 강제한다. 구형 엔드포인트 대비
    llm_json_mode: Literal["json_schema", "json_object"] = "json_schema"
    llm_timeout_seconds: Annotated[int, Field(ge=10, le=3600)] = 300
    # 프롬프트에 실어 보낼 Transcript 길이 상한. 넘으면 잘라 보내고 그 사실을 기록한다.
    llm_max_transcript_chars: Annotated[int, Field(ge=1000, le=200000)] = 20000
    # 외부 LLM 전송 승인. 녹취 본문이 사내 경계를 벗어나므로, 켜기 전에 개인정보
    # 영향평가와 위탁 계약이 선행되어야 한다 (SEC-021 / §8.2).
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

        if self.enable_llm_analysis and not self.llm_base_url.strip():
            raise ConfigurationError(
                "ENABLE_LLM_ANALYSIS=true 이면 LLM_BASE_URL 을 지정해야 한다"
            )

        if self.enable_llm_analysis and not self.llm_model_name.strip():
            raise ConfigurationError(
                "ENABLE_LLM_ANALYSIS=true 이면 LLM_MODEL_NAME 을 지정해야 한다"
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
