/* 화면 공통 유틸.
 *
 * 원칙 두 가지가 이 파일 전체를 지배한다.
 *
 *   1. 서버에서 온 문자열(파일명, Transcript, 감사 로그)은 절대 innerHTML 로 넣지 않는다.
 *      전부 textContent 다. Transcript 는 Untrusted Data 이며 (Harness §13, FR-T-005),
 *      CSP 가 인라인 스크립트를 막더라도 DOM 주입 자체를 만들지 않는 편이 낫다.
 *   2. 상태 변경 요청에는 CSRF 토큰을 실어 보낸다 (SEC-035). 토큰은 쿠키에서 읽는다.
 */

const API = "/api/v1";
const CSRF_COOKIE = "stt_csrf";
const CSRF_HEADER = "X-CSRF-Token";

function readCookie(name) {
  const prefix = name + "=";
  for (const part of document.cookie.split(";")) {
    const trimmed = part.trim();
    if (trimmed.startsWith(prefix)) {
      return decodeURIComponent(trimmed.slice(prefix.length));
    }
  }
  return null;
}

/** 서버가 준 표준 오류 형식 `{code, message, request_id}` 를 담는다 (SEC-032). */
class ApiError extends Error {
  constructor(status, body) {
    const detail = body && body.error ? body.error : {};
    super(detail.message || "요청을 처리하지 못했습니다.");
    this.status = status;
    this.code = detail.code || "UNKNOWN";
    this.requestId = detail.request_id || "";
  }
}

/**
 * API 호출.
 *
 * `redirectOnUnauthorized` 가 기본 true 인 이유는, 세션 만료로 401 을 받은 사용자에게
 * 오류 상자를 보여주는 것보다 로그인 화면으로 보내는 편이 낫기 때문이다. 로그인 요청
 * 자체는 401 이 "자격증명 불일치"라는 정상적인 결과이므로 false 를 넘겨야 한다.
 */
async function request(path, options = {}) {
  const { redirectOnUnauthorized = true, ...rest } = options;
  const init = { credentials: "same-origin", headers: {}, ...rest };

  if (init.method && init.method !== "GET") {
    const token = readCookie(CSRF_COOKIE);
    if (token) {
      init.headers[CSRF_HEADER] = token;
    }
  }
  if (init.json !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(init.json);
    delete init.json;
  }

  const response = await fetch(API + path, init);

  if (response.status === 401 && redirectOnUnauthorized) {
    // 세션이 끊겼다. 화면에 오류를 띄우는 것보다 다시 로그인시키는 편이 낫다.
    window.location.href = "/login";
    throw new ApiError(401, null);
  }
  if (response.status === 204) {
    return null;
  }

  let body = null;
  try {
    body = await response.json();
  } catch {
    body = null;
  }
  if (!response.ok) {
    throw new ApiError(response.status, body);
  }
  return body;
}

/** 알림 표시. 메시지는 항상 textContent 로 넣는다. */
function notify(message, kind = "info") {
  const box = document.getElementById("notice");
  if (!box) return;
  box.textContent = message;
  box.className = "notice" + (kind === "info" ? "" : " notice-" + kind);
  box.hidden = false;
  if (kind === "success") {
    window.setTimeout(() => { box.hidden = true; }, 4000);
  }
}

function clearNotice() {
  const box = document.getElementById("notice");
  if (box) box.hidden = true;
}

/** 오류를 사용자에게 보여준다. request_id 를 함께 노출해 문의 시 추적 가능하게 한다. */
function notifyError(error) {
  if (error instanceof ApiError) {
    const suffix = error.requestId ? ` (요청 ID: ${error.requestId})` : "";
    notify(error.message + suffix, "error");
  } else {
    notify("네트워크 오류가 발생했습니다.", "error");
  }
}

/** 텍스트 노드를 가진 요소를 만든다. innerHTML 을 쓰지 않기 위한 헬퍼. */
function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

function formatDuration(seconds) {
  if (seconds === null || seconds === undefined) return "-";
  const total = Math.round(seconds);
  const mm = String(Math.floor(total / 60)).padStart(2, "0");
  const ss = String(total % 60).padStart(2, "0");
  return `${mm}:${ss}`;
}

