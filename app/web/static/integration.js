/* 연동 인터페이스 화면.
 *
 * 값을 바꾸지 않는다. 접속 정보는 환경변수로만 주입되므로 (SEC-022) 이 화면은
 * "지금 어디에 어떻게 붙어 있는가"를 보여주고 수집을 실행할 뿐이다.
 */

function connBadge(id, state, label) {
  const box = document.getElementById(id);
  box.className = "conn conn-" + state;
  box.textContent = label;
}

function facts(id, rows) {
  const dl = document.getElementById(id);
  dl.replaceChildren();
  for (const [label, value] of rows) {
    dl.appendChild(el("dt", null, label));
    dl.appendChild(el("dd", null, value));
  }
}

/** 키는 값이 아니라 "설정됨/없음"으로만 보여준다 (Harness §44). */
function keyText(present) {
  return present ? "설정됨" : "설정되지 않음";
}

function renderInbound(inbound) {
  const status = inbound.status || {};
  if (inbound.source === "off") {
    connBadge("inbound-state", "off", "꺼짐");
  } else if (status.reachable) {
    connBadge("inbound-state", "ok", "연결됨");
  } else {
    connBadge("inbound-state", "bad", "연결 실패");
  }

  const rows = [
    ["수집 방식", inbound.source === "off" ? "사용 안 함" : inbound.source],
    ["수집 계정", inbound.ingest_username || "(지정되지 않음)"],
    ["1회 처리 한도", `${inbound.batch_limit}건`],
  ];
  if (inbound.source === "folder") {
    rows.push(["수집 폴더", inbound.inbox_dir]);
    rows.push(["대기 중", `${status.pending ?? 0}건`]);
  } else if (inbound.source === "http") {
    rows.push(["녹취서버 주소", inbound.api_base_url || "-"]);
    rows.push(["API 키", keyText(inbound.has_api_key)]);
  }
  if (inbound.source !== "off") {
    rows.push(["수집 계정 확인", status.user_exists ? "존재함" : "찾을 수 없음"]);
  }
  facts("inbound-facts", rows);

  const detail = document.getElementById("inbound-detail");
  detail.textContent = status.detail || "";
  detail.hidden = !status.detail;

  document.getElementById("ingest-run").disabled = inbound.source === "off";
}

function renderOutbound(outbound) {
  connBadge(
    "outbound-state",
    outbound.enabled ? "ok" : "off",
    outbound.enabled ? "켜짐" : "꺼짐",
  );
  facts("outbound-facts", [
    ["송신", outbound.enabled ? "사용" : "사용 안 함"],
    ["수신 주소", outbound.url || "-"],
    ["API 키", keyText(outbound.has_api_key)],
    ["재시도 횟수", `${outbound.max_attempts}회`],
    [
      "실어 보내는 것",
      [
        outbound.include_transcript ? "전사" : null,
        outbound.include_analysis ? "분석" : null,
        outbound.include_qa ? "QA" : null,
      ]
        .filter(Boolean)
        .join(", ") || "작업 정보만",
    ],
    ["외부 전송 승인", outbound.allow_external ? "승인됨" : "사내 주소만 허용"],
  ]);
}

function renderEngine(engine) {
  const external = engine.stt_engine !== "faster-whisper" && engine.stt_engine !== "mock";
  connBadge("engine-state", external ? "ok" : "off", external ? "외부 엔진" : "로컬 엔진");
  const rows = [
    ["엔진", engine.stt_engine],
    ["모델", engine.model_name],
  ];
  if (external) {
    rows.push(["엔드포인트", engine.api_base_url || "-"]);
    rows.push(["API 키", keyText(engine.has_api_key)]);
    rows.push(["음성 외부 전송 승인", engine.allow_external ? "승인됨" : "미승인"]);
  }
  facts("engine-facts", rows);
}

async function loadIntegration() {
  try {
    const status = await request("/admin/integration/status");
    renderInbound(status.inbound);
    renderOutbound(status.outbound);
    renderEngine(status.engine);
  } catch (error) {
    notifyError(error);
  }
}

/** 수집 결과를 건별 분류로 보여준다. 실패와 중복은 다른 이야기다 (Harness §4.3). */
function renderIngestResult(result) {
  const box = document.getElementById("ingest-result");
  box.replaceChildren();

  const grid = el("div", "status-grid");
  for (const [label, value] of [
    ["가져옴", result.fetched],
    ["새로 접수", result.created],
    ["이미 처리됨", result.duplicated],
    ["실패", result.failed],
  ]) {
    const tile = el("div", "stat");
    tile.appendChild(el("div", "stat-label", label));
    tile.appendChild(el("div", "stat-value", String(value)));
    grid.appendChild(tile);
  }
  box.appendChild(grid);

  if (result.errors && result.errors.length > 0) {
    const list = el("ul", "inline-list");
    for (const message of result.errors) list.appendChild(el("li", null, message));
    box.appendChild(list);
  }
}

async function runIngest() {
  const button = document.getElementById("ingest-run");
  button.disabled = true;
  try {
    const result = await request("/admin/integration/ingest", { method: "POST" });
    renderIngestResult(result);
    notify(
      `수집 완료 — 새로 접수 ${result.created}건, 이미 처리됨 ${result.duplicated}건, 실패 ${result.failed}건`,
      result.failed > 0 ? "error" : "success",
    );
    await loadIntegration();
  } catch (error) {
    notifyError(error);
  } finally {
    button.disabled = false;
  }
}

document.addEventListener("DOMContentLoaded", () => {
  if (!document.getElementById("inbound-facts")) return;
  document.getElementById("integration-refresh").addEventListener("click", loadIntegration);
  document.getElementById("ingest-run").addEventListener("click", runIngest);
  loadIntegration();
});
