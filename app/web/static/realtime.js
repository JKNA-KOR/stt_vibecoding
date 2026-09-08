/* 실시간 전사 화면.
 *
 * 마이크 → AudioWorklet(PCM 변환) → WebSocket → 서버 조각 전사 → 화면.
 *
 * MediaRecorder 대신 원시 PCM 을 보내는 이유는 `pcm-worklet.js` 주석에 적어 두었다.
 * 전사 결과는 Untrusted Data 이므로 예외 없이 textContent 로 넣는다 (Harness §13).
 */

const rt = {
  socket: null,
  context: null,
  stream: null,
  worklet: null,
  analyser: null,
  levelTimer: null,
  clockTimer: null,
  startedAt: 0,
  segments: [],
  stopping: false,
};

/** WebSocket 종료 코드 → 사용자에게 보여줄 이유. 서버의 상수와 짝을 이룬다. */
const RT_CLOSE_REASONS = {
  4400: "음성 데이터 형식이 올바르지 않습니다.",
  4401: "세션이 만료되었습니다. 다시 로그인해 주세요.",
  4403: "허용되지 않은 접근입니다.",
  4404: "실시간 전사가 꺼져 있습니다.",
  4408: "세션 최대 길이에 도달해 종료되었습니다.",
  4429: "동시 사용 한도에 도달했습니다. 잠시 후 다시 시도해 주세요.",
  4500: "전사 중 오류가 발생했습니다.",
};

function setState(label, kind) {
  const box = document.getElementById("rt-state");
  box.textContent = label;
  box.className = "status" + (kind ? " status-" + kind : "");
}

function setRunning(running) {
  document.getElementById("rt-start").disabled = running;
  document.getElementById("rt-stop").disabled = !running;
}

function formatClock(seconds) {
  const total = Math.floor(seconds);
  const mm = String(Math.floor(total / 60)).padStart(2, "0");
  const ss = String(total % 60).padStart(2, "0");
  return `${mm}:${ss}`;
}

function appendSegment(payload) {
  rt.segments.push(payload);

  const list = document.getElementById("rt-segments");
  const item = document.createElement("li");
  item.appendChild(
    el("span", "segment-time", `${formatClock(payload.start)} – ${formatClock(payload.end)}`),
  );
  // 본문은 반드시 textContent 로만 넣는다.
  item.appendChild(el("span", "segment-text", payload.text || "(인식된 발화 없음)"));
  list.appendChild(item);
  // 새 조각이 화면 밖으로 밀리면 실시간으로 보는 의미가 없다.
  item.scrollIntoView({ block: "nearest" });

  document.getElementById("rt-placeholder").hidden = true;
  document.getElementById("rt-count").textContent = `${rt.segments.length}개 조각`;
  document.getElementById("rt-download").disabled = false;
  document.getElementById("rt-clear").disabled = false;
}

/** 입력 레벨 표시. 마이크가 실제로 잡히는지 눈으로 확인할 수 있어야 한다. */
function startLevelMeter() {
  const data = new Uint8Array(rt.analyser.frequencyBinCount);
  rt.levelTimer = window.setInterval(() => {
    rt.analyser.getByteTimeDomainData(data);
    let peak = 0;
    for (const value of data) peak = Math.max(peak, Math.abs(value - 128));
    // 폭은 10% 단위 클래스로만 준다. CSP 가 인라인 style 을 막는다.
    const step = Math.min(100, Math.round((peak / 128) * 10) * 10);
    document.getElementById("rt-level").className = "level-bar w" + step;
  }, 120);
}

function startClock() {
  rt.startedAt = Date.now();
  rt.clockTimer = window.setInterval(() => {
    document.getElementById("rt-timer").textContent =
      formatClock((Date.now() - rt.startedAt) / 1000);
  }, 500);
}

function socketUrl() {
  // 페이지와 같은 출처로만 연결한다. 서버도 Origin 을 확인한다.
  const scheme = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${scheme}//${window.location.host}/api/v1/realtime/stream`;
}