function formatTimestamp(value) {
  if (!value) return "-";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "-";
  return date.toLocaleString("ko-KR", { hour12: false });
}

function formatNumber(value, digits = 2) {
  if (value === null || value === undefined) return "-";
  return Number(value).toFixed(digits);
}

function formatBytes(value) {
  if (!value) return "-";
  const units = ["B", "KB", "MB", "GB"];
  let size = Number(value);
  let unit = 0;
  while (size >= 1024 && unit < units.length - 1) {
    size /= 1024;
    unit += 1;
  }
  return `${size.toFixed(unit === 0 ? 0 : 1)} ${units[unit]}`;
}

/** 진행률을 10% 단위 클래스로 표현한다 (CSP 가 인라인 style 을 막는다). */
function setProgress(bar, ratio) {
  const step = Math.min(100, Math.max(0, Math.round(ratio * 10) * 10));
  bar.className = "progress-bar p" + step;
}

/* --- 삭제 확인 팝업 (공용) --------------------------------------------------
 *
 * 되돌릴 수 없는 동작이므로 **무엇이 사라지는지 이름으로 보여준 뒤** 확인을 받는다
 * (Harness §48). 화면마다 따로 만들면 한쪽만 고쳐져 경고 문구가 갈라지므로 여기 하나만
 * 둔다. 팝업 자체는 관리자에게만 렌더되므로 없으면 조용히 아무 일도 하지 않는다.
 */

const deleteModal = { onConfirm: null };

/**
 * 삭제 확인을 받는다.
 *
 * @param {Array<{id: string, name: string, meta?: string}>} targets 지울 대상
 * @param {Function} onConfirm 확인을 누르면 호출된다. 실패는 호출부가 알린다.
 */
function askDeleteConfirmation(targets, onConfirm) {
  const modal = document.getElementById("delete-modal");
  if (!modal || targets.length === 0) return;

  const list = document.getElementById("delete-modal-list");
  list.replaceChildren();
  for (const target of targets) {
    const row = el("li");
    // 이름은 사용자가 올린 파일명이다. textContent 로만 넣는다 (Harness §13).
    row.appendChild(el("span", "delete-target-name", target.name));
    if (target.meta) row.appendChild(el("span", "delete-target-meta", target.meta));
    list.appendChild(row);
  }

  document.getElementById("delete-modal-count").textContent =
    `선택한 상담 ${targets.length}건을 삭제합니다.`;
  deleteModal.onConfirm = onConfirm;
  modal.hidden = false;
  document.getElementById("delete-modal-cancel").focus();
}

function closeDeleteModal() {
  const modal = document.getElementById("delete-modal");
  if (!modal) return;
  modal.hidden = true;
  deleteModal.onConfirm = null;
}

/** 여러 상담을 지운다. 부분 실패를 그대로 알린다 (Harness §4.3). */
async function deleteJobs(jobIds) {
  const result = await request("/jobs/bulk-delete", {
    method: "POST",
    json: { job_ids: jobIds },
  });

  if (result.failed.length === 0) {
    notify(`${result.deleted.length}건을 삭제했습니다. 감사 로그에 기록되었습니다.`, "success");
  } else {
    // 실패 이유를 그대로 보여준다. 숨기면 사용자가 다음 행동을 정할 수 없다.
    notify(
      `${result.deleted.length}건 삭제, ${result.failed.length}건 실패: ` +
        result.failed[0].message,
      "error",
    );
  }
  return result;
}

/** 화자 라벨의 표시 색. 서버가 주는 값은 "상담원" / "고객" 둘뿐이다. */
function speakerClass(label) {
  if (label === "상담원") return "agent";
  if (label === "고객") return "customer";
  return "unknown";
}

/* --- 라이브 캡션 재생 -------------------------------------------------------
 *
 * 전사 결과를 한 줄씩 드러낸다. **이것은 표시 효과이지 스트리밍이 아니다.** 업로드
 * 경로는 파일 전체를 한 번에 전사하므로 중간 결과가 존재하지 않는다 — 진짜 실시간
 * 자막은 실시간 전사 화면(`/realtime`)의 몫이다. 이름으로 둘을 헷갈리게 하지 않으려고
 * 여기 적어 둔다 (Harness §4.3).
 *
 * 긴 녹취를 실제 발화 속도로 재생하면 끝까지 보는 데 통화 시간만큼 걸린다. 그래서
 * 고정 간격으로 빠르게 훑는다.
 */

