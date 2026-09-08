"""용어사전 서비스 (FR-M-004, Harness §17 / §37).

용어를 바꾸면 **전사 결과와 분석 결과가 함께 달라진다.** 그래서 프롬프트·QA 기준과 같은
급으로 다룬다 — 변경은 관리자만 할 수 있고 전부 감사에 남는다.

읽기는 누구나 할 수 있다. 용어의 뜻은 상담원이 통화 중에 확인해야 하는 정보이고,
민감정보가 아니다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.audit.events import AuditEventType
from app.audit.service import AuditService
from app.auth.principal import Principal
from app.auth.roles import Permission
from app.core.config import Settings
from app.core.exceptions import ConflictError, NotFoundError, ValidationError
from app.core.logging import get_logger
from app.glossary.seed import seed_payloads
from app.storage.models import GlossaryTerm

logger = get_logger(__name__)

_MAX_TERM_CHARS = 120
_MAX_DEFINITION_CHARS = 500
_MAX_ALIASES = 10
_MAX_CATEGORY_CHARS = 40

# 전사 힌트에 실을 최대 크기. **글자 수가 아니라 UTF-8 바이트다.**
#
# Whisper 의 `initial_prompt` 는 약 224 토큰까지만 반영되고, 엔드포인트는 그 한도를
# 바이트로 강제한다 — Groq 는 896바이트를 넘으면 `invalid_prompt` 로 400 을 돌려준다.
#
# **한글은 UTF-8 로 한 글자가 3바이트다.** 글자 수로 세면 393자짜리 힌트가 935바이트가
# 되어 상한을 넘는다. 실제로 그렇게 전사가 통째로 실패한 적이 있어 바이트로 바꿨다.
# 상한 아래로 여유를 두는 것은 엔드포인트마다 계산이 조금씩 다르기 때문이다.
HINT_MAX_BYTES = 800


@dataclass(frozen=True, slots=True)
class TermView:
    id: str
    term: str
    aliases: list[str]
    category: str
    definition: str
    is_active: bool
    priority: int
    updated_at: datetime
    updated_by: str


class GlossaryService:
    def __init__(self, session: Session, *, settings: Settings) -> None:
        self._session = session
        self._settings = settings
        self._audit = AuditService(session, application_version=settings.app_version)

    # --- 조회 ---------------------------------------------------------------

    def list_terms(
        self,
        *,
        search: str = "",
        category: str = "",
        include_inactive: bool = True,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[list[TermView], int]:
        stmt = select(GlossaryTerm)
        count_stmt = select(func.count()).select_from(GlossaryTerm)

        if not include_inactive:
            stmt = stmt.where(GlossaryTerm.is_active.is_(True))
            count_stmt = count_stmt.where(GlossaryTerm.is_active.is_(True))
        if category:
            stmt = stmt.where(GlossaryTerm.category == category)
            count_stmt = count_stmt.where(GlossaryTerm.category == category)
        if search:
            # ORM 의 파라미터 바인딩을 쓴다. 문자열을 이어 붙이지 않는다 (Harness §12).
            pattern = f"%{search.strip()}%"
            stmt = stmt.where(GlossaryTerm.term.ilike(pattern))
            count_stmt = count_stmt.where(GlossaryTerm.term.ilike(pattern))

        total = int(self._session.execute(count_stmt).scalar_one())
        rows = (
            self._session.execute(
                stmt.order_by(GlossaryTerm.category, GlossaryTerm.term)
                .limit(min(max(limit, 1), 500))
                .offset(max(offset, 0))
            )
            .scalars()
            .all()
        )
        return [_to_view(row) for row in rows], total

    def categories(self) -> list[str]:
        rows = self._session.execute(
            select(GlossaryTerm.category).distinct().order_by(GlossaryTerm.category)
        ).scalars().all()
        return [row for row in rows if row]

    # --- 파이프라인이 쓰는 값 -------------------------------------------------

    def transcription_hint(self) -> str:
        """전사 엔진에 넘길 어휘 힌트.

        용어를 쉼표로 이어 붙인 한 줄이다. 상한(UTF-8 바이트)을 넘으면 `priority` 가
        높은 것부터 채우고 나머지는 버린다. 넘겨서 엔드포인트가 거절하면 **전사 자체가
        실패한다** — 잘라서 보내는 편이 낫다 (Harness §4.3).

        별칭은 넣지 않는다. 힌트는 "이런 말이 나올 것"을 알리는 자리이지 오인식 표기를
        학습시키는 자리가 아니며, 잘못된 표기를 넣으면 그쪽으로 끌려간다.
        """
        rows = (
            self._session.execute(
                select(GlossaryTerm.term)
                .where(GlossaryTerm.is_active.is_(True))
                .order_by(GlossaryTerm.priority.desc(), GlossaryTerm.term)
            )
            .scalars()
            .all()
        )

        parts: list[str] = []
        size = 0
        for term in rows:
            # 구분자 ", " 2바이트를 함께 센다. 한글은 글자당 3바이트라 글자 수로 재면
            # 실제 크기의 3분의 1로 착각하게 된다.
            addition = len(term.encode("utf-8")) + 2
            if size + addition > HINT_MAX_BYTES:
                break
            parts.append(term)
            size += addition

        if len(parts) < len(rows):
            logger.info(
                "glossary hint truncated to fit the prompt limit",
                extra={
                    "event": "GLOSSARY_HINT_TRUNCATED",
                    "included": len(parts),
                    "total": len(rows),
                },
            )
        return ", ".join(parts)

    def prompt_appendix(self, *, limit: int = 40) -> str:
        """분석·QA 프롬프트에 붙일 용어 설명.

        전사 힌트와 달리 뜻까지 넣는다. LLM 은 문맥 길이가 넉넉하고, 용어를 오해해서
        요약이 어긋나는 편이 프롬프트가 조금 길어지는 것보다 나쁘다.
        """
        rows = (
            self._session.execute(
                select(GlossaryTerm)
                .where(GlossaryTerm.is_active.is_(True))
                .order_by(GlossaryTerm.priority.desc(), GlossaryTerm.term)
                .limit(limit)
            )
            .scalars()
            .all()
        )
        if not rows:
            return ""

        lines: list[str] = []
        for row in rows:
            if not row.definition:
                continue
            line = f"- {row.term}: {row.definition}"
            # 오인식 표기를 함께 알려 준다. 전사가 "중도 상환 수수료"로 적어도 모델이
            # 같은 용어로 읽게 하려는 것이다. 전사 결과를 고쳐 쓰지는 않는다 —
            # 정규화기는 어휘를 바꾸지 않는다는 계약을 지킨다 (app/stt/normalization.py).
            aliases = [alias for alias in (row.aliases or []) if isinstance(alias, str)]
            if aliases:
                line += f" (녹취에 '{"', '".join(aliases[:3])}' 로 적혔을 수 있다)"
            lines.append(line)
        return "\n".join(lines)

    # --- 변경 (관리자) --------------------------------------------------------

    def create(self, principal: Principal, payload: dict[str, object]) -> TermView:
        principal.require(Permission.ADMIN_MANAGE)
        term = _clean_term(payload.get("term"))

        if self._find_by_term(term) is not None:
            raise ConflictError(
                "이미 등록된 용어입니다.", internal_detail=f"term '{term}' exists"
            )

        row = GlossaryTerm(
            term=term,
            aliases=_clean_aliases(payload.get("aliases")),
            category=_clean_text(payload.get("category"), _MAX_CATEGORY_CHARS),
            definition=_clean_text(payload.get("definition"), _MAX_DEFINITION_CHARS),
            is_active=bool(payload.get("is_active", True)),
            priority=_clean_priority(payload.get("priority")),
            updated_by=principal.id,
        )
        self._session.add(row)
        self._session.flush()
        self._record(principal, "create_glossary_term", row)
        return _to_view(row)

    def update(
        self, principal: Principal, term_id: str, payload: dict[str, object]
    ) -> TermView:
        principal.require(Permission.ADMIN_MANAGE)
        row = self._session.get(GlossaryTerm, term_id)
        if row is None:
            raise NotFoundError(internal_detail=f"glossary term {term_id} not found")

        if "term" in payload:
            new_term = _clean_term(payload.get("term"))
            existing = self._find_by_term(new_term)
            if existing is not None and existing.id != row.id:
                raise ConflictError(
                    "이미 등록된 용어입니다.",
                    internal_detail=f"term '{new_term}' exists",
                )
            row.term = new_term
        if "aliases" in payload:
            row.aliases = _clean_aliases(payload.get("aliases"))
        if "category" in payload:
            row.category = _clean_text(payload.get("category"), _MAX_CATEGORY_CHARS)
        if "definition" in payload:
            row.definition = _clean_text(payload.get("definition"), _MAX_DEFINITION_CHARS)
        if "is_active" in payload:
            row.is_active = bool(payload.get("is_active"))
        if "priority" in payload:
            row.priority = _clean_priority(payload.get("priority"))

        row.updated_by = principal.id
        row.updated_at = datetime.now(UTC)
        self._session.flush()
        self._record(principal, "update_glossary_term", row)
        return _to_view(row)

    def delete(self, principal: Principal, term_id: str) -> None:
        principal.require(Permission.ADMIN_MANAGE)
        row = self._session.get(GlossaryTerm, term_id)
        if row is None:
            raise NotFoundError(internal_detail=f"glossary term {term_id} not found")

        self._record(principal, "delete_glossary_term", row)
        self._session.delete(row)
        self._session.flush()

    def load_seed(self, principal: Principal) -> int:
        """기본 용어를 채운다. 이미 있는 용어는 건드리지 않는다.

        덮어쓰지 않는 이유는, 운영자가 고쳐 놓은 뜻과 우선순위를 되돌리면 안 되기
        때문이다. 되돌리고 싶으면 해당 용어를 지우고 다시 불러오면 된다.
        """
        principal.require(Permission.ADMIN_MANAGE)

        added = 0
        for payload in seed_payloads():
            if self._find_by_term(str(payload["term"])) is not None:
                continue
            self._session.add(
                GlossaryTerm(
                    term=str(payload["term"]),
                    aliases=list(payload["aliases"]),  # type: ignore[arg-type]
                    category=str(payload["category"]),
                    definition=str(payload["definition"]),
                    is_active=True,
                    priority=int(payload["priority"]),  # type: ignore[arg-type]
                    updated_by=principal.id,
                )
            )
            added += 1

        self._session.flush()
        self._audit.record(
            AuditEventType.SYSTEM_CONFIG_CHANGED,
            actor=principal.to_audit_actor(),
            action="load_glossary_seed",
            target_type="glossary",
            target_id="seed",
            metadata={"added": added},
        )
        logger.info(
            "glossary seed loaded", extra={"event": "GLOSSARY_SEEDED", "added": added}
        )
        return added

    # --- 공통 ---------------------------------------------------------------

    def _find_by_term(self, term: str) -> GlossaryTerm | None:
        return self._session.execute(
            select(GlossaryTerm).where(GlossaryTerm.term == term)
        ).scalar_one_or_none()

    def _record(self, principal: Principal, action: str, row: GlossaryTerm) -> None:
        """용어 변경을 감사에 남긴다.

        용어를 바꾸면 전사와 분석 결과가 함께 달라지므로, 결과가 이상할 때 "그 사이에
        사전이 바뀌었는가"를 물을 수 있어야 한다 (Harness §37).
        """
        self._audit.record(
            AuditEventType.SYSTEM_CONFIG_CHANGED,
            actor=principal.to_audit_actor(),
            action=action,
            target_type="glossary",
            target_id=row.id,
            metadata={
                "term": row.term,
                "category": row.category,
                "is_active": row.is_active,
                "priority": row.priority,
            },
        )


def _to_view(row: GlossaryTerm) -> TermView:
    return TermView(
        id=row.id,
        term=row.term,
        aliases=list(row.aliases or []),
        category=row.category,
        definition=row.definition,
        is_active=row.is_active,
        priority=row.priority,
        updated_at=row.updated_at,
        updated_by=row.updated_by,
    )


def _clean_term(value: object) -> str:
    text = _clean_text(value, _MAX_TERM_CHARS)
    if not text:
        raise ValidationError("용어를 입력해 주세요.", internal_detail="term is empty")
    return text


def _clean_text(value: object, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip()[:limit]


def _clean_aliases(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    cleaned = []
    for item in value[:_MAX_ALIASES]:
        text = _clean_text(item, _MAX_TERM_CHARS)
        if text and text not in cleaned:
            cleaned.append(text)
    return cleaned


def _clean_priority(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return int(min(max(int(value), 0), 1000))
