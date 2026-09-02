"""런타임 설정 관리 (FR-M-004, Harness §37).

aicc_code 가 프롬프트와 모델 선택을 DB 에 두어 재배포 없이 바꿀 수 있게 한 방식을
가져왔다. 두 가지를 다르게 했다.

  1. **기본값은 코드에 있다.** DB 는 덮어쓰기만 한다. DB 가 비어 있어도 서비스가 뜨고,
     기본값이 코드 리뷰와 버전 관리를 받는다. aicc_code 는 DB 에 설정이 없으면
     기능 자체가 실패한다.
  2. **변경은 전부 감사된다.** 누가 언제 무엇을 왜 바꿨는지 `config_change` 에 남는다.
     Secret 성 키는 값 대신 마스킹 표식을 남긴다.

캐시를 두지 않는 것은 aicc_code 와 같다. 설정 변경이 즉시 반영되어야 하고, 그러려면
읽을 때마다 DB 를 보는 편이 단순하다. 호출 빈도가 낮아 비용도 문제되지 않는다.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.events import AuditEventType
from app.audit.service import Actor, AuditService
from app.core.config import AUDITED_CONFIG_KEYS, Settings
from app.core.exceptions import ValidationError
from app.core.logging import get_logger
from app.llm.prompts import ANALYSIS_PROMPT_KEY, DEFAULT_ANALYSIS_PROMPT
from app.storage.models import ConfigChange, RuntimeConfig

logger = get_logger(__name__)

# 값이 길어 별도로 다루는 키. 감사에는 길이와 해시만 남기고 본문은 남기지 않는다.
_PROMPT_KEYS: frozenset[str] = frozenset({ANALYSIS_PROMPT_KEY})

# 런타임에 바꿀 수 있는 키 전체. 여기 없는 키는 API 가 거부한다.
EDITABLE_KEYS: frozenset[str] = AUDITED_CONFIG_KEYS | _PROMPT_KEYS

# 코드에 두는 기본값. DB 에 값이 없으면 이것이 쓰인다.
_DEFAULTS: dict[str, str] = {ANALYSIS_PROMPT_KEY: DEFAULT_ANALYSIS_PROMPT}

_MAX_VALUE_CHARS = 20000


@dataclass(frozen=True, slots=True)
class ConfigEntry:
    key: str
    value: str
    is_overridden: bool
    updated_by: str
    # 프롬프트처럼 긴 값은 목록 조회에서 본문 대신 길이만 준다.
    length: int


class RuntimeConfigService:
    """DB 에 저장된 설정을 읽고 쓴다."""

    def __init__(self, session: Session, *, settings: Settings) -> None:
        self._session = session
        self._settings = settings
        self._audit = AuditService(session, application_version=settings.app_version)

    def get(self, key: str) -> str:
        """현재 유효한 값. DB 값이 없으면 코드 기본값, 그것도 없으면 환경설정 값."""
        row = self._session.get(RuntimeConfig, key)
        if row is not None:
            return row.config_value
        if key in _DEFAULTS:
            return _DEFAULTS[key]
        # AUDITED_CONFIG_KEYS 는 Settings 의 필드명과 같다.
        return str(getattr(self._settings, key, ""))

    def list_all(self) -> list[ConfigEntry]:
        overrides = {
            row.config_key: row
            for row in self._session.execute(select(RuntimeConfig)).scalars().all()
        }
        entries: list[ConfigEntry] = []
        for key in sorted(EDITABLE_KEYS):
            row = overrides.get(key)
            value = self.get(key)
            entries.append(
                ConfigEntry(
                    key=key,
                    # 긴 값은 목록에서 잘라 보낸다. 전체는 단건 조회로 받는다.
                    value=value if key not in _PROMPT_KEYS else value[:200],
                    is_overridden=row is not None,
                    updated_by=row.updated_by if row else "",
                    length=len(value),
                )
            )
        return entries

    def set(self, key: str, value: str, *, actor: Actor, reason: str) -> None:
        """값을 바꾸고 변경 이력을 남긴다 (Harness §37).

        Args:
            reason: 변경 사유. 비워 둘 수 없다 — 사유 없는 운영 설정 변경은
                나중에 추적이 불가능해진다.

        Raises:
            ValidationError: 허용되지 않은 키, 빈 사유, 길이 초과.
        """
        if key not in EDITABLE_KEYS:
            raise ValidationError(
                "변경할 수 없는 설정 키입니다.",
                internal_detail=f"key '{key}' is not runtime-editable",
            )
        if not reason.strip():
            raise ValidationError("변경 사유를 입력해 주세요.")
        if len(value) > _MAX_VALUE_CHARS:
            raise ValidationError("설정 값이 너무 깁니다.")

        previous = self.get(key)
        row = self._session.get(RuntimeConfig, key)
        if row is None:
            row = RuntimeConfig(config_key=key, config_value=value, updated_by=actor.id)
            self._session.add(row)
        else:
            row.config_value = value
            row.updated_by = actor.id

        self._session.add(
            ConfigChange(
                changed_by=actor.id,
                config_key=key,
                previous_value=_for_history(key, previous),
                new_value=_for_history(key, value),
                reason=reason.strip(),
                request_id=_request_id(),
            )
        )
        self._audit.record(
            _audit_event_for(key),
            actor=actor,
            action="update_runtime_config",
            target_type="config",
            target_id=key,
            # 값 자체는 감사 메타데이터에 넣지 않는다. 이력 테이블에 남는다 (Harness §64).
            metadata={"reason_length": len(reason), "value_length": len(value)},
        )
        self._session.flush()

        logger.info(
            "runtime config changed",
            extra={"event": "CONFIG_CHANGED", "config_key": key},
        )

    def reset(self, key: str, *, actor: Actor, reason: str) -> None:
        """덮어쓴 값을 지워 기본값으로 되돌린다."""
        row = self._session.get(RuntimeConfig, key)
        if row is None:
            return
        previous = row.config_value
        self._session.delete(row)
        self._session.add(
            ConfigChange(
                changed_by=actor.id,
                config_key=key,
                previous_value=_for_history(key, previous),
                new_value="(기본값으로 복원)",
                reason=reason.strip() or "기본값 복원",
                request_id=_request_id(),
            )
        )
        self._audit.record(
            _audit_event_for(key),
            actor=actor,
            action="reset_runtime_config",
            target_type="config",
            target_id=key,
        )
        self._session.flush()


def _for_history(key: str, value: str) -> str:
    """이력에 남길 표현.

    프롬프트는 수천 자에 달하므로 전문을 남기면 이력 테이블이 감당하지 못한다.
    본문 대신 길이와 해시를 남겨 "같은 값인지"를 판별할 수 있게 한다.
    """
    if key in _PROMPT_KEYS:
        import hashlib

        digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]
        return f"(길이 {len(value)}자, sha256:{digest})"
    return value


def _audit_event_for(key: str) -> AuditEventType:
    """모델 관련 변경은 별도 이벤트로 남긴다 (Harness §17)."""
    if key in ("llm_model_name", "stt_model_name"):
        return AuditEventType.MODEL_CONFIG_CHANGED
    if key.endswith("_retention_days"):
        return AuditEventType.RETENTION_POLICY_CHANGED
    return AuditEventType.SYSTEM_CONFIG_CHANGED


def _request_id() -> str:
    from app.core.context import get_request_id

    return get_request_id() or ""
