"""용어사전 API 스키마."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from app.api.schemas.common import PageMeta


class TermResponse(BaseModel):
    id: str
    term: str
    aliases: list[str]
    category: str
    definition: str
    is_active: bool
    priority: int
    updated_at: datetime
    updated_by: str


class TermListResponse(BaseModel):
    items: list[TermResponse]
    page: PageMeta
    categories: list[str]


class TermCreateRequest(BaseModel):
    term: str = Field(min_length=1, max_length=120)
    aliases: list[str] = Field(default_factory=list, max_length=10)
    category: str = Field(default="", max_length=40)
    definition: str = Field(default="", max_length=500)
    is_active: bool = True
    # 전사 힌트는 길이 상한이 있어 전부 실리지 않는다. 큰 값이 먼저 실린다.
    priority: int = Field(default=0, ge=0, le=1000)


class TermUpdateRequest(BaseModel):
    """부분 수정. 보내지 않은 필드는 그대로 둔다."""

    term: str | None = Field(default=None, min_length=1, max_length=120)
    aliases: list[str] | None = Field(default=None, max_length=10)
    category: str | None = Field(default=None, max_length=40)
    definition: str | None = Field(default=None, max_length=500)
    is_active: bool | None = None
    priority: int | None = Field(default=None, ge=0, le=1000)


class HintResponse(BaseModel):
    """전사 엔진에 실제로 넘어가는 힌트.

    등록한 용어가 전부 실리지는 않는다 (프롬프트 길이 상한). 무엇이 실렸는지 화면에서
    볼 수 있어야 "등록했는데 왜 안 되지"를 스스로 답할 수 있다 (Harness §4.3).
    """

    hint: str
    included_count: int
    active_count: int
    max_chars: int
