"""웹 화면 테스트 (FR-M-001 / FR-T-005, Harness §11 / §40 / §42 / §47).

화면 자체의 렌더링보다 다음을 확인한다.
  * 미인증 접근이 화면 단계에서 새지 않는가
  * CSP 를 깨는 인라인 스크립트·스타일·핸들러가 섞여 들어가지 않았는가
  * 외부 CDN 의존이 없는가 (폐쇄망 동작)
  * 화면이 Transcript 본문을 서버 렌더로 끼워 넣지 않는가
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.dependencies.ratelimit import (
    api_rate_limiter,
    login_rate_limiter,
    upload_rate_limiter,
)
from app.auth.roles import UserRole
from app.core.config import Settings
from app.core.security import hash_password
from app.main import API_PREFIX, create_app
from app.storage.database import session_scope
from app.storage.models import User
from app.web.routes import STATIC_DIR, TEMPLATES_DIR

_PASSWORD = "Correct-Horse-9!"


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
            ("d-1", "auditor", UserRole.AUDITOR),
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


def _login(client: TestClient, username: str = "agent") -> None:
    response = client.post(
        f"{API_PREFIX}/auth/login", json={"username": username, "password": _PASSWORD}
    )
    assert response.status_code == 200, response.text


# --- 접근 제어 -----------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/",
        "/jobs/stt-anything",
        "/consultations",
        "/realtime",
        "/glossary",
        "/qa",
        "/qa/scores",
        "/qa/compliance",
        "/admin",
        "/admin/integration",
    ],
)
def test_unauthenticated_pages_redirect_to_login(client: TestClient, path: str) -> None:
    """화면은 401 JSON 대신 로그인으로 보낸다. API 는 그대로 401 이다."""
    response = client.get(path, follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_login_page_is_public(client: TestClient) -> None:
    response = client.get("/login")

    assert response.status_code == 200
    assert "로그인" in response.text


def test_login_page_redirects_when_already_signed_in(client: TestClient) -> None:
    _login(client)
    response = client.get("/login", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/"


def test_admin_page_is_separate_from_user_pages(client: TestClient) -> None:
    """FR-M-001: 관리자 화면은 라우트가 분리되어 있고 권한 없는 사용자는 못 본다."""
    _login(client, "agent")
    response = client.get("/admin", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/"


def test_admin_page_opens_for_admin(client: TestClient) -> None:
    _login(client, "root")

    assert client.get("/admin").status_code == 200


def test_auditor_sees_audit_section_but_not_system_settings(client: TestClient) -> None:
    """직무분리: AUDITOR 는 감사만 본다 (Harness §46)."""
    _login(client, "auditor")
    body = client.get("/admin").text

    assert "감사 로그" in body
    assert "시스템 상태" in body
    # 시스템 설정·모델 상태를 채우는 자리는 렌더되지 않는다.
    assert 'id="status-grid"' not in body


def test_upload_form_hidden_without_permission(client: TestClient) -> None:
    """버튼을 숨기는 것은 권한 제어가 아니지만(Harness §10), 쓸 수 없는 UI 를 보일 이유도 없다."""
    _login(client, "auditor")
    response = client.get("/admin")

    assert 'id="upload-form"' not in response.text


# --- CSP 준수 (Harness §11) ----------------------------------------------------


def _rendered_pages(client: TestClient) -> dict[str, str]:
    _login(client, "root")
    return {
        "/login": client.get("/login", follow_redirects=True).text,
        "/": client.get("/").text,
        "/jobs/stt-x": client.get("/jobs/stt-x").text,
        "/consultations": client.get("/consultations").text,
        "/realtime": client.get("/realtime").text,
        "/glossary": client.get("/glossary").text,
        "/admin/integration": client.get("/admin/integration").text,
        "/qa": client.get("/qa").text,
        "/qa/scores": client.get("/qa/scores").text,
        "/qa/compliance": client.get("/qa/compliance").text,
        "/admin": client.get("/admin").text,
    }


def test_no_inline_scripts(client: TestClient) -> None:
    """CSP 가 script-src 'self' 다. 인라인 스크립트는 실행되지 않는다."""
    for path, body in _rendered_pages(client).items():
        for match in re.finditer(r"<script\b([^>]*)>(.*?)</script>", body, re.S):
            attributes, content = match.groups()
            assert "src=" in attributes, f"{path}: src 없는 <script> 가 있다"
            assert content.strip() == "", f"{path}: 인라인 스크립트 본문이 있다"


def test_no_inline_styles_or_handlers(client: TestClient) -> None:
    """style= 속성과 on* 핸들러도 CSP 에 막힌다."""
    for path, body in _rendered_pages(client).items():
        assert "<style" not in body, f"{path}: 인라인 <style> 이 있다"
        assert re.search(r"\sstyle\s*=", body) is None, f"{path}: style= 속성이 있다"
        assert re.search(r"\son(click|load|error|submit|change)\s*=", body) is None, (
            f"{path}: 인라인 이벤트 핸들러가 있다"
        )


def test_no_external_resources(client: TestClient) -> None:
    """폐쇄망에서 동작해야 한다 (Harness §42). 외부 CDN 을 참조하지 않는다."""
    for path, body in _rendered_pages(client).items():
        assert "//cdn" not in body, f"{path}: 외부 CDN 참조"
        assert "https://" not in body, f"{path}: 외부 자원 참조"


def test_static_assets_are_served(client: TestClient) -> None:
    for asset in (
        "/static/app.css",
        "/static/app.js",
        "/static/jobs.js",
        "/static/consultations.js",
        "/static/qa.js",
        "/static/qa_list.js",
        "/static/realtime.js",
        "/static/pcm-worklet.js",
        "/static/glossary.js",
        "/static/integration.js",
    ):
        response = client.get(asset)
        assert response.status_code == 200, asset
        assert response.content


def test_pages_carry_security_headers(client: TestClient) -> None:
    headers = client.get("/login").headers

    assert headers["X-Frame-Options"] == "DENY"
    assert "script-src 'self'" in headers["Content-Security-Policy"]
    assert "'unsafe-inline'" not in headers["Content-Security-Policy"]


# --- Transcript 취급 (FR-T-005) -------------------------------------------------


def test_pages_do_not_server_render_transcripts(client: TestClient) -> None:
    """본문은 API 에서 받아 textContent 로 넣는다. 서버 렌더 경로를 두지 않는다."""
    _login(client)
    body = client.get("/jobs/stt-x").text

    # 세그먼트 목록은 서버가 비운 채로 내려보내고 JS 가 채운다.
    assert '<ol class="segments" id="segments"></ol>' in body
    # 서버 템플릿에는 Transcript 를 꺼내는 표현식이 없다.
    assert "{{" not in body


def _strip_js_comments(source: str) -> str:
    """주석을 걷어낸다. 주석 속 단어까지 금지어로 세면 "쓰지 않는다"는 설명도 걸린다."""
    without_block = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    return re.sub(r"^\s*//.*$", "", without_block, flags=re.M)


def test_javascript_never_uses_innerhtml() -> None:
    """DOM 주입 경로 자체를 만들지 않는다 (Harness §13)."""
    for script in STATIC_DIR.glob("*.js"):
        code = _strip_js_comments(script.read_text(encoding="utf-8"))
        assert "innerHTML" not in code, f"{script.name}: innerHTML 사용"
        assert "outerHTML" not in code, f"{script.name}: outerHTML 사용"
        assert "insertAdjacentHTML" not in code, f"{script.name}: insertAdjacentHTML 사용"
        assert "document.write" not in code, f"{script.name}: document.write 사용"


def test_templates_do_not_disable_autoescape() -> None:
    """Jinja2 autoescape 를 끄거나 |safe 로 우회하지 않는다 (FR-T-005)."""
    for template in Path(TEMPLATES_DIR).glob("*.html"):
        source = template.read_text(encoding="utf-8")
        assert "|safe" not in source, f"{template.name}: |safe 사용"
        assert "autoescape false" not in source, f"{template.name}: autoescape 해제"


def test_username_is_escaped_in_the_page(client: TestClient, database: None) -> None:  # noqa: ARG001
    """사용자명도 신뢰하지 않는 값으로 다룬다."""
    with session_scope() as session:
        session.add(
            User(
                id="x-1",
                username="<script>alert(1)</script>",
                role=UserRole.USER,
                password_hash=hash_password(_PASSWORD, rounds=4),
                auth_provider="local",
            )
        )
    _login(client, "<script>alert(1)</script>")

    body = client.get("/").text

    assert "<script>alert(1)</script>" not in body
    assert "&lt;script&gt;" in body


# --- 상담 목록 / QA 화면 -----------------------------------------------------------


def test_consultation_page_leaves_the_script_for_javascript(client: TestClient) -> None:
    """스크립트 본문은 API 에서 받아 textContent 로 넣는다. 서버 렌더 경로를 두지 않는다."""
    _login(client)
    body = client.get("/consultations").text

    assert '<ol class="segments" id="script-segments"></ol>' in body
    assert "{{" not in body


def test_consultation_shows_details_first(client: TestClient) -> None:
    """상세정보가 기본이다. 캡션은 눌러야 돈다 — 자동 재생은 확인을 방해한다."""
    _login(client)
    body = client.get("/consultations").text

    assert 'id="consult-facts"' in body
    assert 'id="detail-panel"' in body
    # 재생 버튼은 있되, 자동으로 돌지 않는다 (스크립트 쪽에서 검증한다).
    assert 'id="script-caption"' in body


def test_recording_delete_is_admin_only_in_the_ui(client: TestClient) -> None:
    """버튼을 숨기는 것은 권한 제어가 아니지만(Harness §10), 쓸 수 없는 UI 를 보일
    이유도 없다. 실제 차단은 API 가 한다."""
    _login(client, "agent")
    body = client.get("/consultations").text

    assert 'id="consult-delete-mode"' not in body
    assert 'id="delete-modal"' not in body

    _login(client, "root")
    body = client.get("/consultations").text

    assert 'id="consult-delete-mode"' in body
    assert 'id="consult-delete"' in body
    assert 'id="delete-modal"' in body


def test_delete_modal_warns_before_confirming(client: TestClient) -> None:
    """무엇이 사라지는지 밝히지 않고 확인을 받으면 안 된다 (Harness §48)."""
    _login(client, "root")
    body = client.get("/consultations").text

    assert "되돌릴 수 없습니다" in body
    assert "감사 로그에 기록됩니다" in body
    assert 'id="delete-modal-confirm"' in body
    assert 'id="delete-modal-cancel"' in body


def test_consultation_script_does_not_autoplay_captions() -> None:
    """목록 화면에서 캡션이 저절로 돌면 내용을 확인하려는 사람에게 방해가 된다."""
    source = (STATIC_DIR / "consultations.js").read_text(encoding="utf-8")
    code = _strip_js_comments(source)

    # 재생 함수는 '캡션 재생' 핸들러 안에서만 불린다.
    assert code.count("playCaptions(") == 1
    assert "script-caption" in code


def test_qa_menus_are_present_for_signed_in_users(client: TestClient) -> None:
    _login(client)
    body = client.get("/").text

    assert 'href="/qa"' in body
    assert 'href="/qa/scores"' in body
    assert 'href="/qa/compliance"' in body
    assert 'href="/consultations"' in body


def test_qa_rubric_editor_is_admin_only(client: TestClient) -> None:
    """감사 권한만 있는 사용자에게 평가 기준 편집기를 그리지 않는다 (Harness §46)."""
    _login(client, "auditor")
    assert 'id="rubric-text"' not in client.get("/admin").text

    _login(client, "root")
    body = client.get("/admin").text
    assert 'id="rubric-text"' in body
    assert 'id="compliance-text"' in body


# --- 실시간 전사 화면 ---------------------------------------------------------------


def test_realtime_menu_is_present(client: TestClient) -> None:
    _login(client)

    assert 'href="/realtime"' in client.get("/").text


def test_realtime_page_says_why_it_is_unavailable(client: TestClient) -> None:
    """기능이 꺼져 있어도 화면은 연다. 설정이 꺼진 것과 화면이 없는 것은 다르다."""
    _login(client)
    body = client.get("/realtime").text

    assert 'data-enabled="false"' in body
    assert "ENABLE_REALTIME_STT" in body
    # 쓸 수 없는 화면에 마이크 컨트롤을 그리지 않는다.
    assert 'id="rt-segments"' not in body


def test_worklet_is_served_as_javascript(client: TestClient) -> None:
    """AudioWorklet 모듈은 같은 출처에서 와야 한다 (script-src 'self')."""
    response = client.get("/static/pcm-worklet.js")

    assert response.status_code == 200
    assert "registerProcessor" in response.text


def test_topbar_shows_who_is_signed_in(client: TestClient) -> None:
    """사이드바가 접히는 좁은 화면에서도 접속 주체가 보여야 한다."""
    _login(client, "root")
    body = client.get("/").text

    assert 'class="topbar-user"' in body
    assert 'id="logout-top"' in body
    # 사용자명과 역할이 함께 나온다.
    assert "root" in body
    assert "ADMIN" in body


def test_dashboard_has_no_delete_action(client: TestClient) -> None:
    """되돌릴 수 없는 동작이 여러 화면에 흩어져 있으면 실수하기 쉽다.

    대시보드는 진행 상황을 보는 곳이고, 삭제는 상담 목록 화면에만 둔다.
    """
    _login(client, "root")
    body = client.get("/").text

    # 삭제를 시작하는 버튼도, 확인 팝업도 이 화면에는 없다.
    assert 'id="consult-delete-mode"' not in body
    assert 'id="consult-delete"' not in body
    assert 'id="delete-modal"' not in body


def test_delete_modal_is_rendered_only_where_deletion_happens(
    client: TestClient,
) -> None:
    """열 수 없는 팝업을 모든 화면에 렌더할 이유가 없다."""
    _login(client, "root")

    assert 'id="delete-modal"' in client.get("/consultations").text
    assert 'id="delete-modal"' not in client.get("/qa").text


def test_consultations_can_filter_by_status(client: TestClient) -> None:
    """취소·실패한 상담도 여기서 정리한다. 기본은 완료된 상담이다."""
    _login(client, "root")
    body = client.get("/consultations").text

    assert 'id="consult-status"' in body
    assert 'value="CANCELLED"' in body
    assert 'value="FAILED"' in body
    # 기본 선택은 완료다 — 이 화면의 목적은 스크립트를 읽는 것이다.
    assert body.index('value="COMPLETED"') < body.index('value="CANCELLED"')
