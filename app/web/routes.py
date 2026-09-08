"""웹 화면 라우트 (Harness §40 / §42 / §47, FR-T-005).

서버는 뼈대 HTML 만 내려주고 데이터는 JSON API 에서 가져온다. 화면 로직이 API 를 쓰는
구조라 권한 판단이 한 곳(백엔드)에만 있게 된다 — 템플릿이 직접 DB 를 읽기 시작하면
API 와 화면의 권한 규칙이 갈라진다.

빌드 체인을 두지 않는다 (Jinja2 + Vanilla JS). Node 의존성이 없으므로 공급망 표면이
좁고(Harness §40) 폐쇄망에서도 그대로 동작한다 (§42).

CSP 가 `script-src 'self'; style-src 'self'` 이므로 인라인 `<script>`, `<style>`,
`style=` 속성, `onclick=` 핸들러를 쓸 수 없다. 전부 외부 파일과 `addEventListener` 다.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.api.dependencies.common import optional_principal
from app.auth.principal import Principal
from app.auth.roles import Permission

WEB_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = WEB_DIR / "templates"
STATIC_DIR = WEB_DIR / "static"

# autoescape 는 Jinja2 의 기본값이며, Transcript 를 화면에 넣을 때의 1차 방어선이다
# (FR-T-005). 본문은 대부분 JS 가 textContent 로 넣지만, 서버 렌더 경로도 막아 둔다.
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# 화면 라우트는 OpenAPI 스키마에 넣지 않는다. API 문서는 JSON API 만 기술한다.
# `response_model=None` 은 반환이 HTMLResponse 또는 RedirectResponse 인 유니온이라
# FastAPI 가 응답 모델을 유추하지 못하기 때문이다.
router = APIRouter(include_in_schema=False)


def _redirect_to_login(request: Request) -> RedirectResponse:
    """미인증 화면 요청은 401 JSON 이 아니라 로그인 화면으로 보낸다.

    API 는 401 을 그대로 주고, 화면만 이렇게 다룬다. 브라우저 사용자에게 JSON 오류를
    보여주는 것은 쓸모가 없다.
    """
    return RedirectResponse(url="/login", status_code=303)


@router.get("/login", response_class=HTMLResponse, response_model=None)
def login_page(
    request: Request, principal: Principal | None = Depends(optional_principal)
) -> HTMLResponse | RedirectResponse:
    if principal is not None:
        return RedirectResponse(url="/", status_code=303)
    return templates.TemplateResponse(request, "login.html", {"page": "login"})


@router.get("/", response_class=HTMLResponse, response_model=None)
def jobs_page(
    request: Request, principal: Principal | None = Depends(optional_principal)
) -> HTMLResponse | RedirectResponse:
    if principal is None:
        return _redirect_to_login(request)
    return templates.TemplateResponse(
        request, "jobs.html", {"page": "jobs", **_view_context(principal)}
    )


@router.get("/jobs/{job_id}", response_class=HTMLResponse, response_model=None)
def job_detail_page(
    request: Request,
    job_id: str,
    principal: Principal | None = Depends(optional_principal),
) -> HTMLResponse | RedirectResponse:
    """Job 상세 화면.

    `job_id` 의 접근 권한은 여기서 판단하지 않는다. 화면은 뼈대만 그리고, 데이터를 가져오는
    API 호출이 권한을 검사한다 — 권한이 없으면 화면에 "찾을 수 없음"이 표시된다.
    """
    if principal is None:
        return _redirect_to_login(request)
    return templates.TemplateResponse(
        request,
        "job_detail.html",
        {"page": "jobs", "job_id": job_id, **_view_context(principal)},
    )


@router.get("/consultations", response_class=HTMLResponse, response_model=None)
def consultations_page(
    request: Request, principal: Principal | None = Depends(optional_principal)
) -> HTMLResponse | RedirectResponse:
    """상담 목록 화면.

    작업 목록(`/`)과 나누어 둔 이유는 보는 목적이 다르기 때문이다. 목록 화면은 "전사가
    잘 돌았는가"를 보고, 이 화면은 "무슨 말이 오갔는가"를 본다 — 그래서 여기서는 변환된
    스크립트를 곧바로 펼쳐 준다.
    """
    if principal is None:
        return _redirect_to_login(request)
    return templates.TemplateResponse(
        request, "consultations.html", {"page": "consultations", **_view_context(principal)}
    )


@router.get("/realtime", response_class=HTMLResponse, response_model=None)
def realtime_page(
    request: Request, principal: Principal | None = Depends(optional_principal)
) -> HTMLResponse | RedirectResponse:
    """실시간 전사 화면.

    기능이 꺼져 있어도 화면은 연다. 화면이 사라지는 것보다 "왜 쓸 수 없는지"를 보여주는
    편이 낫다 — 설정이 꺼진 것과 화면이 없는 것을 사용자가 구분할 수 있어야 한다.
    """
    if principal is None:
        return _redirect_to_login(request)

    from app.core.config import get_settings

    return templates.TemplateResponse(
        request,
        "realtime.html",
        {
            "page": "realtime",
            "realtime_enabled": get_settings().enable_realtime_stt,
            **_view_context(principal),
        },
    )


@router.get("/glossary", response_class=HTMLResponse, response_model=None)
def glossary_page(
    request: Request, principal: Principal | None = Depends(optional_principal)
) -> HTMLResponse | RedirectResponse:
    """용어사전 화면.

    읽기는 누구나 할 수 있고 편집은 관리자만 할 수 있다. 화면은 편집 도구를 권한에 따라
    감추지만, 실제 판단은 API 가 다시 한다 (SEC-010).
    """
    if principal is None:
        return _redirect_to_login(request)
    return templates.TemplateResponse(
        request, "glossary.html", {"page": "glossary", **_view_context(principal)}
    )


@router.get("/qa", response_class=HTMLResponse, response_model=None)
def qa_page(
    request: Request, principal: Principal | None = Depends(optional_principal)
) -> HTMLResponse | RedirectResponse:
    """QA 개요. 평균 점수와 등급 분포를 본다."""
    if principal is None:
        return _redirect_to_login(request)
    return templates.TemplateResponse(
        request, "qa.html", {"page": "qa", **_view_context(principal)}
    )


@router.get("/qa/scores", response_class=HTMLResponse, response_model=None)
def qa_scores_page(
    request: Request, principal: Principal | None = Depends(optional_principal)
) -> HTMLResponse | RedirectResponse:
    """상담 점수 목록."""
    if principal is None:
        return _redirect_to_login(request)
    return templates.TemplateResponse(
        request, "qa_scores.html", {"page": "qa_scores", **_view_context(principal)}
    )


@router.get("/qa/compliance", response_class=HTMLResponse, response_model=None)
def qa_compliance_page(
    request: Request, principal: Principal | None = Depends(optional_principal)
) -> HTMLResponse | RedirectResponse:
    """컴플라이언스 점수 목록. 위반이 있는 상담만 모아 본다."""
    if principal is None:
        return _redirect_to_login(request)
    return templates.TemplateResponse(
        request, "qa_compliance.html", {"page": "qa_compliance", **_view_context(principal)}
    )


@router.get("/admin/integration", response_class=HTMLResponse, response_model=None)
def integration_page(
    request: Request, principal: Principal | None = Depends(optional_principal)
) -> HTMLResponse | RedirectResponse:
    """연동 설정 화면 (시스템 관리 하위).

    값을 여기서 바꾸지는 않는다. 접속 주소·키는 환경변수로만 주입되며 (SEC-022),
    이 화면은 "지금 어디에 어떻게 붙어 있는가"를 보여주고 수집을 실행한다.
    """
    if principal is None:
        return _redirect_to_login(request)
    if not principal.has(Permission.ADMIN_MANAGE):
        return RedirectResponse(url="/", status_code=303)
    return templates.TemplateResponse(
        request, "integration.html", {"page": "integration", **_view_context(principal)}
    )


@router.get("/admin", response_class=HTMLResponse, response_model=None)
def admin_page(
    request: Request, principal: Principal | None = Depends(optional_principal)
) -> HTMLResponse | RedirectResponse:
    """관리자 화면. 일반 사용자 화면과 라우트를 분리한다 (Harness §47, FR-M-001).

    화면 접근을 막는 것은 편의일 뿐이며, 실제 권한 검증은 각 API 가 수행한다 (SEC-010).
    """
    if principal is None:
        return _redirect_to_login(request)
    if not (
        principal.has(Permission.ADMIN_MANAGE) or principal.has(Permission.AUDIT_READ)
    ):
        return RedirectResponse(url="/", status_code=303)
    return templates.TemplateResponse(
        request, "admin.html", {"page": "admin", **_view_context(principal)}
    )


def _view_context(principal: Principal) -> dict[str, object]:
    """템플릿에 넘길 최소 컨텍스트.

    권한 목록은 화면 구성용이다. 버튼을 숨기는 것은 권한 제어가 아니며 (Harness §10),
    서버가 같은 판단을 다시 한다.
    """
    return {
        "username": principal.username,
        "role": principal.role.value,
        "can_admin": principal.has(Permission.ADMIN_MANAGE),
        "can_audit": principal.has(Permission.AUDIT_READ),
        "can_upload": principal.has(Permission.JOB_CREATE),
        "can_download": principal.has(Permission.TRANSCRIPT_DOWNLOAD),
    }
