/* 관리 화면. 시스템 상태와 감사 로그를 보여준다 (FR-M-002 / FR-M-006). */

function statTile(label, value) {
  const tile = el("div", "stat");
  tile.appendChild(el("div", "stat-label", label));
  tile.appendChild(el("div", "stat-value", value));
  return tile;
}

async function loadStatus() {
  const grid = document.getElementById("status-grid");
  if (!grid) return;

  try {
    const status = await request("/admin/status");

    grid.replaceChildren();
    grid.appendChild(statTile("엔진", status.model.engine));
    grid.appendChild(statTile("모델", status.model.model_name));
    grid.appendChild(statTile("연산 방식", `${status.model.device_type} / ${status.model.compute_type}`));
    // 모델은 워커가 적재한다. API 프로세스의 적재 여부는 의미가 없으므로 워커 생존을 보여준다.
    grid.appendChild(
      statTile("워커", status.workers_online < 0 ? "조회 불가" : `${status.workers_online}대`),
    );
    // 큐 길이를 알 수 없으면 -1 이 온다. 숫자를 그대로 보여주면 오해를 부른다.
    grid.appendChild(
      statTile("큐 길이", status.queue_depth < 0 ? "조회 불가" : String(status.queue_depth)),
    );
    grid.appendChild(statTile("동시 실행 한도", String(status.limits.max_concurrent_jobs)));
    grid.appendChild(statTile("큐 상한", String(status.limits.queue_max_length)));
    grid.appendChild(statTile("작업 타임아웃(초)", String(status.limits.job_timeout_seconds)));

    const counts = document.getElementById("job-counts");
    counts.replaceChildren();
    for (const [name, value] of Object.entries(status.jobs)) {
      counts.appendChild(statTile(name, String(value)));
    }
  } catch (error) {
    notifyError(error);
  }
}

async function loadAudit() {
  const body = document.getElementById("audit-body");
  if (!body) return;

  const filter = document.getElementById("event-filter").value.trim();
  const params = new URLSearchParams({ limit: "50" });
  if (filter) params.set("event_type", filter);

  try {
    const payload = await request("/admin/audit?" + params.toString());
    body.replaceChildren();

    for (const row of payload.items) {
      const tr = document.createElement("tr");
      tr.appendChild(el("td", null, formatTimestamp(row.event_time)));
      tr.appendChild(el("td", null, row.event_type));
      tr.appendChild(el("td", null, `${row.actor_id} (${row.actor_role})`));
      tr.appendChild(el("td", null, row.action));
      tr.appendChild(el("td", null, row.result));
      tr.appendChild(el("td", null, row.target_id || row.job_id || "-"));
      body.appendChild(tr);
    }
    document.getElementById("audit-empty").hidden = payload.items.length > 0;
  } catch (error) {
    // 알 수 없는 이벤트 종류를 넣으면 검증 오류가 난다. 사용자에게 그대로 알린다.
    notifyError(error);
  }
}

async function verifyChain() {
  const result = document.getElementById("chain-result");
  try {
    const payload = await request("/admin/audit/integrity");
    result.textContent = payload.is_valid
      ? `무결성 확인: ${payload.checked_count}건 이상 없음`
      : `무결성 위반: id ${payload.first_broken_id} 부터 불일치`;
  } catch (error) {
    notifyError(error);
  }
}

document.addEventListener("DOMContentLoaded", () => {
  const root = document.getElementById("admin-root");

  if (root.dataset.canAdmin === "true") {
    loadStatus();
  }
  if (root.dataset.canAudit === "true") {
    document.getElementById("audit-refresh").addEventListener("click", loadAudit);
    document.getElementById("verify-chain").addEventListener("click", verifyChain);
    loadAudit();
  }
});

/* --- 런타임 설정 · 사용자 관리 (FR-M-003 / FR-M-004) ---------------------- */