async function startRealtime() {
  clearNotice();
  setRunning(true);
  setState("연결 중", "QUEUED");

  try {
    rt.stream = await navigator.mediaDevices.getUserMedia({
      audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true },
    });
  } catch (error) {
    // 권한 거부와 장치 없음은 사용자가 할 수 있는 일이 다르다.
    const message =
      error && error.name === "NotAllowedError"
        ? "마이크 권한이 거부되었습니다. 브라우저 주소창의 권한 설정을 확인해 주세요."
        : "마이크를 열 수 없습니다. 장치가 연결되어 있는지 확인해 주세요.";
    notify(message, "error");
    await stopRealtime(false);
    return;
  }

  try {
    // 16kHz 를 요청한다. 브라우저가 거절하면 실제 값을 그대로 서버에 알린다.
    rt.context = new AudioContext({ sampleRate: 16000 });
    await rt.context.audioWorklet.addModule("/static/pcm-worklet.js");

    const source = rt.context.createMediaStreamSource(rt.stream);
    rt.analyser = rt.context.createAnalyser();
    rt.analyser.fftSize = 512;
    source.connect(rt.analyser);

    rt.worklet = new AudioWorkletNode(rt.context, "pcm-worklet");
    source.connect(rt.worklet);
    // 목적지에 연결하지 않는다. 연결하면 자기 목소리가 스피커로 되돌아 나온다.
  } catch {
    notify("오디오 처리를 시작하지 못했습니다.", "error");
    await stopRealtime(false);
    return;
  }

  rt.socket = new WebSocket(socketUrl());
  rt.socket.binaryType = "arraybuffer";

  rt.socket.addEventListener("open", () => {
    rt.socket.send(JSON.stringify({ sample_rate: rt.context.sampleRate }));
  });

  rt.socket.addEventListener("message", (event) => {
    let payload = null;
    try {
      payload = JSON.parse(event.data);
    } catch {
      return;
    }

    if (payload.type === "ready") {
      setState("녹음 중", "PROCESSING");
      document.getElementById("rt-hint").textContent =
        `약 ${payload.segment_seconds}초 단위로 전사됩니다. 최대 ${Math.floor(payload.max_session_seconds / 60)}분까지 이어집니다.`;
      // 준비되기 전에 보낸 오디오는 서버가 샘플레이트를 모른다. 여기서부터 흘려보낸다.
      rt.worklet.port.onmessage = (message) => {
        if (rt.socket && rt.socket.readyState === WebSocket.OPEN) {
          rt.socket.send(message.data.buffer);
        }
      };
      startLevelMeter();
      startClock();
    } else if (payload.type === "segment") {
      appendSegment(payload);
    } else if (payload.type === "done") {
      setState("완료", "COMPLETED");
    } else if (payload.type === "error") {
      notify(payload.message || "전사 중 오류가 발생했습니다.", "error");
    }
  });

  rt.socket.addEventListener("close", (event) => {
    const reason = RT_CLOSE_REASONS[event.code];
    if (reason) notify(reason, "error");
    stopRealtime(false);
  });

  rt.socket.addEventListener("error", () => {
    // close 가 이어서 오므로 여기서는 상태만 남긴다.
    setState("연결 오류", "FAILED");
  });
}

async function stopRealtime(sendStop = true) {
  if (rt.stopping) return;
  rt.stopping = true;

  if (sendStop && rt.socket && rt.socket.readyState === WebSocket.OPEN) {
    // 남은 버퍼를 마지막 조각으로 전사해 달라고 알린 뒤, 서버가 닫기를 기다린다.
    setState("마무리 중", "QUEUED");
    rt.socket.send(JSON.stringify({ type: "stop" }));
  }

  if (rt.worklet) {
    rt.worklet.port.onmessage = null;
    rt.worklet.disconnect();
    rt.worklet = null;
  }
  if (rt.stream) {
    for (const track of rt.stream.getTracks()) track.stop();
    rt.stream = null;
  }
  if (rt.context) {
    try {
      await rt.context.close();
    } catch {
      // 이미 닫힌 컨텍스트다.
    }
    rt.context = null;
  }
  if (rt.levelTimer !== null) {
    window.clearInterval(rt.levelTimer);
    rt.levelTimer = null;
  }
  if (rt.clockTimer !== null) {
    window.clearInterval(rt.clockTimer);
    rt.clockTimer = null;
  }

  document.getElementById("rt-level").className = "level-bar w0";
  setRunning(false);
  if (document.getElementById("rt-state").textContent === "녹음 중") setState("대기");
  rt.stopping = false;
}

/** 전사 결과를 텍스트 파일로 내려받는다. 서버에 남지 않으므로 여기서 만든다. */
function downloadTranscript() {
  const lines = rt.segments.map(
    (segment) => `[${formatClock(segment.start)} - ${formatClock(segment.end)}] ${segment.text}`,
  );
  const blob = new Blob([lines.join("\n")], { type: "text/plain;charset=utf-8" });
  const url = URL.createObjectURL(blob);

  const link = document.createElement("a");
  link.href = url;
  link.download = `realtime-${new Date().toISOString().slice(0, 19).replace(/[:T]/g, "")}.txt`;
  link.click();
  URL.revokeObjectURL(url);
}

function clearTranscript() {
  rt.segments = [];
  document.getElementById("rt-segments").replaceChildren();
  document.getElementById("rt-placeholder").hidden = false;
  document.getElementById("rt-count").textContent = "";
  document.getElementById("rt-download").disabled = true;
  document.getElementById("rt-clear").disabled = true;
}

document.addEventListener("DOMContentLoaded", () => {
  const root = document.getElementById("realtime-root");
  if (!root || root.dataset.enabled !== "true") return;

  document.getElementById("rt-start").addEventListener("click", startRealtime);
  document.getElementById("rt-stop").addEventListener("click", () => stopRealtime(true));
  document.getElementById("rt-download").addEventListener("click", downloadTranscript);
  document.getElementById("rt-clear").addEventListener("click", clearTranscript);

  // 탭을 닫거나 다른 화면으로 가면 마이크를 놓아준다. 켜진 채로 남기지 않는다.
  window.addEventListener("pagehide", () => stopRealtime(false));
});
