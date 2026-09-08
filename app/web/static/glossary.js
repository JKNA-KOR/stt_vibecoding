/* 용어사전 화면.
 *
 * 용어는 관리자가 넣은 값이지만 화면에서는 다른 사용자 입력과 똑같이 다룬다 —
 * 예외 없이 textContent 다 (Harness §13). 편집 도구를 숨기는 것은 권한 제어가 아니며
 * (§10), 실제 판단은 API 가 다시 한다.
 */

const glossary = {
  canAdmin: false,
  search: "",
  category: "",
  terms: [],
};

function renderTerms(payload) {
  const body = document.getElementById("glossary-body");
  const empty = document.getElementById("glossary-empty");
  body.replaceChildren();
  glossary.terms = payload.items;

  for (const term of payload.items) {
    const row = document.createElement("tr");
    row.appendChild(el("td", null, term.term));
    row.appendChild(el("td", null, term.category || "-"));
    row.appendChild(el("td", null, term.definition || "-"));
    row.appendChild(el("td", null, term.aliases.length ? term.aliases.join(", ") : "-"));
    row.appendChild(el("td", "numeric", String(term.priority)));

    const stateCell = el("td");
    stateCell.appendChild(
      term.is_active
        ? el("span", "status status-COMPLETED", "사용")
        : el("span", "status status-CANCELLED", "중지"),
    );
    row.appendChild(stateCell);

    if (glossary.canAdmin) {
      const actionCell = el("td");
      const toggle = el("button", null, term.is_active ? "중지" : "사용");
      toggle.addEventListener("click", () => setActive(term, !term.is_active));
      actionCell.appendChild(toggle);

      const remove = el("button", "danger", "삭제");
      remove.addEventListener("click", () => removeTerm(term));
      actionCell.appendChild(remove);
      row.appendChild(actionCell);
    }

    body.appendChild(row);
  }

  empty.hidden = payload.items.length > 0;
  document.getElementById("glossary-count").textContent = `${payload.page.total}건`;
  renderCategories(payload.categories);
}

function renderCategories(categories) {
  const select = document.getElementById("glossary-category");
  const current = select.value;
  select.replaceChildren();

  const all = document.createElement("option");
  all.value = "";
  all.textContent = "전체 분류";
  select.appendChild(all);

  for (const name of categories) {
    const option = document.createElement("option");
    option.value = name;
    option.textContent = name;
    select.appendChild(option);
  }
  select.value = current;
}

async function loadTerms() {
  const params = new URLSearchParams({ limit: "200" });
  if (glossary.search) params.set("search", glossary.search);
  if (glossary.category) params.set("category", glossary.category);
  try {
    renderTerms(await request("/glossary?" + params.toString()));
  } catch (error) {
    notifyError(error);
  }
}

/** 엔진에 실제로 넘어가는 힌트를 그대로 보여준다. 잘렸으면 잘렸다고 말한다. */
async function loadHint() {
  try {
    const hint = await request("/glossary/hint");
    document.getElementById("hint-text").value = hint.hint;
    document.getElementById("hint-included").textContent = `${hint.included_count}개`;
    document.getElementById("hint-active").textContent = `${hint.active_count}개`;

    const warning = document.getElementById("hint-warning");
    const dropped = hint.active_count - hint.included_count;
    if (dropped > 0) {
      warning.textContent =
        `길이 상한(${hint.max_chars}자) 때문에 ${dropped}개 용어가 힌트에서 빠졌습니다. ` +
        `중요한 용어의 우선순위를 올리세요. 뜻은 분석·QA 에는 그대로 쓰입니다.`;
      warning.hidden = false;
    } else {
      warning.hidden = true;
    }
  } catch (error) {
    notifyError(error);
  }
}

async function addTerm(event) {
  event.preventDefault();
  const submit = document.getElementById("term-submit");
  const aliases = document
    .getElementById("term-aliases")
    .value.split(",")
    .map((item) => item.trim())
    .filter(Boolean);

  const priority = Number.parseInt(document.getElementById("term-priority").value, 10);

  submit.disabled = true;
  try {
    await request("/glossary", {
      method: "POST",
      json: {
        term: document.getElementById("term-name").value.trim(),
        category: document.getElementById("term-category").value.trim(),
        definition: document.getElementById("term-definition").value.trim(),
        aliases,
        priority: Number.isFinite(priority) ? priority : 0,
        is_active: true,
      },
    });
    notify("용어를 추가했습니다. 다음 전사부터 반영됩니다.", "success");
    document.getElementById("glossary-form").reset();
    document.getElementById("term-priority").value = "50";
    await loadTerms();
    await loadHint();
  } catch (error) {
    notifyError(error);
  } finally {
    submit.disabled = false;
  }
}

async function setActive(term, isActive) {
  try {
    await request(`/glossary/${encodeURIComponent(term.id)}`, {
      method: "PUT",
      json: { is_active: isActive },
    });
    await loadTerms();
    await loadHint();
  } catch (error) {
    notifyError(error);
  }
}

async function removeTerm(term) {
  // 되돌릴 수 없는 삭제는 확인을 받는다 (Harness §48).
  if (!window.confirm(`'${term.term}' 을(를) 삭제할까요? 되돌릴 수 없습니다.`)) return;
  try {
    await request(`/glossary/${encodeURIComponent(term.id)}`, { method: "DELETE" });
    notify("용어를 삭제했습니다.", "success");
    await loadTerms();
    await loadHint();
  } catch (error) {
    notifyError(error);
  }
}

async function loadSeed() {
  try {
    const result = await request("/glossary/seed", { method: "POST" });
    notify(
      result.added > 0
        ? `기본 용어 ${result.added}개를 추가했습니다.`
        : "이미 모든 기본 용어가 등록되어 있습니다.",
      "success",
    );
    await loadTerms();
    await loadHint();
  } catch (error) {
    notifyError(error);
  }
}

document.addEventListener("DOMContentLoaded", () => {
  const root = document.getElementById("glossary-root");
  glossary.canAdmin = root.dataset.canAdmin === "true";

  document.getElementById("glossary-refresh").addEventListener("click", () => {
    loadTerms();
    loadHint();
  });

  const search = document.getElementById("glossary-search");
  search.addEventListener("input", () => {
    glossary.search = search.value;
    loadTerms();
  });

  const category = document.getElementById("glossary-category");
  category.addEventListener("change", () => {
    glossary.category = category.value;
    loadTerms();
  });

  if (glossary.canAdmin) {
    document.getElementById("glossary-form").addEventListener("submit", addTerm);
    document.getElementById("glossary-seed").addEventListener("click", loadSeed);
  }

  loadTerms();
  loadHint();
});