const ANALYSIS_PROMPT_KEY = "llm_analysis_prompt";
const ROLES = ["USER", "REVIEWER", "ADMIN", "AUDITOR"];

async function loadPrompt() {
  const box = document.getElementById("prompt-text");
  if (!box) return;
  try {
    const payload = await request(`/admin/config/${ANALYSIS_PROMPT_KEY}`);
    box.value = payload.value;
  } catch (error) {
    notifyError(error);
  }
}

async function savePrompt() {
  const reason = document.getElementById("prompt-reason").value.trim();
  if (!reason) {
    notify("변경 사유를 입력해 주세요.", "error");
    return;
  }
  try {
    await request(`/admin/config/${ANALYSIS_PROMPT_KEY}`, {
      method: "PUT",
      json: { value: document.getElementById("prompt-text").value, reason },
    });
    notify("프롬프트를 저장했습니다. 다음 분석부터 반영됩니다.", "success");
    document.getElementById("prompt-reason").value = "";
    await loadConfigHistory();
  } catch (error) {
    notifyError(error);
  }
}

async function resetPrompt() {
  if (!window.confirm("프롬프트를 기본값으로 되돌릴까요?")) return;
  try {
    await request(`/admin/config/${ANALYSIS_PROMPT_KEY}`, { method: "DELETE" });
    notify("기본값으로 복원했습니다.", "success");
    await loadPrompt();
    await loadConfigHistory();
  } catch (error) {
    notifyError(error);
  }
}

async function loadUsers() {
  const body = document.getElementById("users-body");
  if (!body) return;
  try {
    const payload = await request("/admin/users");
    body.replaceChildren();
    for (const user of payload.items) {
      const row = document.createElement("tr");
      row.appendChild(el("td", null, user.username));

      const roleCell = el("td");
      const select = document.createElement("select");
      for (const role of ROLES) {
        const option = document.createElement("option");
        option.value = role;
        option.textContent = role;
        option.selected = role === user.role;
        select.appendChild(option);
      }
      roleCell.appendChild(select);
      row.appendChild(roleCell);

      row.appendChild(el("td", null, user.is_active ? "활성" : "비활성"));
      row.appendChild(el("td", null, user.auth_provider));

      const actionCell = el("td");
      const apply = el("button", null, "역할 변경");
      apply.addEventListener("click", () => changeRole(user.id, select.value));
      actionCell.appendChild(apply);
      row.appendChild(actionCell);

      body.appendChild(row);
    }
  } catch (error) {
    notifyError(error);
  }
}

async function changeRole(userId, role) {
  const reason = window.prompt("변경 사유를 입력해 주세요.");
  if (!reason) return;
  try {
    await request(`/admin/users/${encodeURIComponent(userId)}/role`, {
      method: "PUT",
      json: { role, reason },
    });
    notify("역할을 변경했습니다. 다음 요청부터 적용됩니다.", "success");
    await loadUsers();
  } catch (error) {
    notifyError(error);
  }
}

async function loadConfigHistory() {
  const body = document.getElementById("config-history-body");
  if (!body) return;
  try {
    const payload = await request("/admin/config/history/all?limit=20");
    body.replaceChildren();
    for (const row of payload.items) {
      const tr = document.createElement("tr");
      tr.appendChild(el("td", null, formatTimestamp(row.changed_at)));
      tr.appendChild(el("td", null, row.config_key));
      tr.appendChild(el("td", null, row.changed_by));
      tr.appendChild(el("td", null, row.reason));
      body.appendChild(tr);
    }
  } catch (error) {
    notifyError(error);
  }
}

document.addEventListener("DOMContentLoaded", () => {
  const root = document.getElementById("admin-root");
  if (root.dataset.canAdmin !== "true") return;

  document.getElementById("prompt-save").addEventListener("click", savePrompt);
  document.getElementById("prompt-reset").addEventListener("click", resetPrompt);
  loadPrompt();
  loadUsers();
  loadConfigHistory();
});