const CAPTION_STEP_MS = 140;
// 조각이 많으면 재생만 몇 분이 걸린다. 그 이상은 효과 없이 바로 보여준다.
const CAPTION_MAX_ITEMS = 120;

const captionState = { timer: null, list: null };

/** 목록의 자식들을 한 줄씩 드러낸다. 이미 재생 중이면 먼저 멈춘다. */
function playCaptions(list, { onDone } = {}) {
  stopCaptions();
  const items = Array.from(list.children);
  if (items.length === 0 || items.length > CAPTION_MAX_ITEMS) {
    revealAll(list);
    if (onDone) onDone();
    return;
  }

  captionState.list = list;
  for (const item of items) item.classList.add("caption-hidden");

  let index = 0;
  captionState.timer = window.setInterval(() => {
    if (index >= items.length) {
      stopCaptions();
      if (onDone) onDone();
      return;
    }
    const item = items[index];
    item.classList.remove("caption-hidden");
    item.classList.add("caption-in");
    item.scrollIntoView({ block: "nearest" });
    index += 1;
  }, CAPTION_STEP_MS);
}

/** 재생을 멈추고 남은 줄을 모두 드러낸다. 중간에 끊겨 사라진 줄이 없어야 한다. */
function stopCaptions() {
  if (captionState.timer !== null) {
    window.clearInterval(captionState.timer);
    captionState.timer = null;
  }
  if (captionState.list) {
    revealAll(captionState.list);
    captionState.list = null;
  }
}

function revealAll(list) {
  for (const item of list.children) item.classList.remove("caption-hidden");
}

/** 사용자가 애니메이션을 원치 않으면 효과를 쓰지 않는다 (접근성). */
function captionsAllowed() {
  return !window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

document.addEventListener("DOMContentLoaded", () => {
  // 좁은 화면에서는 사이드바가 접혀 있다. 넓은 화면에서는 토글 버튼 자체가 보이지 않는다.
  const sidebar = document.getElementById("sidebar");
  const toggle = document.getElementById("sidebar-toggle");
  if (sidebar && toggle) {
    toggle.addEventListener("click", () => sidebar.classList.toggle("open"));
    // 메뉴를 고르면 화면이 바뀐다. 열린 채로 두면 내용을 가린다.
    for (const link of sidebar.querySelectorAll(".nav-item")) {
      link.addEventListener("click", () => sidebar.classList.remove("open"));
    }
  }

  const modal = document.getElementById("delete-modal");
  if (modal) {
    document.getElementById("delete-modal-cancel").addEventListener("click", closeDeleteModal);
    document.getElementById("delete-modal-confirm").addEventListener("click", async () => {
      const button = document.getElementById("delete-modal-confirm");
      const handler = deleteModal.onConfirm;
      if (!handler) return;

      button.disabled = true;
      try {
        await handler();
      } finally {
        button.disabled = false;
        closeDeleteModal();
      }
    });
    // 배경을 눌러도 닫힌다. 확인 버튼은 배경 클릭으로 눌리지 않는다.
    modal.addEventListener("click", (event) => {
      if (event.target.id === "delete-modal") closeDeleteModal();
    });
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape") closeDeleteModal();
    });
  }

  // 로그아웃 버튼이 두 곳(사이드바 하단, 상단바)에 있다. 같은 동작을 붙인다.
  for (const id of ["logout-button", "logout-top"]) {
    const logout = document.getElementById(id);
    if (!logout) continue;
    logout.addEventListener("click", async () => {
      try {
        await request("/auth/logout", { method: "POST" });
      } catch (error) {
        // 로그아웃 실패는 사용자가 할 수 있는 일이 없다. 쿠키는 서버가 지운다.
        notifyError(error);
      }
      window.location.href = "/login";
    });
  }
});
