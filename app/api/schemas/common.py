"""공통 응답 스키마 (SEC-032 / SEC-033, Harness §23 / §44).

응답에 담는 것은 오류 분류 코드와 사용자용 문구, 그리고 상관관계 ID 뿐이다.
내부 경로·DB PK·호스트명·스택트레이스·설정값은 어떤 필드로도 나가지 않는다.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class ErrorBody(BaseModel):
    code: str = Field(description="Harness §23 오류 분류 코드")
    message: str = Field(description="사용자에게 보여줄 문구")
    request_id: str = Field(default="", description="장애 문의 시 사용할 상관관계 ID")


class ErrorResponse(BaseModel):
    """표준 오류 응답 `{code, message, request_id}` (SEC-032)."""

    error: ErrorBody


class PageMeta(BaseModel):
    total: int
    limit: int
    offset: int
