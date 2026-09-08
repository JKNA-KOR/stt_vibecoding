"""용어사전 라우트 (FR-M-004).

읽기는 로그인한 사용자면 누구나 할 수 있다. 용어의 뜻은 상담원이 통화 중에 확인해야
하는 정보이며 민감정보가 아니다. 변경은 관리자만 하고 전부 감사에 남는다 —
용어를 바꾸면 전사와 분석 결과가 함께 달라지기 때문이다.
"""

from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from app.api.dependencies.common import csrf_protected, db_session, rate_limited, settings_dep
from app.api.schemas.common import PageMeta
from app.api.schemas.glossary import (
    HintResponse,
    TermCreateRequest,
    TermListResponse,
    TermResponse,
    TermUpdateRequest,
)
from app.auth.principal import Principal
from app.core.config import Settings
from app.glossary.service import HINT_MAX_BYTES, GlossaryService

router = APIRouter(prefix="/glossary", tags=["glossary"])


def glossary_service(
    session: Session = Depends(db_session),
    settings: Settings = Depends(settings_dep),
) -> GlossaryService:
    return GlossaryService(session, settings=settings)


@router.get("", response_model=TermListResponse)
def list_terms(
    search: str = Query(default="", max_length=120),
    category: str = Query(default="", max_length=40),
    include_inactive: bool = Query(default=True),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    principal: Principal = Depends(rate_limited),  # noqa: ARG001 - 인증만 요구한다
    service: GlossaryService = Depends(glossary_service),
) -> TermListResponse:
    items, total = service.list_terms(
        search=search,
        category=category,
        include_inactive=include_inactive,
        limit=limit,
        offset=offset,
    )
    return TermListResponse(
        items=[TermResponse(**asdict(item)) for item in items],
        page=PageMeta(total=total, limit=limit, offset=offset),
        categories=service.categories(),
    )


@router.get("/hint", response_model=HintResponse)
def transcription_hint(
    principal: Principal = Depends(rate_limited),  # noqa: ARG001 - 인증만 요구한다
    service: GlossaryService = Depends(glossary_service),
) -> HintResponse:
    """전사 엔진에 실제로 넘어가는 힌트를 그대로 보여준다."""
    hint = service.transcription_hint()
    _, active_count = service.list_terms(include_inactive=False, limit=1)
    return HintResponse(
        hint=hint,
        included_count=len([part for part in hint.split(", ") if part]),
        active_count=active_count,
        max_bytes=HINT_MAX_BYTES,
        hint_bytes=len(hint.encode("utf-8")),
    )


@router.post("", response_model=TermResponse, status_code=status.HTTP_201_CREATED)
def create_term(
    payload: TermCreateRequest,
    principal: Principal = Depends(csrf_protected),
    service: GlossaryService = Depends(glossary_service),
) -> TermResponse:
    return TermResponse(**asdict(service.create(principal, payload.model_dump())))


@router.put("/{term_id}", response_model=TermResponse)
def update_term(
    term_id: str,
    payload: TermUpdateRequest,
    principal: Principal = Depends(csrf_protected),
    service: GlossaryService = Depends(glossary_service),
) -> TermResponse:
    # 보내지 않은 필드는 건드리지 않는다. `exclude_unset` 이 그 경계를 만든다.
    changes = payload.model_dump(exclude_unset=True)
    return TermResponse(**asdict(service.update(principal, term_id, changes)))


@router.delete("/{term_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_term(
    term_id: str,
    principal: Principal = Depends(csrf_protected),
    service: GlossaryService = Depends(glossary_service),
) -> None:
    service.delete(principal, term_id)


@router.post("/seed", response_model=dict[str, int])
def load_seed(
    principal: Principal = Depends(csrf_protected),
    service: GlossaryService = Depends(glossary_service),
) -> dict[str, int]:
    """기본 금융 용어를 채운다. 이미 있는 용어는 건드리지 않는다."""
    return {"added": service.load_seed(principal)}
