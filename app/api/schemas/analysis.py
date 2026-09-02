"""분석 API 스키마 (SEC-001 / SEC-033)."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class AnalysisResponse(BaseModel):
    """LLM 분석 결과.

    Provider 주소·프롬프트 본문은 담지 않는다. 어떤 모델과 프롬프트 버전으로 나왔는지만
    노출해 결과 해석에 필요한 정보를 준다 (Harness §20 / §44).
    """

    job_id: str
    summary: list[str]
    keywords: list[str]
    action_items: list[str]
    customer_reaction: str
    contact_classification: str
    sentiment: str
    opinion: str
    provider: str
    model_name: str
    prompt_version: str
    # 입력이 잘렸는지. 결과를 얼마나 믿을지에 직접 영향을 준다.
    transcript_truncated: bool
    warnings: list[str]
    created_at: datetime


class ConfigEntryResponse(BaseModel):
    key: str
    value: str
    is_overridden: bool
    updated_by: str
    length: int


class ConfigUpdateRequest(BaseModel):
    value: str = Field(max_length=20000)
    # 사유 없는 운영 설정 변경은 나중에 추적이 불가능해진다 (Harness §37).
    reason: str = Field(min_length=1, max_length=500)


class RoleUpdateRequest(BaseModel):
    role: str = Field(min_length=1, max_length=20)
    reason: str = Field(min_length=1, max_length=500)


class UserSummary(BaseModel):
    id: str
    username: str
    role: str
    is_active: bool
    auth_provider: str
