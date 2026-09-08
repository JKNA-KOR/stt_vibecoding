"""용어사전 통합 테스트 (FR-M-004, Harness §10 / §12 / §17 / §37).

용어를 바꾸면 전사와 분석 결과가 함께 달라진다. 그래서 확인해야 하는 것은 세 가지다 —
권한, 감사, 그리고 **힌트가 잘릴 때 그 사실이 드러나는가**.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.api.dependencies.ratelimit import (
    api_rate_limiter,
    login_rate_limiter,
    upload_rate_limiter,
)
from app.auth.roles import UserRole
from app.auth.session import CSRF_HEADER_NAME
from app.core.config import Settings
from app.core.security import hash_password
from app.glossary.service import HINT_MAX_BYTES
from app.main import API_PREFIX, create_app
from app.storage.database import session_scope
from app.storage.models import AuditEvent, User

_PASSWORD = "Correct-Horse-9!"
_GLOSSARY = f"{API_PREFIX}/glossary"


@pytest.fixture(autouse=True)
def _reset_rate_limiters() -> Iterator[None]:
    for limiter in (api_rate_limiter, login_rate_limiter, upload_rate_limiter):
        limiter.reset()
    yield
    for limiter in (api_rate_limiter, login_rate_limiter, upload_rate_limiter):
        limiter.reset()


@pytest.fixture
def accounts(database: None) -> None:  # noqa: ARG001
    with session_scope() as session:
        for user_id, username, role in (
            ("u-1", "agent", UserRole.USER),
            ("a-1", "root", UserRole.ADMIN),
        ):
            session.add(
                User(
                    id=user_id,
                    username=username,
                    role=role,
                    password_hash=hash_password(_PASSWORD, rounds=4),
                    auth_provider="local",
                )
            )


@pytest.fixture
def client(settings: Settings, accounts: None) -> Iterator[TestClient]:  # noqa: ARG001
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def _login(client: TestClient, username: str = "root") -> str:
    response = client.post(
        f"{API_PREFIX}/auth/login", json={"username": username, "password": _PASSWORD}
    )
    assert response.status_code == 200, response.text
    return response.json()["csrf_token"]


def _add(client: TestClient, csrf: str, **payload: object) -> dict:
    body = {"term": "테스트용어", "definition": "뜻", "priority": 10, **payload}
    response = client.post(_GLOSSARY, json=body, headers={CSRF_HEADER_NAME: csrf})
    assert response.status_code == 201, response.text
    return response.json()


# --- 권한 (Harness §10) -----------------------------------------------------------


def test_anyone_signed_in_can_read_terms(client: TestClient) -> None:
    """용어의 뜻은 상담원이 통화 중에 확인해야 하는 정보다. 민감정보가 아니다."""
    csrf = _login(client, "root")
    _add(client, csrf, term="중도상환수수료")

    _login(client, "agent")
    body = client.get(_GLOSSARY).json()

    assert [item["term"] for item in body["items"]] == ["중도상환수수료"]


def test_only_admins_can_change_terms(client: TestClient) -> None:
    csrf = _login(client, "agent")

    response = client.post(
        _GLOSSARY, json={"term": "무단추가"}, headers={CSRF_HEADER_NAME: csrf}
    )

    assert response.status_code == 403


def test_changing_a_term_requires_csrf(client: TestClient) -> None:
    _login(client, "root")

    assert client.post(_GLOSSARY, json={"term": "토큰없음"}).status_code == 403


# --- 등록과 수정 -------------------------------------------------------------------


def test_terms_are_created_and_listed(client: TestClient) -> None:
    csrf = _login(client)
    created = _add(
        client, csrf, term="리볼빙", category="카드", aliases=["일부결제금액이월약정"]
    )

    assert created["term"] == "리볼빙"
    assert created["aliases"] == ["일부결제금액이월약정"]

    body = client.get(_GLOSSARY).json()
    assert body["page"]["total"] == 1
    assert body["categories"] == ["카드"]


def test_duplicate_terms_are_refused(client: TestClient) -> None:
    """같은 용어가 두 번 들어가면 힌트에 중복으로 실린다."""
    csrf = _login(client)
    _add(client, csrf, term="거치기간")

    response = client.post(
        _GLOSSARY, json={"term": "거치기간"}, headers={CSRF_HEADER_NAME: csrf}
    )

    assert response.status_code == 409


def test_partial_update_leaves_other_fields_alone(client: TestClient) -> None:
    csrf = _login(client)
    created = _add(client, csrf, term="연체이자", definition="원래 뜻", category="여신")

    updated = client.put(
        f"{_GLOSSARY}/{created['id']}",
        json={"is_active": False},
        headers={CSRF_HEADER_NAME: csrf},
    ).json()

    assert updated["is_active"] is False
    assert updated["definition"] == "원래 뜻"
    assert updated["category"] == "여신"


def test_terms_can_be_deleted(client: TestClient) -> None:
    csrf = _login(client)
    created = _add(client, csrf, term="삭제대상")

    response = client.delete(
        f"{_GLOSSARY}/{created['id']}", headers={CSRF_HEADER_NAME: csrf}
    )

    assert response.status_code == 204
    assert client.get(_GLOSSARY).json()["page"]["total"] == 0


def test_search_uses_bound_parameters(client: TestClient) -> None:
    """검색어를 문자열로 이어 붙이지 않는다 (Harness §12). 특수문자가 와도 깨지지 않는다."""
    csrf = _login(client)
    _add(client, csrf, term="한도조회")

    body = client.get(_GLOSSARY, params={"search": "100%' OR '1'='1"}).json()

    assert body["page"]["total"] == 0


# --- 기본 용어 --------------------------------------------------------------------


def test_seed_loads_default_financial_terms(client: TestClient) -> None:
    csrf = _login(client)

    added = client.post(f"{_GLOSSARY}/seed", headers={CSRF_HEADER_NAME: csrf}).json()

    assert added["added"] >= 1000
    assert client.get(_GLOSSARY, params={"limit": 1}).json()["page"]["total"] >= 1000

    # 1000개를 한 페이지로 받지 않는다. 검색으로 확인한다.
    for term in ("중도상환수수료", "불완전판매", "부장", "리볼빙"):
        found = client.get(_GLOSSARY, params={"search": term}).json()
        assert term in {item["term"] for item in found["items"]}, term


def test_seed_includes_positions_and_titles(client: TestClient) -> None:
    """상담에서 호칭으로 자주 나오고 짧아 오인식이 잦다."""
    csrf = _login(client)
    client.post(f"{_GLOSSARY}/seed", headers={CSRF_HEADER_NAME: csrf})

    body = client.get(_GLOSSARY, params={"category": "직위", "limit": 500}).json()
    positions = {item["term"] for item in body["items"]}
    assert {"부장", "차장", "과장", "대리", "상무", "전무", "대표이사"} <= positions

    body = client.get(_GLOSSARY, params={"category": "직책", "limit": 500}).json()
    titles = {item["term"] for item in body["items"]}
    assert {"지점장", "팀장", "센터장", "상담원", "준법감시인"} <= titles


def test_seed_terms_are_unique(client: TestClient) -> None:
    """같은 용어가 두 번 실리면 힌트에 중복으로 들어가고 등록도 조용히 건너뛴다."""
    from app.glossary.seed import SEED_TERMS

    terms = [term for term, _, _, _ in SEED_TERMS]
    assert len(terms) == len(set(terms))


def test_seed_priority_reflects_the_category(client: TestClient) -> None:
    """힌트 길이 상한 때문에 전부 실을 수 없다. 오인식이 잦은 분류가 먼저 실려야 한다."""
    csrf = _login(client)
    client.post(f"{_GLOSSARY}/seed", headers={CSRF_HEADER_NAME: csrf})

    hint = client.get(f"{_GLOSSARY}/hint").json()

    # 1000개 중 일부만 실린다. 그 사실이 응답에 드러나야 한다.
    assert hint["active_count"] >= 1000
    assert hint["included_count"] < hint["active_count"]
    # 여신 용어가 우선순위가 가장 높다.
    assert "중도상환수수료" in hint["hint"]


def test_seed_does_not_overwrite_edited_terms(client: TestClient) -> None:
    """운영자가 고쳐 놓은 뜻을 기본값이 되돌리면 안 된다."""
    csrf = _login(client)
    _add(client, csrf, term="중도상환수수료", definition="우리 센터 기준 설명")

    client.post(f"{_GLOSSARY}/seed", headers={CSRF_HEADER_NAME: csrf})

    body = client.get(_GLOSSARY, params={"search": "중도상환수수료"}).json()
    assert body["items"][0]["definition"] == "우리 센터 기준 설명"


# --- 전사 힌트 --------------------------------------------------------------------


def test_hint_contains_active_terms_only(client: TestClient) -> None:
    csrf = _login(client)
    _add(client, csrf, term="사용중용어")
    disabled = _add(client, csrf, term="중지된용어")
    client.put(
        f"{_GLOSSARY}/{disabled['id']}",
        json={"is_active": False},
        headers={CSRF_HEADER_NAME: csrf},
    )

    hint = client.get(f"{_GLOSSARY}/hint").json()

    assert "사용중용어" in hint["hint"]
    assert "중지된용어" not in hint["hint"]


def test_hint_reports_what_it_had_to_drop(client: TestClient) -> None:
    """등록했는데 왜 안 되는지를 화면에서 스스로 답할 수 있어야 한다 (Harness §4.3)."""
    csrf = _login(client)
    for index in range(60):
        _add(client, csrf, term=f"아주긴금융용어이름{index:03d}", priority=index)

    hint = client.get(f"{_GLOSSARY}/hint").json()

    assert len(hint["hint"].encode("utf-8")) <= HINT_MAX_BYTES
    assert hint["active_count"] == 60
    assert hint["included_count"] < hint["active_count"]


def test_higher_priority_terms_survive_the_cut(client: TestClient) -> None:
    csrf = _login(client)
    _add(client, csrf, term="가장중요한용어", priority=1000)
    for index in range(60):
        _add(client, csrf, term=f"덜중요한용어이름{index:03d}", priority=1)

    hint = client.get(f"{_GLOSSARY}/hint").json()

    assert "가장중요한용어" in hint["hint"]


def test_empty_glossary_produces_no_hint(client: TestClient) -> None:
    """사전이 비어 있으면 아무것도 넘기지 않는다 — 기존 동작과 같아진다."""
    _login(client)

    assert client.get(f"{_GLOSSARY}/hint").json()["hint"] == ""


# --- 감사 (Harness §37) -----------------------------------------------------------


def test_term_changes_are_audited(client: TestClient) -> None:
    """결과가 이상할 때 '그 사이 사전이 바뀌었는가'를 물을 수 있어야 한다."""
    csrf = _login(client)
    created = _add(client, csrf, term="감사대상용어")
    client.delete(f"{_GLOSSARY}/{created['id']}", headers={CSRF_HEADER_NAME: csrf})

    with session_scope() as session:
        actions = {row.action for row in session.query(AuditEvent).all()}

    assert "create_glossary_term" in actions
    assert "delete_glossary_term" in actions


def test_prompt_appendix_carries_definitions_and_aliases(
    client: TestClient, settings: Settings
) -> None:
    """분석·QA 는 뜻과 오인식 표기를 함께 받아야 같은 용어로 읽는다.

    전사 결과를 고쳐 쓰지는 않는다 — 정규화기는 어휘를 바꾸지 않는다는 계약이 있고,
    그것을 깨려면 버전을 올리고 회귀 테스트를 다시 돌려야 한다 (Harness §36).
    """
    csrf = _login(client)
    _add(
        client,
        csrf,
        term="중도상환수수료",
        definition="약정 기간 전에 갚을 때 내는 수수료.",
        aliases=["중도 상환 수수료"],
    )

    from app.glossary.service import GlossaryService

    with session_scope() as session:
        appendix = GlossaryService(session, settings=settings).prompt_appendix()

    assert "중도상환수수료: 약정 기간 전에 갚을 때 내는 수수료." in appendix
    assert "중도 상환 수수료" in appendix


def test_hint_is_capped_in_bytes_not_characters(client: TestClient) -> None:
    """한글은 UTF-8 로 글자당 3바이트다.

    글자 수로 재면 393자짜리 힌트가 935바이트가 되어 엔드포인트의 896바이트 상한을
    넘고, **전사가 통째로 실패한다.** 실제로 그렇게 실패한 적이 있어 못박아 둔다.
    """
    csrf = _login(client)
    for index in range(200):
        _add(client, csrf, term=f"한글로만이루어진긴금융용어{index:03d}", priority=index)

    hint = client.get(f"{_GLOSSARY}/hint").json()

    assert hint["hint_bytes"] == len(hint["hint"].encode("utf-8"))
    assert hint["hint_bytes"] <= hint["max_bytes"]
    # 글자 수는 상한보다 훨씬 적다 — 바이트로 세고 있다는 증거다.
    assert len(hint["hint"]) < hint["max_bytes"]


def test_full_seed_hint_fits_the_endpoint_limit(client: TestClient) -> None:
    """기본 사전 1000개를 전부 켠 상태에서도 힌트가 상한을 넘지 않아야 한다."""
    csrf = _login(client)
    client.post(f"{_GLOSSARY}/seed", headers={CSRF_HEADER_NAME: csrf})

    hint = client.get(f"{_GLOSSARY}/hint").json()

    # Groq 가 강제하는 값(896바이트)보다 여유가 있어야 한다.
    assert hint["hint_bytes"] <= 896
    assert hint["hint_bytes"] <= hint["max_bytes"]
