"""요청 단위 상관관계 컨텍스트 (Harness §11 / §16).

로거와 감사 서비스가 동일한 `request_id` 를 참조할 수 있도록 ContextVar 로 보관한다.
장애 분석은 이 값과 `job_id` 만으로 가능해야 한다 (Harness §35).
"""

from __future__ import annotations

import uuid
from contextvars import ContextVar
from dataclasses import dataclass

_request_id: ContextVar[str | None] = ContextVar("request_id", default=None)
_actor_id: ContextVar[str | None] = ContextVar("actor_id", default=None)
_actor_role: ContextVar[str | None] = ContextVar("actor_role", default=None)
_source_ip_masked: ContextVar[str | None] = ContextVar("source_ip_masked", default=None)


@dataclass(frozen=True, slots=True)
class RequestContext:
    """현재 요청의 비민감 식별자 묶음."""

    request_id: str | None
    actor_id: str | None
    actor_role: str | None
    source_ip_masked: str | None


def new_request_id() -> str:
    return f"req-{uuid.uuid4().hex}"


def set_request_id(request_id: str) -> None:
    _request_id.set(request_id)


def get_request_id() -> str | None:
    return _request_id.get()


def set_actor(actor_id: str | None, actor_role: str | None) -> None:
    _actor_id.set(actor_id)
    _actor_role.set(actor_role)


def set_source_ip_masked(value: str | None) -> None:
    _source_ip_masked.set(value)


def current_context() -> RequestContext:
    return RequestContext(
        request_id=_request_id.get(),
        actor_id=_actor_id.get(),
        actor_role=_actor_role.get(),
        source_ip_masked=_source_ip_masked.get(),
    )


def mask_ip(raw_ip: str | None) -> str | None:
    """클라이언트 IP 를 비식별화한다 (Harness §16 client_ip_masked).

    IPv4 는 마지막 옥텟을, IPv6 는 하위 64비트를 제거해 개별 단말 추적을 어렵게 하되
    네트워크 대역 단위의 이상징후 탐지(Harness §32)는 유지한다.
    """
    if not raw_ip:
        return None
    if ":" in raw_ip:
        head = raw_ip.split(":")[:4]
        return ":".join(head) + "::/64"
    parts = raw_ip.split(".")
    if len(parts) != 4:
        return "invalid"
    return f"{parts[0]}.{parts[1]}.{parts[2]}.0/24"
