/* KR Public Data Hub 화면 동작. 서버(/api/*)만 부르고, 인증키는 화면에 오지 않는다. */
(() => {
  "use strict";

  const $ = (s, el = document) => el.querySelector(s);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const fmtInt = (n) => Number(n).toLocaleString("ko-KR");
  const CYCLE_LABEL = { A: "연", S: "반기", Q: "분기", M: "월", SM: "반월", D: "일", IR: "부정기", F: "다년" };
  const CYCLE_PREF = ["M", "Q", "A", "S", "D", "SM", "IR", "F"];
  const SRC_ORG = { KOSIS: "KOSIS", ECOS: "ECOS", DATAGO: "공공데이터포털" };
  const DG_KIND = { FILE: "파일데이터", API: "오픈API", STD: "표준데이터" };
  const HINT = {
    auth: "API 키를 확인하세요. README의 '시작하기'를 보세요.",
    no_data: "기간을 넓히거나 다른 항목·주기를 골라 보세요.",
    not_found: "통계표 코드·ID나 회사 고유번호를 확인하세요. 원천에서 없어졌거나 바뀌었을 수 있습니다.",
    bad_period: "시점 형식을 확인하세요. 월 2020-01, 분기 2020-Q1, 연 2020, 일 2020-01-01. 없는 날짜(2월 30일 등)는 안 됩니다.",
    bad_request: "주기·기간 형식이나 항목 선택이 이 통계표와 맞는지 확인하세요.",
    too_large: "항목·분류 선택이나 기간을 줄여 주세요.",
    rate_limit: "기관 호출 한도에 걸렸습니다. KOSIS는 1분, ECOS는 30분 뒤, OpenDART는 다음 날 다시 해 보세요.",
    server: "원천 기관 서버 문제입니다. 잠시 뒤 다시 해 보세요.",
    network: "인터넷 연결이나 네트워크 차단을 확인하세요.",
  };
  const PALETTE = ["#2B6CF0", "#0F9D8A", "#5B4BDB", "#D9822B", "#C2416B", "#64748B"];

  const st = {
    source: "", cycle: "", syn: true, recent: false,
    results: [], hit: null, detail: null, spec: null, sel: {}, preview: null, previewErr: null,
    cart: loadCart(), dockOpen: false, status: null, lastQuery: "",
    mode: "stat", corps: [], corp: null, company: null, fs: null, dlist: null, dtab: null, dunit: "억원", listedOnly: false,
    dec: { type: "", act: "", y1: "", y2: "", sort: "date" }, decRes: null, decList: [], decSel: null, decDoc: null, decPage: null,
    decAi: false, decAiRes: null,
  };

  // ------------------------------------------------------------ 서버 호출
  async function api(path, opts = {}) {
    const r = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
    let body = null;
    try { body = await r.json(); } catch { body = null; }
    if (!r.ok) {
      const err = (body && body.error) || { kind: "server", label: "오류", message: `HTTP ${r.status}` };
      throw err;
    }
    return body;
  }

  async function downloadPost(path, payload) {
    const r = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
    if (!r.ok) {
      let body = null;
      try { body = await r.json(); } catch { body = null; }
      throw (body && body.error) || { kind: "server", label: "오류", message: `HTTP ${r.status}` };
    }
    const blob = await r.blob();
    const cd = r.headers.get("Content-Disposition") || "";
    const m = cd.match(/filename\*=UTF-8''([^;]+)/i);
    const name = m ? decodeURIComponent(m[1]) : "data";
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = name;
    document.body.appendChild(a);
    a.click();
    setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 1500);
    const failed = r.headers.get("X-DataHub-Failed");
    return { name, failed: failed ? decodeURIComponent(failed) : null };
  }

  function alertBox(err) {
    if (!err) return "";
    const code = err.code ? ` (${esc(err.source || "")} 코드 ${esc(err.code)})` : "";
    return `<div class="alert k-${esc(err.kind)}" role="alert"><b>${esc(err.label || "오류")}</b>${esc(err.message)}${code}
      <div class="hint">${esc(HINT[err.kind] || "")}</div></div>`;
  }

  function toast(msg) {
    const t = $("#toast");
    t.textContent = msg;
    t.hidden = false;
    clearTimeout(toast._t);
    toast._t = setTimeout(() => (t.hidden = true), 3200);
  }

  // ------------------------------------------------------------ 시점 계산(서버 periods.py와 같은 규칙)
  function canon(cycle, s) {
    const t = String(s || "").trim().toUpperCase().replace(/\s/g, "");
    let m;
    if (cycle === "A" && (m = t.match(/^(\d{4})$/))) return [+m[1]];
    if (cycle === "S" && (m = t.match(/^(\d{4})[-.]?[HS]([12])$/))) return [+m[1], +m[2]];
    if (cycle === "Q" && (m = t.match(/^(\d{4})[-.]?Q([1-4])$/))) return [+m[1], +m[2]];
    if (cycle === "M" && (m = t.match(/^(\d{4})[-./]?(\d{2})$/)) && +m[2] >= 1 && +m[2] <= 12) return [+m[1], +m[2]];
    if (cycle === "SM" && (m = t.match(/^(\d{4})[-./]?(\d{2})[-.]?S([12])$/))) return [+m[1], +m[2], +m[3]];
    if (cycle === "D" && (m = t.match(/^(\d{4})[-./]?(\d{2})[-./]?(\d{2})$/))) return [+m[1], +m[2], +m[3]];
    if (cycle === "IR" || cycle === "F") return t ? [t] : null;
    return null;
  }
  function show(cycle, c) {
    const p2 = (n) => String(n).padStart(2, "0");
    return { A: () => `${c[0]}`, S: () => `${c[0]}-H${c[1]}`, Q: () => `${c[0]}-Q${c[1]}`, M: () => `${c[0]}-${p2(c[1])}`,
      SM: () => `${c[0]}-${p2(c[1])}-S${c[2]}`, D: () => `${c[0]}-${p2(c[1])}-${p2(c[2])}`, IR: () => `${c[0]}`, F: () => `${c[0]}` }[cycle]();
  }
  function shift(cycle, s, n) {
    const c = canon(cycle, s);
    if (!c) return s;
    if (cycle === "A") return show(cycle, [c[0] + n]);
    if (cycle === "S" || cycle === "Q" || cycle === "M") {
      const per = { S: 2, Q: 4, M: 12 }[cycle];
      const idx = c[0] * per + (c[1] - 1) + n;
      return show(cycle, [Math.floor(idx / per), (((idx % per) + per) % per) + 1]);
    }
    if (cycle === "D") {
      const d = new Date(Date.UTC(c[0], c[1] - 1, c[2] + n));
      return show(cycle, [d.getUTCFullYear(), d.getUTCMonth() + 1, d.getUTCDate()]);
    }
    return s;
  }
  function count(cycle, a, b) {
    const x = canon(cycle, a), y = canon(cycle, b);
    if (!x || !y) return null;
    if (cycle === "A") return y[0] - x[0] + 1;
    if (cycle === "S" || cycle === "Q" || cycle === "M") {
      const per = { S: 2, Q: 4, M: 12 }[cycle];
      return y[0] * per + y[1] - (x[0] * per + x[1]) + 1;
    }
    if (cycle === "SM") return ((y[0] * 12 + y[1]) * 2 + y[2]) - ((x[0] * 12 + x[1]) * 2 + x[2]) + 1;
    if (cycle === "D") return Math.round((Date.UTC(y[0], y[1] - 1, y[2]) - Date.UTC(x[0], x[1] - 1, x[2])) / 864e5) + 1;
    return 1;
  }
  function cmpPeriod(a, b) {
    for (let i = 0; i < Math.max(a.length, b.length); i++) {
      const x = a[i] ?? 0, y = b[i] ?? 0;
      if (x !== y) return x < y ? -1 : 1;
    }
    return 0;
  }
  const PLACEHOLDER = { A: "2020", S: "2020-H1", Q: "2020-Q1", M: "2020-01", SM: "2020-01-S1", D: "2020-01-01", IR: "2020", F: "2020" };
  const PER_YEAR = { A: 1, S: 2, Q: 4, M: 12, SM: 24, D: 365 };

  // ------------------------------------------------------------ 상태 표시
  async function loadStatus() {
    try {
      st.status = await api("/api/status");
    } catch (e) {
      st.status = null;
    }
    const s = st.status;
    const conn = $("#conn");
    if (!s) { conn.innerHTML = `<span class="bad"><span class="dot"></span>서버 연결 안 됨</span>`; return; }
    if (!s.ready) {   // 처음 켠 직후: 색인을 읽는 동안 기다렸다가 다시 확인
      conn.innerHTML = `<span><span class="dot"></span>검색 색인 불러오는 중</span>`;
      if (!st.lastQuery) $("#results").innerHTML = `<div class="loading">검색 색인을 불러오는 중입니다. 처음 켤 때는 20초쯤 걸립니다.${s.catalog_error ? `<br>읽기에 실패해 다시 시도하는 중입니다: ${esc(s.catalog_error)}` : ""}</div>`;
      setTimeout(loadStatus, 1500);
      return;
    }
    const has = (k) => (k === "AI" ? s.keys.AI || s.keys.OPENAI : s.keys[k]);     // AI는 Claude·GPT 키 중 하나만 있어도 됨
    conn.innerHTML = ["KOSIS", "ECOS", "DART", "AI"].map((k) =>
      `<span class="${has(k) ? "ok" : "bad"}" title="${has(k) ? "인증키 있음" : "인증키 없음"}"><span class="dot"></span>${k}${has(k) ? "" : " 키 없음"}</span>`).join("");
    fillDecFilters();
    if (s.decision && s.decision.loading) setTimeout(loadStatus, 2000);    // 의결서 색인을 읽는 중이면 조금 뒤 다시
    if (!st.lastQuery) {
      if (st.mode === "dart") renderDartWelcome(); else if (st.mode === "ai") renderAiWelcome();
      else if (st.mode === "dec") { if (!st.decRes && !st.decAiRes) (st.decAi ? renderDecAiWelcome : renderDecWelcome)(); } else renderWelcome();
    }
  }

  function renderWelcome() {
    const s = st.status;
    const c = (s && s.catalog) || {};
    const examples = ["CPI", "생산자물가지수", "원달러 환율", "기준금리", "기업 규모", "소매업태별 판매액", "광공업생산지수", "취업자"];
    $("#results").innerHTML = `<div class="welcome">
      <h1>KOSIS·한국은행 ECOS 통계와 공공데이터포털 자료를 한 번에 찾습니다</h1>
      <p>통계 이름을 몰라도 됩니다. 영문 약어나 비슷한 말로 찾고, 기간을 골라 출처가 적힌 파일로 받으세요.</p>
      <div class="examples">${examples.map((x) => `<button class="chip" data-q="${esc(x)}">${esc(x)}</button>`).join("")}</div>
      <div class="facts">
        <div>찾을 수 있는 통계표 <b>KOSIS ${fmtInt(c.KOSIS || 0)}개</b>, <b>ECOS ${fmtInt(c.ECOS || 0)}개</b> (ECOS 항목 ${fmtInt(c.ECOS_items || 0)}개 포함)${c.DATAGO ? `, 공공데이터포털 목록 <b>${fmtInt(c.DATAGO)}건</b>(파일·오픈API)` : ""}</div>
        <div>수치는 받을 때마다 기관 API에서 새로 가져옵니다.</div>
      </div></div>`;
    $("#detail").innerHTML = `<div class="panel empty"><b>왼쪽에서 통계를 고르면 여기에 나옵니다</b>정의·단위·수록 기간을 확인하고 항목과 기간을 골라 미리 본 뒤 내려받습니다.</div>`;
  }

  // ------------------------------------------------------------ 검색
  async function runSearch(q) {
    q = (q || "").trim();
    if (!q && !(st.mode === "dec" && !st.decAi && decHasFilter())) return;     // 의결서 낱말 검색은 검색어 없이 조건만으로도 목록
    $("#q").value = q;
    st.lastQuery = q;
    if (st.mode === "dec") { if (st.decAi) await runDecAi(q); else await runDecSearch(); return; }
    if (st.mode === "dart") { await runCorpSearch(q); return; }
    if (st.mode === "ai") { await runAi(q); return; }
    const params = new URLSearchParams({ q, source: st.source, cycle: st.cycle, syn: st.syn ? 1 : 0, recent: st.recent ? 1 : 0 });
    $("#results").innerHTML = `<div class="loading">'${esc(q)}' 찾는 중</div>`;
    let res;
    try {
      res = await api(`/api/search?${params}`);
    } catch (e) {
      $("#results").innerHTML = `<div class="welcome">${alertBox(e)}</div>`;
      return;
    }
    st.results = res.results;
    st.searchRes = res;
    renderResults(res);
  }

  async function loadMore(btn) {      // 검색 결과 다음 60개
    const res = st.searchRes;
    if (!res) return;
    btn.disabled = true; btn.textContent = "불러오는 중";
    const params = new URLSearchParams({ q: res.query, source: st.source, cycle: st.cycle, syn: st.syn ? 1 : 0,
      recent: st.recent ? 1 : 0, live: 0, offset: st.results.length });
    try {
      const more = await api(`/api/search?${params}`);
      st.results = st.results.concat(more.results);
      renderResults({ ...res, results: st.results, shown: st.results.length });
    } catch (e) {
      toast(`${e.label || "오류"}: ${e.message}`);
      btn.disabled = false; btn.textContent = "더 보기";
    }
  }

  function renderResults(res) {
    const ex = res.expansion || { synonyms: [], related: [] };
    let live = "";
    if (res.live && res.live.status === "error") {
      const e = res.live.errors[0] || {};
      live = `<div class="note warn">KOSIS 바로 검색 실패(${esc(e.label || "")}: ${esc(e.message || "")}). 저장된 색인에서만 찾았습니다.</div>`;
    } else if (res.live && res.live.status === "timeout") {
      live = `<div class="note warn">${esc(res.live.message)}</div>`;
    }
    const synRow = ex.synonyms.length ? `<div class="row"><span class="lab">같은 뜻으로 함께 찾음</span>${ex.synonyms.map((s) => `<button class="chip" data-q="${esc(s)}">${esc(s)}</button>`).join("")}</div>` : "";
    const relRow = ex.related.length ? `<div class="row"><span class="lab">관련 통계(정의가 다를 수 있음)</span>${ex.related.map((s) => `<button class="chip rel" data-q="${esc(s)}">${esc(s)}</button>`).join("")}</div>` : "";
    const more = res.count > res.shown ? ` <span class="note">중 상위 ${fmtInt(res.shown)}개. 아래 '더 보기'로 이어 보거나, 검색어를 더 구체적으로 쓰거나 필터로 좁혀 보세요.</span>` : "";
    const head = `<div class="results-head"><h2><b>${fmtInt(res.count)}</b>개 결과${more}</h2>
      ${synRow || relRow || live ? `<div class="expansion">${synRow}${relRow}${live}</div>` : ""}</div>`;
    if (!res.results.length) {
      $("#results").innerHTML = head + `<div class="welcome"><p>'${esc(res.query)}'에 맞는 자료가 없습니다. 더 짧은 말이나 다른 이름으로 찾아 보세요. 필터(기관·주기·지금도 나오는 통계만)를 풀어 보는 것도 방법입니다.</p></div>`;
      return;
    }
    const items = res.results.map((h, i) => {
      const x = h.extra || {};
      // 수록 기간을 아는 표는 '2016–2026', 모르는 KOSIS 표의 연도는 최종 갱신 연도라 '갱신 2025'
      const lastYear = h.span ? h.span[1].slice(0, 4) : h.end_year;
      const span = h.source === "DATAGO" ? (x.modified ? `수정 ${x.modified}` : "")
        : h.span ? `${h.span[0].slice(0, 4)}–${h.span[1].slice(0, 4)}` : (h.end_year && !h.discontinued ? `갱신 ${h.end_year}` : "");
      const stale = h.discontinued
        ? `<span class="tag off" title="마지막 연도가 3년 넘게 지났습니다. 폐지됐다는 뜻은 아닙니다(조사 주기가 길거나 해마다 새 표로 나오는 통계일 수 있음).">${h.span ? "마지막 자료" : "마지막 갱신"} ${esc(lastYear)}</span>` : "";
      const why = h.reason ? h.reason.replace(/'([^']+)'/g, (m, x) => `'<em>${esc(x)}</em>'`) : "";
      return `<button class="hit src-${h.source}${st.hit && st.hit.source === h.source && st.hit.id === h.id ? " sel" : ""}" data-i="${i}">
        <div class="t">${esc(h.name)}</div>
        <div class="m"><span class="tag ${h.source}">${esc(SRC_ORG[h.source] || h.source)} ${esc(h.org_name || "")}</span>
          ${h.source === "DATAGO" ? `<span class="tag">${esc(DG_KIND[x.kind] || x.kind || "")}${x.fmt ? " " + esc(x.fmt) : ""}</span>` : ""}
          ${h.cycle_labels.length ? `<span class="tag">${h.cycle_labels.join(" ")}</span>` : ""}
          ${span ? `<span class="tag plain">${esc(span)}</span>` : ""}
          ${stale}${h.international ? `<span class="tag intl">국제·외국 통계</span>` : ""}${h.regional ? `<span class="tag intl" title="KOSIS 지역통계: 시·도, 시·군·구가 만든 통계">지자체 통계</span>` : ""}${st.cycle && !h.cycles_known ? `<span class="tag" title="이 표는 아직 열어 보지 않아 주기를 모릅니다. 열면 확인됩니다.">주기 확인 전(열면 확인)</span>` : ""}</div>
        ${h.path ? `<div class="p" title="${esc(h.path)}">${esc(h.path)}</div>` : ""}
        <div class="why">${why}</div></button>`;
    }).join("");
    const left = res.count - res.results.length;
    const moreBtn = left > 0 ? `<div style="padding:12px 16px"><button class="btn btn-ghost" id="btnMore" type="button" style="width:100%">더 보기 (${fmtInt(left)}개 남음)</button></div>` : "";
    $("#results").innerHTML = head + `<div class="list">${items}</div>${moreBtn}`;
  }

  // ------------------------------------------------------------ 상세
  async function openHit(h) {
    st.hit = h;
    document.querySelectorAll(".hit").forEach((b) => b.classList.toggle("sel", st.results[+b.dataset.i] === h));
    st.detail = null; st.preview = null; st.previewErr = null;
    $("#detail").innerHTML = `<div class="panel"><div class="loading">통계표 정보를 받는 중</div></div>`;
    let d;
    try {
      d = await api(`/api/detail?source=${encodeURIComponent(h.source)}&id=${encodeURIComponent(h.id)}`);
    } catch (e) {
      $("#detail").innerHTML = `<div class="panel">${alertBox(e)}</div>`;
      return;
    }
    if (st.hit !== h) return;
    st.detail = d;
    if (d.source === "DATAGO") { renderDatago(); return; }
    const cyc = d.cycles.find((c) => c.cycle === st.cycle) || CYCLE_PREF.map((k) => d.cycles.find((c) => c.cycle === k)).find(Boolean) || d.cycles[0];
    st.sel = {};
    d.dims.forEach((dim) => {
      const hitCodes = (h.items || []).filter((x) => x.grp === dim.key).map((x) => x.code).filter((c) => dim.items.some((it) => it.code === c));
      st.sel[dim.key] = new Set(hitCodes.length ? hitCodes.slice(0, 5) : dim.items.length ? [dim.items[0].code] : []);
    });
    st.spec = { source: d.source, id: d.id, cycle: cyc ? cyc.cycle : null, start: cyc ? cyc.default_start : "", end: cyc ? cyc.default_end : "" };
    renderDetail();
    if (cyc) runPreview();
  }

  function renderDetail() {
    const d = st.detail;
    if (!d) return;
    const ids = Object.entries(d.ids).map(([k, v]) => `<div class="fact"><div class="k">${esc(k)}</div><div class="v">${esc(v)}</div></div>`).join("");
    const cycles = d.cycles.map((c) => `${c.label} ${esc(c.start)} ~ ${esc(c.end)}`).join("<br>");
    const info = Object.entries(d.info || {});
    const infoHtml = info.length ? `<details class="more"><summary>통계 설명</summary><div class="body">${info.map(([k, v]) => `<div class="kv"><b>${esc(k)}</b><p>${esc(v)}</p></div>`).join("")}</div></details>` : "";
    const notes = d.notes && d.notes.length ? `<details class="more"><summary>원천 주석 ${d.notes.length}개</summary><div class="body">${d.notes.map((n) => `<p>${esc(n)}</p>`).join("")}</div></details>` : "";
    $("#detail").innerHTML = `
      <div class="panel">
        <div class="d-head"><div>
          <span class="tag ${d.source}">${esc(d.source_label)}</span>
          <h2>${esc(d.name)}</h2>
          ${d.path ? `<div class="d-path">${esc(d.path)}</div>` : ""}
        </div>
        <a class="btn btn-sm" href="${esc(d.url)}" target="_blank" rel="noopener">원본 화면 열기</a></div>
        <div class="facts-grid">
          <div class="fact"><div class="k">작성 기관</div><div class="v">${esc(d.org_name || "")}</div></div>
          ${ids}
          <div class="fact"><div class="k">수록 주기·기간</div><div class="v">${cycles || "정보 없음"}</div></div>
          <div class="fact"><div class="k">단위</div><div class="v">${esc(d.unit || "항목마다 다름")}</div></div>
        </div>
        ${(d.warnings || []).map((w) => `<div class="note warn">${esc(w)}</div>`).join("")}
        ${infoHtml}${notes}
      </div>
      <div class="panel" id="queryPanel"></div>
      <div class="panel" id="previewPanel"><div class="empty"><b>미리보기</b>조건을 고르고 '미리보기'를 누르세요.</div></div>`;
    renderQuery();
  }

  function renderQuery() {
    const d = st.detail, sp = st.spec;
    const cyc = d.cycles.find((c) => c.cycle === sp.cycle);
    if (!cyc) {
      $("#queryPanel").innerHTML = `<div class="q-head"><h3>조회 조건</h3></div>${alertBox({ kind: "no_data", label: "조회할 수 있는 주기가 없습니다", message: "이 통계표는 API로 받을 수 있는 수록 주기 정보가 없습니다. 원본 화면에서 확인하세요." })}`;
      return;
    }
    const cycleSeg = `<div class="seg" role="group" aria-label="주기">${d.cycles.map((c) => `<button type="button" data-qcycle="${c.cycle}" class="${c.cycle === sp.cycle ? "on" : ""}">${c.label}</button>`).join("")}</div>`;
    const quick = (cyc.cycle in PER_YEAR) ? `<div class="quick">
        <button type="button" class="btn btn-sm btn-ghost" data-span="1">최근 1년</button>
        <button type="button" class="btn btn-sm btn-ghost" data-span="5">5년</button>
        <button type="button" class="btn btn-sm btn-ghost" data-span="10">10년</button>
        <button type="button" class="btn btn-sm btn-ghost" data-span="all">전체</button></div>` : "";
    const dims = d.dims.map((dim) => dimBox(dim)).join("");
    $("#queryPanel").innerHTML = `
      <div class="q-head"><h3>조회 조건</h3><span class="note">수록 ${esc(cyc.start)} ~ ${esc(cyc.end)}</span></div>
      <div class="q-row">
        <div class="field"><span>주기</span>${cycleSeg}</div>
        <div class="field"><span>시작</span><input type="text" id="pStart" value="${esc(sp.start)}" placeholder="${PLACEHOLDER[sp.cycle] || ""}"></div>
        <div class="field"><span>끝</span><input type="text" id="pEnd" value="${esc(sp.end)}" placeholder="${PLACEHOLDER[sp.cycle] || ""}"></div>
        ${quick}
      </div>
      <div class="dims">${dims}</div>
      <div class="q-foot"><div class="est" id="est"></div>
        <button class="btn btn-primary" id="btnPreview" type="button">미리보기</button></div>`;
    updateEstimate();
  }

  function dimBox(dim, filter = "") {
    const sel = st.sel[dim.key];
    const f = filter.trim().toLowerCase();
    const LIMIT = 400;
    let shown = 0, matched = 0;
    const rows = [];
    for (const it of dim.items) {
      const hay = `${it.name} ${it.code}`.toLowerCase();
      if (f && !hay.includes(f)) continue;
      matched++;
      if (shown >= LIMIT) continue;
      shown++;
      const pad = f ? 0 : Math.min(it.depth || 0, 6) * 14;
      let name = esc(it.name);
      if (f) {
        const i = it.name.toLowerCase().indexOf(f);
        if (i >= 0) name = esc(it.name.slice(0, i)) + `<span class="hl">${esc(it.name.slice(i, i + f.length))}</span>` + esc(it.name.slice(i + f.length));
      }
      const cy = it.cycles ? ` title="${esc(Object.entries(it.cycles).map(([c, r]) => `${CYCLE_LABEL[c]} ${r[0]}~${r[1]}`).join(", "))}"` : "";
      rows.push(`<label class="opt" style="padding-left:${12 + pad}px"${cy}><input type="checkbox" data-dim="${esc(dim.key)}" value="${esc(it.code)}" ${sel.has(it.code) ? "checked" : ""}><span>${name}<span class="code">${esc(it.code)}</span></span></label>`);
    }
    const more = matched > shown ? `<div class="dim-more">${fmtInt(matched - shown)}개 더 있음. 위 칸에 이름을 넣어 좁혀 보세요.</div>` : "";
    const tools = dim.items.length > 8 ? `<div class="dim-tools"><input type="search" placeholder="${esc(dim.name)}에서 찾기" data-filter="${esc(dim.key)}" value="${esc(filter)}"></div>` : "";
    return `<div class="dim" data-dimbox="${esc(dim.key)}">
      <div class="dim-h"><div><b>${esc(dim.name)}</b> <span class="cnt">${fmtInt(sel.size)} / ${fmtInt(dim.items.length)}개 선택</span></div>
        <div><button type="button" class="btn btn-sm btn-ghost" data-all="${esc(dim.key)}">전체</button><button type="button" class="btn btn-sm btn-ghost" data-none="${esc(dim.key)}">해제</button></div></div>
      ${tools}<div class="dim-list">${rows.join("") || `<div class="dim-more">맞는 항목이 없습니다.</div>`}${more}</div></div>`;
  }

  function refreshDim(key) {
    const dim = st.detail.dims.find((x) => x.key === key);
    const box = document.querySelector(`[data-dimbox="${CSS.escape(key)}"]`);
    const filterEl = box && box.querySelector("[data-filter]");
    const filter = filterEl ? filterEl.value : "";
    const tmp = document.createElement("div");
    tmp.innerHTML = dimBox(dim, filter);
    box.replaceWith(tmp.firstElementChild);
    if (filter) {
      const el = document.querySelector(`[data-dimbox="${CSS.escape(key)}"] [data-filter]`);
      el.focus();
      el.setSelectionRange(filter.length, filter.length);
    }
    updateEstimate();
  }

  function currentSpec() {
    const d = st.detail, sp = st.spec;
    const sel = {};
    d.dims.forEach((dim) => {
      const s = st.sel[dim.key];
      sel[dim.key] = s.size === dim.items.length ? "ALL" : [...s];
    });
    return { source: d.source, id: d.id, cycle: sp.cycle, start: sp.start, end: sp.end, sel, name: d.name };
  }

  function updateEstimate() {
    const el = $("#est");
    if (!el) return;
    const d = st.detail, sp = st.spec;
    const series = d.dims.reduce((acc, dim) => acc * st.sel[dim.key].size, 1);
    const n = count(sp.cycle, sp.start, sp.end);
    const empty = d.dims.filter((dim) => st.sel[dim.key].size === 0).map((dim) => dim.name);
    const btn = $("#btnPreview");
    if (empty.length) {
      el.innerHTML = `${esc(empty.join(", "))}에서 하나 이상 고르세요.`;
      btn.disabled = true;
      return;
    }
    btn.disabled = n === null;
    if (n === null) { el.innerHTML = `기간 형식을 확인하세요. 예: ${PLACEHOLDER[sp.cycle] || ""}`; return; }
    if (sp.cycle === "IR" || sp.cycle === "F") { el.innerHTML = `시계열 <b>${fmtInt(series)}</b>개, 기간 안의 수록 시점 전부`; return; }
    el.innerHTML = `시계열 <b>${fmtInt(series)}</b>개 × 기간 <b>${fmtInt(Math.max(n, 0))}</b>개 = 최대 <b>${fmtInt(series * Math.max(n, 0))}</b>칸`;
  }


  // ------------------------------------------------------------ 행·열 배치(KOSIS 행렬전환). 규칙은 datahub/pivot.py 와 같다
  const PV_TIME = "시점";
  function pvDefault(p) {
    const d = [...(p.dim_names || [])];
    return p.layout === "accounts" ? { rows: d, cols: [PV_TIME] } : { rows: [PV_TIME], cols: d };
  }
  function pvSame(a, b) { return JSON.stringify(a) === JSON.stringify(b); }
  function pvCurrent(p) { return st.pivot || pvDefault(p); }
  function pvLabels(p) {
    const dims = p.dim_names || [], S = p.series;
    const codes = S.map((s) => Object.values(s.codes || {}));
    const out = S.map((s, si) => Object.fromEntries(dims.map((d, i) => {
      const nm = (s.names || {})[d];
      return [d, nm != null && nm !== "" ? String(nm) : String(codes[si][i] ?? "")];
    })));
    const groups = new Map();               // 모든 차원 이름이 같은 시계열끼리만, 코드가 갈리는 차원에 '(코드)'
    out.forEach((o, si) => { const k = JSON.stringify(dims.map((d) => o[d])); if (!groups.has(k)) groups.set(k, []); groups.get(k).push(si); });
    groups.forEach((g) => {
      if (g.length < 2) return;
      dims.forEach((d, i) => {
        if (new Set(g.map((si) => codes[si][i] ?? null)).size > 1) {
          g.forEach((si) => { const c = codes[si][i]; if (c != null && c !== "") out[si][d] = `${out[si][d]} (${c})`; });
        }
      });
    });
    const units = new Set(S.map((s) => s.unit ?? null));
    if (units.size > 1 && dims.length) out.forEach((o, si) => { o[dims[0]] = `${o[dims[0]]} [${S[si].unit || ""}]`; });
    if (dims.length) {
      const seen = new Map(), last = dims[dims.length - 1];
      out.forEach((o) => { const k = JSON.stringify(dims.map((d) => o[d])); const n = (seen.get(k) || 0) + 1; seen.set(k, n); if (n > 1) o[last] = `${o[last]} #${n}`; });
    }
    return out;
  }
  function pivotGrid(p, pv) {
    const lab = pvLabels(p), dims = p.dim_names || [];
    const order = { [PV_TIME]: new Map(p.times.map((t, i) => [t, i])) };
    dims.forEach((d) => { order[d] = new Map(); lab.forEach((o) => { if (!order[d].has(o[d])) order[d].set(o[d], order[d].size); }); });
    const cells = new Map(), rks = new Map(), cks = new Map();
    p.rows.forEach(([si, t, v]) => {
      const val = { ...lab[si], [PV_TIME]: t };
      const rk = pv.rows.map((d) => val[d]), ck = pv.cols.map((d) => val[d]);
      const rs = JSON.stringify(rk), cs = JSON.stringify(ck);
      cells.set(rs + "\u0000" + cs, v); rks.set(rs, rk); cks.set(cs, ck);
    });
    const sortKeys = (m, ds) => [...m.values()].sort((a, b) => {
      for (let i = 0; i < ds.length; i++) {
        const x = order[ds[i]].get(a[i]) ?? 1e9, y = order[ds[i]].get(b[i]) ?? 1e9;
        if (x !== y) return x - y;
      }
      return 0;
    });
    const rkeys = sortKeys(rks, pv.rows), ckeys = sortKeys(cks, pv.cols);
    const nLeft = Math.max(1, pv.rows.length), leftNames = pv.rows.length ? pv.rows : ["값"];
    let heads;
    if (pv.cols.length <= 1) heads = [leftNames.concat(pv.cols.length ? ckeys.map((k) => k[0]) : ["값"])];
    else {
      heads = pv.cols.map((c, j) => Array(nLeft - 1).fill("").concat([c], ckeys.map((k) => k[j])));
      heads.push(leftNames.concat(Array(ckeys.length).fill("")));
    }
    const body = rkeys.map((rk) => (pv.rows.length ? rk : ["값"]).concat(ckeys.map((ck) => cells.get(JSON.stringify(rk) + "\u0000" + JSON.stringify(ck)) ?? null)));
    return { heads, body, nLeft };
  }
  function pivotControl(p) {
    const pv = pvCurrent(p);
    const chip = (d, side, i) => `<span class="pv-chip">${esc(d)}
        ${i > 0 ? `<button type="button" class="btn btn-sm btn-ghost" data-pvup="${esc(d)}" title="앞으로(바깥쪽으로)">↑</button>` : ""}
        <button type="button" class="btn btn-sm btn-ghost" data-pvflip="${esc(d)}" title="${side === "rows" ? "열로 보내기" : "행으로 보내기"}">${side === "rows" ? "열로 →" : "← 행으로"}</button></span>`;
    return `<div class="pv-ctl"><span class="lab">행·열 배치</span>
      <div class="pv-zone"><b>행</b>${pv.rows.map((d, i) => chip(d, "rows", i)).join("") || '<span class="note">없음</span>'}</div>
      <div class="pv-zone"><b>열</b>${pv.cols.map((d, i) => chip(d, "cols", i)).join("") || '<span class="note">없음</span>'}</div>
      <button type="button" class="btn btn-sm" data-pvswap="1">행↔열 통째로</button>
      ${st.pivot ? `<button type="button" class="btn btn-sm btn-ghost" data-pvreset="1">처음대로</button>` : ""}
      <span class="note">Excel 가로형 시트·CSV 가로형·다운로드 목록이 이 배치를 따릅니다(세로형은 그대로).</span></div>`;
  }
  function pivotTableHtml(p, fmtCell) {
    const { heads, body, nLeft } = pivotGrid(p, pvCurrent(p));
    const pv = pvCurrent(p), LR = 80, LC = 30;
    const tailRows = pv.rows[0] === PV_TIME, tailCols = pv.cols[0] === PV_TIME;   // 시점이 바깥이면 최근 쪽을 보여 준다
    const nC = heads[0].length - nLeft;
    const cIdx = [...Array(nC).keys()];
    const showC = nC > LC ? (tailCols ? cIdx.slice(-LC) : cIdx.slice(0, LC)) : cIdx;
    const showB = body.length > LR ? (tailRows ? body.slice(-LR) : body.slice(0, LR)) : body;
    const row = (r, tag) => `<tr>${r.slice(0, nLeft).map((x) => `<${tag} class="pv-left">${esc(x ?? "")}</${tag}>`).join("")}${showC.map((i) => `<${tag}>${tag === "td" ? fmtCell(r[nLeft + i]) : esc(r[nLeft + i] ?? "")}</${tag}>`).join("")}</tr>`;
    const headRow = (h, j) => {       // 열 머리글 위층은 바깥 차원 값이 같은 칸끼리 합쳐 보인다(파일은 칸마다 이름을 그대로 둠)
      const merge = pv.cols.length >= 2 && j < pv.cols.length - 1;
      let cells = "", i = 0;
      while (i < showC.length) {
        let k = i + 1;
        if (merge) while (k < showC.length && heads.slice(0, j + 1).every((hh) => hh[nLeft + showC[k]] === hh[nLeft + showC[i]])) k++;
        cells += `<th${k - i > 1 ? ` colspan="${k - i}" class="pv-span"` : ""}>${esc(h[nLeft + showC[i]] ?? "")}</th>`;
        i = k;
      }
      return `<tr>${h.slice(0, nLeft).map((x) => `<th class="pv-left">${esc(x ?? "")}</th>`).join("")}${cells}</tr>`;
    };
    const notes = [];
    if (body.length > showB.length) notes.push(`행 ${fmtInt(body.length)}개 중 ${tailRows ? "마지막" : "처음"} ${showB.length}개만 보입니다.`);
    if (nC > showC.length) notes.push(`열 ${fmtInt(nC)}개 중 ${tailCols ? "마지막" : "처음"} ${showC.length}개만 보입니다.`);
    if (notes.length) notes.push("전체는 내려받은 파일에 있습니다.");
    return `<div class="tbl-wrap"><table class="data pv"><thead>${heads.map((h, j) => headRow(h, j)).join("")}</thead><tbody>${showB.map((r) => row(r, "td")).join("")}</tbody></table></div>
      ${notes.length ? `<div class="tbl-note">${notes.join(" ")}</div>` : ""}`;
  }
  function pivotChange(p, fn) {
    const pv = JSON.parse(JSON.stringify(pvCurrent(p)));
    fn(pv);
    st.pivot = pvSame(pv, pvDefault(p)) ? null : pv;
    rerenderPreview();
  }

  // ------------------------------------------------------------ 미리보기
  async function runPreview() {
    const spec = currentSpec();
    const panel = $("#previewPanel");
    panel.innerHTML = `<div class="loading">${esc(st.detail.source)}에서 자료를 받는 중</div>`;
    const btn = $("#btnPreview");
    if (btn) btn.disabled = true;
    try {
      st.preview = await api("/api/preview", { method: "POST", body: JSON.stringify(spec) });
      st.previewErr = null;
      st.previewSpec = spec;
      st.pivot = null;
    } catch (e) {
      st.preview = null;
      st.previewErr = e;
    }
    if (btn) btn.disabled = false;
    renderPreview();
  }

  function renderPreview() {
    const panel = $("#previewPanel");
    if (!panel) return;
    if (st.previewErr) { panel.innerHTML = `<div class="pv-head"><h3>미리보기</h3></div>${alertBox(st.previewErr)}`; return; }
    const p = st.preview;
    if (!p) return;
    const times = p.times;
    const S = p.series;
    const grid = new Map();
    p.rows.forEach(([si, t, v]) => { if (!grid.has(t)) grid.set(t, {}); grid.get(t)[si] = v; });
    const showT = times.slice(-36).reverse();
    const showS = S.slice(0, 10);
    const units = new Set(S.map((s) => s.unit));
    const head = `<tr><th>시점</th>${showS.map((s) => `<th title="${esc(s.label)}">${esc(s.label)}${units.size > 1 && s.unit ? `<br><small>${esc(s.unit)}</small>` : ""}</th>`).join("")}</tr>`;
    const body = showT.map((t) => `<tr><td>${esc(t)}</td>${showS.map((s, i) => { const v = (grid.get(t) || {})[S.indexOf(s)]; return `<td>${v == null ? "" : esc(v)}</td>`; }).join("")}</tr>`).join("");
    const noteBits = [];
    if (times.length > showT.length) noteBits.push(`가장 최근 ${showT.length}개 시점만 보입니다(전체 ${fmtInt(times.length)}개).`);
    if (S.length > showS.length) noteBits.push(`시계열 ${fmtInt(S.length)}개 중 ${showS.length}개만 보입니다.`);
    if (noteBits.length) noteBits.push("전체는 내려받은 파일에 있습니다.");
    const unitText = units.size === 1 ? [...units][0] : "시계열마다 다름";
    const warn = (p.warnings || []).map((w) => `<div class="note warn">${esc(w)}</div>`).join("");
    const inCart = st.cart.some((c) => sameSpec(c.spec, st.previewSpec));
    panel.innerHTML = `
      <div class="pv-head"><div><h3>미리보기</h3>
        <div class="pv-sum"><span>행 <b>${fmtInt(p.row_count)}</b></span><span>시계열 <b>${fmtInt(S.length)}</b></span>
          <span>${esc(p.cycle_label)} <b>${esc(p.start)} ~ ${esc(p.end)}</b></span><span>단위 <b>${esc(unitText || "")}</b></span>
          <span>수집 <b>${esc(p.fetched_at)}</b></span></div></div>
        <div class="actions">
          <button class="btn btn-strong" data-dl="xlsx" type="button">Excel 받기</button>
          <select id="csvShape" aria-label="CSV 모양"><option value="long">CSV 세로형</option><option value="wide">CSV 가로형(화면 배치대로)</option></select>
          <button class="btn" data-dl="csv" type="button">CSV 받기</button>
          <button class="btn" id="btnCart" type="button" ${inCart ? "disabled" : ""}>${inCart ? "목록에 담김" : "다운로드 목록에 담기"}</button>
        </div></div>
      ${warn}
      <div class="chart">${chart(p)}</div>
      ${pivotControl(p)}
      ${st.pivot ? pivotTableHtml(p, (v) => (v == null ? "" : esc(v))) : `<div class="tbl-wrap"><table class="data"><thead>${head}</thead><tbody>${body}</tbody></table></div>
      ${noteBits.length ? `<div class="tbl-note">${noteBits.join(" ")}</div>` : ""}`}
      <details class="more prov"><summary>출처·조회 조건 기록</summary><div class="body">
        <div class="kv"><b>선택</b>${esc(p.selection_text)}</div>
        <div class="kv"><b>API 호출 ${p.requests.length}건(인증키 가림)</b>${p.requests.slice(0, 8).map((u) => `<code>${esc(u)}</code>`).join("")}${p.requests.length > 8 ? `<div>외 ${p.requests.length - 8}건</div>` : ""}</div>
        <div class="kv"><b>파일에 남는 것</b>원천 시스템, 작성 기관, 통계표 ID, 항목 코드, 단위, 조회 기간, 수집 시점, 호출 URL. Excel은 '출처·조회조건' 시트에 모아 둡니다.</div>
      </div></details>`;
  }

  function chart(p) {
    const S = p.series.slice(0, 6);
    const times = p.times;
    if (!times.length || !S.length) return "";
    const idx = new Map(times.map((t, i) => [t, i]));
    const lines = S.map(() => []);
    p.rows.forEach(([si, t, v]) => {
      if (si >= S.length) return;
      const n = parseFloat(String(v).replace(/,/g, ""));
      if (Number.isFinite(n)) lines[si].push([idx.get(t), n]);
    });
    const all = lines.flat();
    if (!all.length) return `<div class="note">숫자로 그릴 값이 없습니다.</div>`;
    let lo = Math.min(...all.map((x) => x[1])), hi = Math.max(...all.map((x) => x[1]));
    if (lo === hi) { lo -= 1; hi += 1; }
    const pad = (hi - lo) * 0.08; lo -= pad; hi += pad;
    const W = 1000, H = 240, L = 58, R = 12, T = 10, B = 26;
    const X = (i) => L + (times.length === 1 ? (W - L - R) / 2 : (i / (times.length - 1)) * (W - L - R));
    const Y = (v) => T + (1 - (v - lo) / (hi - lo)) * (H - T - B);
    const fmt = (v) => Math.abs(hi - lo) >= 100 ? Math.round(v).toLocaleString("ko-KR") : v.toLocaleString("ko-KR", { maximumFractionDigits: 2 });
    let grid = "";
    for (let k = 0; k <= 4; k++) {
      const v = lo + ((hi - lo) * k) / 4;
      grid += `<line class="axis" x1="${L}" x2="${W - R}" y1="${Y(v)}" y2="${Y(v)}"/><text x="${L - 8}" y="${Y(v) + 4}" text-anchor="end">${fmt(v)}</text>`;
    }
    const ticks = [0, Math.floor((times.length - 1) / 2), times.length - 1].filter((v, i, a) => a.indexOf(v) === i);
    grid += ticks.map((i) => `<text x="${X(i)}" y="${H - 6}" text-anchor="${i === 0 ? "start" : i === times.length - 1 ? "end" : "middle"}">${esc(times[i])}</text>`).join("");
    const paths = lines.map((pts, si) => {
      if (!pts.length) return "";
      pts.sort((a, b) => a[0] - b[0]);
      const dpath = pts.map(([i, v], k) => `${k ? "L" : "M"}${X(i).toFixed(1)},${Y(v).toFixed(1)}`).join("");
      const dot = pts.length === 1 ? `<circle cx="${X(pts[0][0])}" cy="${Y(pts[0][1])}" r="3.5" fill="${PALETTE[si]}"/>` : "";
      return `<path d="${dpath}" fill="none" stroke="${PALETTE[si]}" stroke-width="2" vector-effect="non-scaling-stroke" stroke-linejoin="round"/>${dot}`;
    }).join("");
    const legend = S.map((s, i) => `<span><i style="background:${PALETTE[i]}"></i>${esc(s.label)}</span>`).join("");
    const more = p.series.length > S.length ? `<span class="note">그래프는 앞의 ${S.length}개 시계열만</span>` : "";
    return `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img" aria-label="시계열 그래프">${grid}${paths}</svg><div class="legend">${legend}${more}</div>`;
  }

  // ------------------------------------------------------------ 공공데이터포털(목록 정보 + 원본 파일)
  function renderDatago() {
    const d = st.detail;
    const facts = d.fields.filter(([k]) => ["제공기관", "분류체계", "목록유형", "확장자(데이터포맷)", "수정일", "업데이트 주기", "전체행",
      "이용허락범위", "제공형태", "API 유형"].includes(k))
      .map(([k, v]) => `<div class="fact"><div class="k">${esc(k === "확장자(데이터포맷)" ? "형식" : k)}</div><div class="v">${esc(k === "목록유형" ? (DG_KIND[v] || v) : v)}</div></div>`).join("");
    const more = d.fields.filter(([k]) => !["제공기관", "분류체계", "목록유형", "확장자(데이터포맷)", "수정일", "업데이트 주기", "전체행",
      "이용허락범위", "제공형태", "API 유형"].includes(k)).map(([k, v]) => `<div class="kv"><b>${esc(k)}</b>${esc(v)}</div>`).join("");
    $("#detail").innerHTML = `
      <div class="panel">
        <div class="d-head"><div><span class="tag DATAGO">${esc(d.source_label)} · ${esc(d.kind_label)}</span>
          <h2>${esc(d.name)}</h2>${d.path ? `<div class="d-path">${esc(d.path)}</div>` : ""}</div></div>
        <div class="facts-grid"><div class="fact"><div class="k">목록키</div><div class="v">${esc(d.id)}</div></div>${facts}</div>
        ${d.info["설명"] ? `<p style="margin:14px 0 0;white-space:pre-wrap">${esc(d.info["설명"])}</p>` : ""}
        ${d.info["키워드"] ? `<div class="note" style="margin-top:8px">키워드: ${esc(d.info["키워드"])}</div>` : ""}
        <div class="dg-actions">
          ${d.portal_file ? `<button class="btn btn-strong" id="btnDgFile" type="button">파일 받기(원본 그대로)</button>` : ""}
          <a class="btn" href="${esc(d.url)}" target="_blank" rel="noopener">원본 페이지 열기</a>
        </div>
        ${(d.notes || []).map((n) => `<div class="note" style="margin-top:10px">${esc(n)}</div>`).join("")}
        ${more ? `<details class="more"><summary>목록 정보 더 보기</summary><div class="body">${more}
          <div class="kv"><b>목록 정보 출처</b>${esc(d.catalog || "")}</div></div></details>` : ""}
      </div>`;
  }

  async function downloadGet(path) {
    const r = await fetch(path);
    if (!r.ok) {
      let body = null;
      try { body = await r.json(); } catch { body = null; }
      throw (body && body.error) || { kind: "server", label: "오류", message: `HTTP ${r.status}` };
    }
    const blob = await r.blob();
    const cd = r.headers.get("Content-Disposition") || "";
    const m = cd.match(/filename\*=UTF-8''([^;]+)/i);
    const name = m ? decodeURIComponent(m[1]) : "data";
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = name;
    document.body.appendChild(a);
    a.click();
    setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 1500);
    return { name };
  }

  // ------------------------------------------------------------ 회사(OpenDART)
  const SJ = [["BS", "재무상태표"], ["IS", "손익계산서"], ["CIS", "포괄손익계산서"], ["CF", "현금흐름표"], ["SCE", "자본변동표"]];
  const REPRT = [["11011", "사업보고서(연간)"], ["11012", "반기"], ["11013", "1분기"], ["11014", "3분기"]];
  const UNIT_DIV = { "원": 1, "백만원": 1e6, "억원": 1e8 };
  const today = new Date();
  const iso = (d) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;

  function setMode(m, keepQuery) {
    st.mode = m;
    st.aiRes = null;
    renderAiBrief(null);
    document.querySelectorAll("[data-mode]").forEach((b) => b.classList.toggle("on", b.dataset.mode === m));
    $("#filters").hidden = m !== "stat";
    $("#dartFilters").hidden = m !== "dart";
    $("#aiNote").hidden = m !== "ai";
    $("#decFilters").hidden = m !== "dec";
    $("#q").maxLength = m === "ai" || (m === "dec" && st.decAi) ? 500 : m === "dec" ? 200 : 80;
    $("#q").placeholder = m === "dart" ? "회사 이름, 종목코드 6자리, 영문명 (예: 삼성전자, 005930, LG전자)"
      : m === "dec" ? (st.decAi ? "의결서에 대해 문장으로 묻기 (예: 입찰담합에서 조사협력 감경은 어떻게 적용했나요?)" : "의결서 본문·사건명에서 찾을 말 (예: 자진신고 감경, \"관련매출액\" 산정, 2019서경)")
      : m === "ai" ? "필요한 자료를 문장으로 (예: 라면 시장이 커졌는지 볼 수 있는 자료)"
      : "통계 이름, 영문 약어, 비슷한 말 (예: CPI, 원달러 환율, 기업 규모)";
    if (!keepQuery) {
      st.lastQuery = ""; $("#q").value = ""; st.hit = null; st.corp = null;
      st.decRes = null; st.decList = []; st.decSel = null; st.decDoc = null; st.decPage = null; st.decAiRes = null;
      if (m === "dart") renderDartWelcome(); else if (m === "ai") renderAiWelcome();
      else if (m === "dec") (st.decAi ? renderDecAiWelcome : renderDecWelcome)(); else renderWelcome();
      writeHash();
    }
  }

  // ------------------------------------------------------------ AI 추천(자연어 질문 → 색인 속 자료 우선순위)
  async function loadAiModels() {      // 모델 고르는 칸(고른 값은 이 브라우저에 기억)
    const sel = $("#aiModel");
    if (!sel || sel.options.length) return;
    try {
      const r = await api("/api/ai/models");
      let saved = null;
      try { saved = localStorage.getItem("datahub.aiModel"); } catch { saved = null; }
      sel.innerHTML = r.models.map((m) => `<option value="${esc(m.id)}" ${m.available ? "" : "disabled"}>${esc(m.label)}${m.available ? "" : " (키 없음)"}</option>`).join("");
      const avail = (id) => r.models.some((m) => m.id === id && m.available);
      sel.value = avail(saved) ? saved : avail(r.default) ? r.default : ((r.models.find((m) => m.available) || {}).id || r.default);
    } catch { /* 모델 목록을 못 받아도 서버 기본 모델로 동작 */ }
  }

  function renderAiWelcome() {
    loadAiModels();
    const ok = st.status && st.status.keys && (st.status.keys.AI || st.status.keys.OPENAI);
    const examples = ["라면 시장이 커졌는지 볼 수 있는 자료", "편의점 가맹점 수와 매출 추이", "건설 하도급 거래 규모를 볼 자료",
      "대형마트와 온라인 쇼핑 매출 비중 변화", "소주 가격과 출고량 추이"];
    $("#results").innerHTML = `<div class="welcome">
      <h1>필요한 자료를 문장으로 물어보세요</h1>
      <p>AI가 질문을 검색어로 풀어 이 프로그램 색인에서 후보를 모은 뒤, 볼 만한 순서로 추천합니다. 추천은 색인에 실제로 있는 자료 중에서만 합니다.</p>
      ${ok ? "" : `<div class="note warn">AI 키(ANTHROPIC_API_KEY 또는 OPENAI_API_KEY)가 없어 지금은 쓸 수 없습니다.</div>`}
      <div class="examples">${examples.map((x) => `<button class="chip" data-q="${esc(x)}">${esc(x)}</button>`).join("")}</div>
      <div class="facts"><div>기본 Claude Sonnet 5.5: 질문 한 번에 20초 안팎·50원 안팎. Haiku 5.5는 더 빠르고 몇 원. GPT-6.1 Sol·Terra도 고를 수 있습니다.</div></div></div>`;
    $("#detail").innerHTML = `<div class="panel empty"><b>추천을 누르면 여기에 상세가 나옵니다</b>추천 이유와 주의점을 보고, 열어서 정의·단위·기간을 직접 확인하세요.</div>`;
  }

  async function runAi(q) {
    st.aiRes = null;
    renderAiBrief(null);
    $("#results").innerHTML = `<div class="loading">AI가 '${esc(q.length > 40 ? q.slice(0, 40) + "…" : q)}'에 맞는 자료를 찾는 중(10~30초)</div>`;
    let res;
    try {
      const model = ($("#aiModel") || {}).value || null;
      res = await api("/api/ai/recommend", { method: "POST", body: JSON.stringify({ question: q, model }) });
    } catch (e) {
      $("#results").innerHTML = `<div class="welcome">${alertBox(e)}</div>`;
      return;
    }
    if (st.lastQuery !== q) return;
    st.results = res.recommendations;
    st.aiRes = res;
    renderAi(res);
  }

  // 오른쪽 위 'AI 추천 설명'(목록에서 항목을 눌러도 그대로 남음, 접기 가능)
  function renderAiBrief(res) {
    const el = $("#aiBrief");
    if (!el) return;
    if (!res) { el.hidden = true; el.innerHTML = ""; return; }
    const u = res.usage || {};
    const searched = (res.searches || []).map((s) => `${esc(s.query)}(${s.found})`).join(" · ");
    el.innerHTML = `<div class="ab-head"><h3>AI 추천 설명</h3>
        <button type="button" class="btn btn-sm btn-ghost" id="abFold">${st.abFolded ? "펼치기" : "접기"}</button></div>
      <div class="ab-body">
        <div class="ab-und"><b>이렇게 이해했습니다</b> ${esc(res.understanding || "")}</div>
        ${res.summary ? `<div class="ab-sum">${esc(res.summary)}</div>` : ""}
        ${res.clarify ? `<div class="note warn">더 정확히 하려면: ${esc(res.clarify)}</div>` : ""}
        ${res.missing ? `<div class="ab-row"><b>이 색인에 없는 자료·찾아볼 곳</b>${esc(res.missing)}</div>` : ""}
        <div class="ab-meta">찾아본 검색어 ${searched} → 후보 ${fmtInt(res.candidates)}개 중 ${fmtInt(res.recommendations.length)}개 추천
          · AI ${esc(res.model_label || res.model)} · ${esc(res.elapsed_s)}초 · 토큰 ${fmtInt(u.input_tokens || 0)}+${fmtInt(u.output_tokens || 0)}${u.usd_estimate != null ? ` · 약 $${esc(u.usd_estimate)}` : ""}</div>
      </div>`;
    el.classList.toggle("folded", !!st.abFolded);
    el.hidden = false;
  }

  function renderAi(res) {
    const items = res.recommendations.map((h, i) => {
      const x = h.extra || {};
      const span = h.source === "DATAGO" ? (x.modified ? `수정 ${x.modified}` : "")
        : h.span ? `${h.span[0].slice(0, 4)}–${h.span[1].slice(0, 4)}` : (h.end_year ? `갱신 ${h.end_year}` : "");
      return `<button class="hit src-${h.source}" data-i="${i}">
        <div class="t"><span class="ai-rank">${h.rank}</span>${esc(h.name)}</div>
        <div class="m"><span class="tag ${h.source}">${esc(SRC_ORG[h.source] || h.source)} ${esc(h.org_name || "")}</span>
          ${h.source === "DATAGO" ? `<span class="tag">${esc(DG_KIND[x.kind] || x.kind || "")}${x.fmt ? " " + esc(x.fmt) : ""}</span>` : ""}
          ${h.cycle_labels && h.cycle_labels.length ? `<span class="tag">${h.cycle_labels.join(" ")}</span>` : ""}
          ${span ? `<span class="tag plain">${esc(span)}</span>` : ""}${h.regional ? `<span class="tag intl">지자체 통계</span>` : ""}</div>
        ${h.ai_reason ? `<div class="ai-why">${esc(h.ai_reason)}</div>` : ""}
        ${h.ai_caution ? `<div class="ai-caution">확인: ${esc(h.ai_caution)}</div>` : ""}</button>`;
    }).join("");
    const head = `<div class="results-head"><h2><b>${fmtInt(res.recommendations.length)}</b>개 추천</h2></div>`;
    $("#results").innerHTML = head + (items ? `<div class="list">${items}</div>` : `<div class="welcome"><p>추천할 만한 자료를 찾지 못했습니다. 표현을 바꾸거나 '통계'에서 검색어로 찾아 보세요.</p></div>`);
    renderAiBrief(res);
  }

  function renderDartWelcome() {
    const d = (st.status && st.status.dart) || {};
    const examples = ["삼성전자", "LG전자", "005930", "현대자동차", "네이버", "하이트진로"];
    const state = d.count ? `회사 목록 <b>${fmtInt(d.count)}개</b>(상장 ${fmtInt(d.listed)}개), ${esc(d.at || "")} 받음`
      : d.refreshing ? "회사 목록을 받는 중입니다(처음 한 번, 1분 안팎)." : d.error ? `회사 목록을 받지 못했습니다: ${esc(d.error.message)}` : "회사 목록이 아직 없습니다. 검색하면 받아 옵니다.";
    $("#results").innerHTML = `<div class="welcome">
      <h1>전자공시(DART) 회사 재무제표와 공시 목록</h1>
      <p>회사 이름이나 종목코드로 찾아, 여러 해의 재무제표를 계정 × 연도 표로 받습니다. 공시 목록도 원문 링크와 함께 받을 수 있습니다.</p>
      <div class="examples">${examples.map((x) => `<button class="chip" data-q="${esc(x)}">${esc(x)}</button>`).join("")}</div>
      <div class="facts"><div>${state}</div>
        <div>회사 이름 검색은 이 PC 안에서만 합니다. 금감원(OpenDART)에는 고른 회사의 고유번호와 연도만 보냅니다.</div></div></div>`;
    $("#detail").innerHTML = `<div class="panel empty"><b>왼쪽에서 회사를 고르면 여기에 나옵니다</b>기업개황을 확인하고, 재무제표(연결·별도, 연간·분기)나 공시 목록을 미리 본 뒤 내려받습니다.</div>`;
  }

  async function runCorpSearch(q) {
    $("#results").innerHTML = `<div class="loading">'${esc(q)}' 회사 찾는 중</div>`;
    let res;
    try {
      res = await api(`/api/dart/search?${new URLSearchParams({ q, listed: st.listedOnly ? 1 : 0 })}`);
    } catch (e) {
      $("#results").innerHTML = `<div class="welcome">${alertBox(e)}</div>`;
      return;
    }
    if (st.status && res.corps) st.status.dart = { ...(st.status.dart || {}), ...res.corps };
    st.corps = res.results;
    const more = res.count > res.results.length ? ` <span class="note">중 앞의 ${fmtInt(res.results.length)}개. 이름을 더 정확히 쓰거나 '상장사만'을 켜 보세요.</span>` : "";
    const groups = (res.groups || []).length ? `<div class="ai-box"><div><b>기업집단</b> ${res.groups.map((g) =>
      `<button class="chip" type="button" data-egroup="${esc(g.code)}">${esc(g.name)} · ${fmtInt(g.n_comp || 0)}개사${g.asset_rank ? ` · 자산 ${g.asset_rank}위` : ""}</button>`).join(" ")}</div>
      <div class="note">공정위 지정 대규모기업집단(${esc(res.groups[0].ym_label)} 기준). 누르면 소속회사·변경내역을 봅니다.</div></div>` : "";
    const head = `<div class="results-head"><h2><b>${fmtInt(res.count)}</b>개 회사${more}</h2></div>` + groups;
    if (!res.results.length) {
      $("#results").innerHTML = head + `<div class="welcome"><p>'${esc(res.query)}'에 맞는 회사가 없습니다. '(주)'·'주식회사' 없이 이름만 넣거나 종목코드 6자리로 찾아 보세요.</p></div>`;
      return;
    }
    $("#results").innerHTML = head + `<div class="list">${res.results.map((c, i) => `
      <button class="hit src-DART${st.corp && st.corp.corp_code === c.corp_code ? " sel" : ""}" data-corp="${i}">
        <div class="t">${esc(c.name)}</div>
        <div class="m">${c.listed ? `<span class="tag DART">상장 ${esc(c.stock_code)}</span>` : c.stock_code ? `<span class="tag off">상장폐지 등(옛 종목코드 ${esc(c.stock_code)})</span>` : `<span class="tag plain">비상장</span>`}
          ${c.market ? `<span class="tag">${esc(c.market)}</span>` : ""}<span class="tag plain">고유번호 ${esc(c.corp_code)}</span></div>
        ${c.eng_name ? `<div class="p">${esc(c.eng_name)}</div>` : ""}</button>`).join("")}</div>`;
  }

  async function openCorp(c) {
    st.corp = c; st.company = null; st.preview = null; st.previewErr = null;
    document.querySelectorAll(".hit[data-corp]").forEach((b) => b.classList.toggle("sel", st.corps[+b.dataset.corp] === c));
    $("#detail").innerHTML = `<div class="panel"><div class="loading">기업개황을 받는 중</div></div>`;
    let d;
    try {
      d = await api(`/api/dart/company?corp=${encodeURIComponent(c.corp_code)}`);
    } catch (e) {
      $("#detail").innerHTML = `<div class="panel">${alertBox(e)}</div>`;
      return;
    }
    if (st.corp !== c) return;
    st.company = d;
    const lastAnnual = today.getFullYear() - 1;
    st.fs = st.fs || { scope: "key", fs_div: "CFS", reprt: "11011", flow: "cum", start: String(lastAnnual - 4), end: String(lastAnnual), sj: new Set(["BS", "IS", "CIS", "CF"]) };
    const y1 = new Date(today); y1.setFullYear(today.getFullYear() - 1);
    st.dlist = st.dlist || { start: iso(y1), end: iso(today) };
    st.dtab = st.dtab || "fs";
    renderCompany();
    writeHash();
  }

  function renderCompany() {
    const d = st.company;
    const facts = [["회사 고유번호", d.corp_code], ["종목코드", d.stock_code || "비상장"], ["시장", d.market], ["대표자", d.ceo],
      ["업종코드(KSIC)", d.induty_code], ["설립일", d.est_dt], ["결산월", d.acc_mt ? `${d.acc_mt}월` : ""]]
      .map(([k, v]) => `<div class="fact"><div class="k">${esc(k)}</div><div class="v">${esc(v || "-")}</div></div>`).join("")
      + egroupFact(d);
    $("#detail").innerHTML = `
      <div class="panel">
        <div class="d-head"><div><span class="tag DART">${esc(d.source_label)}</span>
          <h2>${esc(d.name)}</h2>${d.eng_name ? `<div class="d-path">${esc(d.eng_name)}</div>` : ""}</div>
</div>
        <div class="facts-grid">${facts}</div>
      </div>
      <div class="panel" id="queryPanel"></div>
      <div class="panel" id="previewPanel"><div class="empty"><b>미리보기</b>조건을 고르고 '미리보기'를 누르세요.</div></div>`;
    renderDartQuery();
  }

  function egroupFact(d) {      // 공정위 지정 기업집단 소속(법인등록번호로 찾음)
    const eg = d.egroup, info = (st.status && st.status.egroup) || {};
    if (!info.latest) return "";
    const v = eg ? `${esc(eg.group_name)}${eg.current ? "" : ` <small>(${esc(eg.ym_label)}까지, 지금은 지정 소속 아님)</small>`}
        <small>계열편입 ${esc(eg.joined || "-")}</small>
        <button class="btn btn-sm btn-ghost" type="button" data-egroup="${esc(eg.group_code || "")}" data-egym="${esc(eg.ym)}">집단 보기</button>`
      : `<small>지정 기업집단 소속 아님(${esc(info.latest.slice(0, 4))}-${esc(info.latest.slice(4, 6))} 공정위 지정 기준)</small>`;
    return `<div class="fact"><div class="k">소속 기업집단</div><div class="v">${v}</div></div>`;
  }

  // ------------------------------------------------------------ 기업집단(공정위 기업집단포털)
  async function openEgroup(code, ym) {
    $("#detail").innerHTML = `<div class="panel"><div class="loading">기업집단 정보를 여는 중</div></div>`;
    let g;
    try {
      g = await api(`/api/egroup/group?code=${encodeURIComponent(code)}${ym ? `&ym=${encodeURIComponent(ym)}` : ""}`);
    } catch (e) {
      $("#detail").innerHTML = `<div class="panel">${alertBox(e)}</div>`;
      return;
    }
    st.egroup = g;
    renderEgroup();
  }

  function trillion(v) {        // 자산총액(10억원, 쉼표 표기) → '695.8조원'
    const n = Number(String(v || "").replace(/,/g, ""));
    return Number.isFinite(n) && n > 0 ? `${(n / 1000).toLocaleString("ko-KR", { maximumFractionDigits: 1 })}조원` : "-";
  }

  function renderEgroup() {
    const g = st.egroup, gg = g.group;
    const facts = [["기업집단코드", gg.code], ["동일인", gg.owner], ["대표회사", gg.rep], ["소속회사 수", gg.n_comp != null ? `${fmtInt(gg.n_comp)}개` : "-"],
      ["구분", [gg.limit_type, gg.cls].filter(Boolean).join(" · ")], ["자산총액", `${trillion(gg.asset_total)}${gg.asset_rank ? ` (${gg.asset_rank}위)` : ""}`]]
      .map(([k, v]) => `<div class="fact"><div class="k">${esc(k)}</div><div class="v">${esc(v || "-")}</div></div>`).join("");
    const ymSel = `<select id="egYm" aria-label="공개년월">${g.yms.map((y) => `<option value="${esc(y.ym)}" ${y.ym === gg.ym ? "selected" : ""}>${esc(y.label)}</option>`).join("")}</select>`;
    const affs = g.affiliates.map((a) => `<tr><td>${a.dart ? `<button class="btn btn-sm btn-ghost" type="button" data-egcorp="${esc(a.dart.corp_code)}" data-egname="${esc(a.company)}">${esc(a.company)}</button>` : esc(a.company)}${a.dart && a.dart.listed ? ` <small class="sub">상장 ${esc(a.dart.stock_code)}</small>` : ""}</td>
        <td class="nowrap">${esc(a.jurirno)}</td><td>${esc(a.ceo)}</td><td class="nowrap">${esc(a.founded)}</td><td class="nowrap">${esc(a.joined)}</td></tr>`).join("");
    const ch = g.changes.slice(0, 300).map((c) => `<tr><td class="nowrap">${esc(c.day)}</td><td class="nowrap">${esc(c.change)}</td><td>${esc(c.company)}</td><td>${esc(c.reason)}</td></tr>`).join("");
    const dl = (kind, label) => `<button class="btn btn-sm" type="button" data-egdl="${kind}" data-fmt="xlsx">${label} Excel</button>
      <button class="btn btn-sm btn-ghost" type="button" data-egdl="${kind}" data-fmt="csv">CSV</button>`;
    $("#detail").innerHTML = `
      <div class="panel">
        <div class="d-head"><div><span class="tag DART">공정거래위원회 기업집단포털</span>
          <h2>${esc(gg.name)}</h2><div class="d-path">공정위 지정 대규모기업집단 · 공개년월 ${ymSel}</div></div>
          <a class="btn btn-sm" href="https://www.egroup.go.kr" target="_blank" rel="noopener">기업집단포털 열기</a></div>
        <div class="facts-grid">${facts}</div>
        <div class="dg-actions">${dl("affiliates", "소속회사")} ${dl("changes", "변경내역")} ${dl("groups", "그해 전체 집단 목록")}</div>
      </div>
      <div class="panel"><div class="pv-head"><h3>소속회사 ${fmtInt(g.affiliates.length)}곳 <small class="note">${esc(gg.ym_label)} 기준</small></h3></div>
        <div class="note">DART에서 찾은 회사(법인등록번호가 같거나, 이름이 DART 회사 목록에 하나뿐인 회사)는 이름을 눌러 회사(DART) 화면으로 갑니다.</div>
        <div class="tbl-wrap"><table class="data list"><thead><tr><th>회사명</th><th>법인등록번호</th><th>대표자</th><th>설립일</th><th>계열편입일</th></tr></thead><tbody>${affs}</tbody></table></div></div>
      <div class="panel"><div class="pv-head"><h3>계열 편입·제외·유예 변경내역 ${fmtInt(g.changes.length)}건</h3></div>
        ${g.changes.length > 300 ? `<div class="note">최근 300건만 보입니다. 전체는 '변경내역 Excel'로 받으세요.</div>` : ""}
        <div class="tbl-wrap"><table class="data list"><thead><tr><th>일자</th><th>구분</th><th>회사명</th><th>사유</th></tr></thead><tbody>${ch}</tbody></table></div>
        ${(g.notes || []).map((n) => `<div class="note" style="margin-top:8px">${esc(n)}</div>`).join("")}</div>`;
  }

  function segHtml(attr, opts, cur) {
    return `<div class="seg" role="group">${opts.map(([v, l]) => `<button type="button" data-${attr}="${v}" class="${v === cur ? "on" : ""}">${l}</button>`).join("")}</div>`;
  }

  function renderDartQuery() {
    const tabs = `<div class="tabs" role="tablist"><button type="button" data-dtab="fs" class="${st.dtab === "fs" ? "on" : ""}">재무제표</button>
      <button type="button" data-dtab="list" class="${st.dtab === "list" ? "on" : ""}">공시 목록</button>
      <button type="button" data-dtab="ind" class="${st.dtab === "ind" ? "on" : ""}">업종 비교</button></div>`;
    let body;
    if (st.dtab === "fs") {
      const f = st.fs;
      body = `<div class="q-row">
          <div class="field"><span>범위</span>${segHtml("scope", [["key", "주요계정"], ["all", "전체 계정"]], f.scope)}</div>
          <div class="field"><span>구분</span>${segHtml("fsdiv", [["CFS", "연결"], ["OFS", "별도"]], f.fs_div)}</div>
          <div class="field"><span>보고서</span>${segHtml("reprt", REPRT, f.reprt)}</div>
          ${f.reprt !== "11011" ? `<div class="field"><span>손익 기간</span>${segHtml("flow", [["cum", "누적(사업연도 시작부터)"], ["q", "해당 분기 3개월"]], f.flow)}</div>` : ""}
        </div>
        <div class="q-row">
          <div class="field"><span>사업연도 시작</span><input type="text" id="fsStart" value="${esc(f.start)}" placeholder="2021" inputmode="numeric"></div>
          <div class="field"><span>끝</span><input type="text" id="fsEnd" value="${esc(f.end)}" placeholder="2025" inputmode="numeric"></div>
          <div class="quick"><button type="button" class="btn btn-sm btn-ghost" data-fsspan="3">최근 3년</button>
            <button type="button" class="btn btn-sm btn-ghost" data-fsspan="5">5년</button>
            <button type="button" class="btn btn-sm btn-ghost" data-fsspan="10">10년</button></div>
        </div>
        ${f.scope === "all" ? `<div class="sj-picks">${SJ.map(([k, l]) => `<label class="check"><input type="checkbox" data-sj="${k}" ${f.sj.has(k) ? "checked" : ""}> ${l}</label>`).join("")}</div>`
          : `<div class="note">주요계정: 자산·부채·자본총계, 매출액, 영업이익, 당기순이익 등 표준 계정 15개 안팎. 이름이 해마다 같아 연도 비교가 쉽습니다. 세부 계정이 필요하면 '전체 계정'을 고르세요.</div>`}
        <div class="q-foot"><div class="est">2015년 이후 사업연도, 한 번에 12개 연도까지. 금액은 원 단위 원천 값 그대로 받습니다.</div>
          <button class="btn btn-primary" id="btnDart" type="button">미리보기</button></div>`;
    } else if (st.dtab === "ind") {
      body = indQueryHtml();
    } else {
      const l = st.dlist;
      body = `<div class="q-row">
          <div class="field"><span>시작</span><input type="text" id="dlStart" value="${esc(l.start)}" placeholder="2025-01-01"></div>
          <div class="field"><span>끝</span><input type="text" id="dlEnd" value="${esc(l.end)}" placeholder="2026-12-31"></div>
          <div class="quick"><button type="button" class="btn btn-sm btn-ghost" data-dlspan="3">최근 3개월</button>
            <button type="button" class="btn btn-sm btn-ghost" data-dlspan="12">1년</button>
            <button type="button" class="btn btn-sm btn-ghost" data-dlspan="36">3년</button></div>
        </div>
        <div class="q-foot"><div class="est">최신순으로 최대 2,000건. 보고서명을 누르면 DART 원문이 열립니다.</div>
          <button class="btn btn-primary" id="btnDart" type="button">미리보기</button></div>`;
    }
    $("#queryPanel").innerHTML = tabs + body;
  }

  function dartSpec() {
    const c = st.company;
    if (st.dtab === "fs") {
      const f = st.fs;
      return { source: "DART", kind: "fs", id: c.corp_code, scope: f.scope, fs_div: f.fs_div, reprt: f.reprt, flow: f.reprt === "11011" ? "cum" : f.flow,
        start: f.start, end: f.end, cycle: "A", sel: { SJ: f.scope === "key" ? ["BS", "IS"] : SJ.map(([k]) => k).filter((k) => f.sj.has(k)) } };
    }
    if (st.dtab === "ind") {
      const n = st.ind;
      return { source: "DART", kind: "industry", id: n.prefix, start: n.year, end: n.year, reprt: n.reprt, fs_div: n.fs_div, cycle: "A", sel: {} };
    }
    return { source: "DART", kind: "list", id: c.corp_code, start: st.dlist.start, end: st.dlist.end, cycle: "D", sel: {} };
  }

  // 업종 비교: 회사 업종코드의 단계(세세분류 → 중분류)를 고르고, 같은 업종 상장사의 주요계정을 한 표로
  async function loadInd() {
    const c = st.company;
    try {
      const r = await api(`/api/dart/industry?corp=${encodeURIComponent(c.corp_code)}`);
      if (st.company !== c) return;
      const prev = st.ind && st.ind.corp === c.corp_code ? st.ind : null;
      const pick = (r.levels.find((l) => l.count >= 3) || r.levels[0] || {}).code;
      st.ind = { corp: c.corp_code, code: r.code, levels: r.levels, info: r.info, job: r.job,
        prefix: prev ? prev.prefix : pick, year: prev ? prev.year : String(today.getFullYear() - 1),
        reprt: prev ? prev.reprt : "11011", fs_div: prev ? prev.fs_div : "CFS" };
    } catch (e) {
      st.ind = { corp: c.corp_code, error: e };
    }
    if (st.dtab === "ind") renderDartQuery();
    clearTimeout(loadInd._t);
    if (st.ind && st.ind.job && st.ind.job.running) loadInd._t = setTimeout(() => { if (st.dtab === "ind") loadInd(); }, 3000);
  }

  function indQueryHtml() {
    const n = st.ind;
    if (!n || n.corp !== (st.company || {}).corp_code) { loadInd(); return `<div class="loading">업종 정보를 확인하는 중</div>`; }
    if (n.error) return alertBox(n.error);
    const info = n.info || {}, job = n.job || {};
    const cover = info.listed ? info.with_code / info.listed : 0;
    let setup = "";
    if (job.running) {
      setup = `<div class="note warn">상장사 업종코드를 받는 중입니다: ${fmtInt(job.done)} / ${fmtInt(job.total || info.listed)}곳. 끝나면 이 화면이 저절로 바뀝니다.</div>`;
    } else if (cover < 0.9) {
      setup = `<div class="note warn">업종 비교에는 상장사 업종코드가 필요합니다(지금 ${fmtInt(info.with_code)} / ${fmtInt(info.listed)}곳).
        처음 한 번 받으세요. 상장사마다 기업개황을 받아 15분 안팎 걸립니다.
        ${job.error ? `<br>지난번 실패: ${esc(job.error.message)}` : ""}</div>
        <div style="margin-top:8px"><button class="btn" id="btnIndRefresh" type="button">상장사 업종코드 받기</button></div>`;
    }
    if (!n.code) return `<div class="note">OpenDART 기업개황에 이 회사의 업종코드가 없습니다.</div>${setup}`;
    const levels = n.levels.map((l) => `<label class="check" style="display:flex;margin:4px 0"><input type="radio" name="indlv" data-indlv="${esc(l.code)}" ${l.code === n.prefix ? "checked" : ""}>
        <span><b>${esc(l.code)}</b> ${esc(l.name || "(이름 없음)")} <span class="note">${esc(l.level)}, 상장사 ${fmtInt(l.count)}곳</span></span></label>`).join("");
    return `${setup}
      <div class="field" style="margin-top:12px"><span>비교할 업종(KSIC, 이 회사 업종코드 ${esc(n.code)})</span><div>${levels}</div></div>
      <div class="q-row">
        <div class="field"><span>사업연도</span><input type="text" id="indYear" value="${esc(n.year)}" inputmode="numeric"></div>
        <div class="field"><span>보고서</span>${segHtml("indreprt", REPRT, n.reprt)}</div>
        <div class="field"><span>재무제표</span>${segHtml("indfs", [["CFS", "연결 우선"], ["OFS", "별도 우선"]], n.fs_div)}</div>
      </div>
      <div class="q-foot"><div class="est">같은 업종코드로 시작하는 상장사의 매출액·영업이익·당기순이익·자산·부채·자본. 100곳마다 호출 1번.</div>
        <button class="btn btn-primary" id="btnDart" type="button" ${n.prefix ? "" : "disabled"}>미리보기</button></div>`;
  }

  async function runDartPreview() {
    const spec = dartSpec();
    const panel = $("#previewPanel");
    if (spec.kind === "fs" && !spec.sel.SJ.length) { toast("재무제표를 하나 이상 고르세요"); return; }
    panel.innerHTML = `<div class="loading">OpenDART에서 자료를 받는 중</div>`;
    const btn = $("#btnDart");
    if (btn) btn.disabled = true;
    try {
      st.preview = await api("/api/preview", { method: "POST", body: JSON.stringify(spec) });
      st.previewErr = null;
      st.previewSpec = spec;
      st.pivot = null;
    } catch (e) {
      st.preview = null;
      st.previewErr = e;
    }
    if (btn) btn.disabled = false;
    renderDartPreview();
  }

  function fmtAmt(v, unit) {
    if (v == null || v === "") return "";
    const n = Number(String(v).replace(/,/g, ""));
    if (!Number.isFinite(n)) return esc(v);
    const div = unit === "원" ? UNIT_DIV[st.dunit] : 1;
    return (n / div).toLocaleString("ko-KR", { maximumFractionDigits: div === 1 ? 6 : 0 });
  }

  function renderDartPreview() {
    const panel = $("#previewPanel");
    if (!panel) return;
    if (st.previewErr) { panel.innerHTML = `<div class="pv-head"><h3>미리보기</h3></div>${alertBox(st.previewErr)}`; return; }
    const p = st.preview;
    if (!p) return;
    const inCart = st.cart.some((c) => sameSpec(c.spec, st.previewSpec));
    const isTable = p.layout === "table";
    let table, sum;
    if (p.kind === "industry") {
      const t = p.table, me = st.company ? st.company.corp_code : "";
      sum = `<span>상장사 <b>${fmtInt(p.row_count)}</b>곳</span><span>${esc(p.cycle_label)} <b>${esc(p.start)}</b></span>`;
      const amt = (v) => fmtAmt(v, "원");
      const rows = t.rows.map((r) => `<tr${r[3] === me ? ' class="me"' : ""}><td class="nowrap">${esc(r[0])}</td><td>${esc(r[1])}<small class="sub">${esc(r[6] || "")} ${esc(r[5] || "")}</small></td>
        <td class="nowrap">${esc(r[7])}</td>${[10, 11, 12, 13, 16].map((i) => `<td class="num">${i === 16 ? esc(r[i] ?? "") : amt(r[i])}</td>`).join("")}</tr>`).join("");
      table = `<div class="q-row" style="margin:8px 0"><div class="field"><span>화면 표시 단위</span>${segHtml("dunit", [["원", "원"], ["백만원", "백만원"], ["억원", "억원"]], st.dunit)}</div>
          <span class="note">파일은 원 단위 원천 값 그대로, 부채·자본총계와 손익 기간·접수번호도 파일에 있습니다.</span></div>
        <div class="tbl-wrap"><table class="data list ind"><thead><tr><th>순위</th><th>회사</th><th>재무제표</th><th>매출액</th><th>영업이익</th><th>당기순이익</th><th>자산총계</th><th>매출 비중(%)</th></tr></thead><tbody>${rows}</tbody></table></div>`;
    } else if (isTable) {
      const t = p.table;
      sum = `<span>공시 <b>${fmtInt(p.row_count)}</b>건</span><span>기간 <b>${esc(p.start)} ~ ${esc(p.end)}</b></span>`;
      const rows = t.rows.slice(0, 500).map((r) => `<tr><td class="nowrap">${esc(r[0])}</td><td><a href="${esc(r[6])}" target="_blank" rel="noopener">${esc(r[1])}</a></td>
        <td>${esc(r[2])}</td><td class="nowrap">${esc(r[3])}</td><td>${esc(r[4])}</td></tr>`).join("");
      table = `<div class="tbl-wrap"><table class="data list"><thead><tr><th>접수일자</th><th>보고서명</th><th>제출인</th><th>접수번호</th><th>비고</th></tr></thead><tbody>${rows}</tbody></table></div>
        ${t.rows.length > 500 ? `<div class="tbl-note">앞의 500건만 보입니다. 전체는 내려받은 파일에 있습니다.</div>` : ""}`;
    } else {
      const grid = new Map();
      p.rows.forEach(([si, t, v]) => { if (!grid.has(si)) grid.set(si, {}); grid.get(si)[t] = v; });
      let lastSj = null;
      const body = p.series.map((s, i) => {
        const sj = s.codes.SJ;
        const g = sj !== lastSj ? `<tr class="grp"><td colspan="${p.times.length + 1}">${esc(s.names["재무제표"])}</td></tr>` : "";
        lastSj = sj;
        const vals = p.times.map((t) => `<td>${fmtAmt((grid.get(i) || {})[t], s.unit)}</td>`).join("");
        return `${g}<tr><td class="acc">${esc(s.names["계정"])}${s.names["세부"] ? `<small>${esc(s.names["세부"])}</small>` : ""}${s.unit && s.unit !== "원" ? `<small>단위 ${esc(s.unit)}</small>` : ""}</td>${vals}</tr>`;
      }).join("");
      sum = `<span>계정 <b>${fmtInt(p.series.length)}</b>개</span><span>${esc(p.cycle_label)} <b>${esc(p.start)} ~ ${esc(p.end)}</b></span>
        <span>값 <b>${fmtInt(p.row_count)}</b>개</span>${p.url ? `<span><a href="${esc(p.url)}" target="_blank" rel="noopener">최근 보고서 원문</a></span>` : ""}`;
      const unitOf = (p.series[0] || {}).unit;
      table = `<div class="q-row" style="margin:8px 0"><div class="field"><span>화면 표시 단위</span>${segHtml("dunit", [["원", "원"], ["백만원", "백만원"], ["억원", "억원"]], st.dunit)}</div>
          <span class="note">표시만 바뀝니다. 파일은 원 단위 원천 값 그대로입니다.</span></div>
        ${pivotControl(p)}
        ${st.pivot ? pivotTableHtml(p, (v) => fmtAmt(v, unitOf)) : `<div class="tbl-wrap"><table class="data fs"><thead><tr><th>계정</th>${p.times.map((t) => `<th>${esc(t)}</th>`).join("")}</tr></thead><tbody>${body}</tbody></table></div>`}`;
    }
    const warn = (p.warnings || []).map((w) => `<div class="note warn">${esc(w)}</div>`).join("");
    const notes = (p.notes || []).map((n) => `<div class="kv">${esc(n)}</div>`).join("");
    const extra = (p.extra_prov || []).map(([k, v]) => `<div class="kv"><b>${esc(k)}</b>${esc(v)}</div>`).join("");
    panel.innerHTML = `
      <div class="pv-head"><div><h3>미리보기</h3>
        <div class="pv-sum">${sum}<span>수집 <b>${esc(p.fetched_at)}</b></span></div></div>
        <div class="actions">
          <button class="btn btn-strong" data-dl="xlsx" type="button">Excel 받기</button>
          ${isTable ? "" : `<select id="csvShape" aria-label="CSV 모양"><option value="wide">CSV 가로형(화면 배치대로)</option><option value="long">CSV 세로형</option></select>`}
          <button class="btn" data-dl="csv" type="button">CSV 받기</button>
          <button class="btn" id="btnCart" type="button" ${inCart ? "disabled" : ""}>${inCart ? "목록에 담김" : "다운로드 목록에 담기"}</button>
        </div></div>
      ${warn}${table}
      <details class="more prov"><summary>출처·조회 조건 기록</summary><div class="body">
        <div class="kv"><b>선택</b>${esc(p.selection_text)}</div>${notes}${extra}
        <div class="kv"><b>API 호출 ${p.requests.length}건(인증키 가림)</b>${p.requests.slice(0, 12).map((u) => `<code>${esc(u)}</code>`).join("")}${p.requests.length > 12 ? `<div>외 ${p.requests.length - 12}건</div>` : ""}</div>
      </div></details>`;
  }

  function rerenderPreview() { if (st.mode === "dart") renderDartPreview(); else renderPreview(); }

  // ------------------------------------------------------------ 공정위 의결서(이 프로그램 안 색인에서만 찾음)
  const DEC_EXAMPLES = ["자진신고 감경", "\"관련매출액\" 산정", "재판매가격유지", "기업결합 시장획정", "하도급대금 부당 감액", "부당지원 정상가격", "플랫폼 자사우대"];
  const segMark = (segs) => (segs || []).map(([s, h]) => (h ? `<mark>${esc(s)}</mark>` : esc(s))).join("");
  const decInfo = () => (st.status && st.status.decision) || {};
  function decHasFilter() { const d = st.dec; return !!(d.type || d.act || d.y1 || d.y2); }
  function decParams(extra = {}) {
    const d = st.dec;
    return new URLSearchParams({ q: st.lastQuery || "", type: d.type, act: d.act, y1: d.y1 || 0, y2: d.y2 || 0, sort: d.sort, ...extra });
  }

  function fillDecFilters() {            // 위반유형·조치 고르는 칸(괄호 안은 전체 건수)
    const info = decInfo(), t = $("#decType"), a = $("#decAct");
    if (info.available && t.options.length <= 1) {
      t.insertAdjacentHTML("beforeend", info.types.map(([n, c]) => `<option value="${esc(n)}">${esc(n)} (${fmtInt(c)})</option>`).join(""));
      a.insertAdjacentHTML("beforeend", info.acts.filter(([n]) => n).map(([n, c]) => `<option value="${esc(n)}">${esc(n)} (${fmtInt(c)})</option>`).join(""));
    }
    syncDecFilters();
  }
  function syncDecFilters() {
    const d = st.dec;
    $("#decType").value = d.type; $("#decAct").value = d.act;
    $("#decY1").value = d.y1; $("#decY2").value = d.y2;
    document.querySelectorAll("[data-decsort]").forEach((b) => b.classList.toggle("on", b.dataset.decsort === d.sort));
  }

  function renderDecWelcome() {
    fillDecFilters();
    const info = decInfo(), yrs = info.years || [];
    const missing = !st.status || info.available ? "" : info.loading
      ? `<div class="note">의결서 색인을 불러오는 중입니다(처음 켤 때 몇 초~수십 초). 지금 찾아도 다 읽은 뒤 결과가 나옵니다.</div>`
      : `<div class="note warn">${esc(info.error && info.error !== "missing" ? info.error : "의결서 색인(data/decisions.sqlite)이 없어 이 탭은 꺼져 있습니다. 선택 기능이며 README의 '의결서 탭'을 보세요.")}</div>`;
    $("#results").innerHTML = `<div class="welcome">
      <h1>공정위 의결서를 본문까지 찾습니다</h1>
      <p>사건명뿐 아니라 의결서 본문 전체에서 찾아, 그 말이 나온 쪽과 앞뒤 문장을 보여 줍니다. 쪽마다 본문을 읽고 원문 PDF로 확인하세요.</p>
      ${missing}
      <div class="examples">${DEC_EXAMPLES.map((x) => `<button class="chip" data-q="${esc(x)}">${esc(x)}</button>`).join("")}</div>
      <div class="facts">
        ${info.available ? `<div>의결서 <b>${fmtInt(info.count)}건</b>(본문 ${fmtInt(info.pages)}쪽), ${esc(yrs[0])}–${esc(yrs[1])}년 의결. 공정위 사건처리시스템에서 ${esc((info.built || "").slice(0, 10))}에 모은 것입니다.</div>` : ""}
        <div>띄어 쓴 말은 <b>모두 든</b> 의결서를, <b>"따옴표"</b>는 붙어 있는 구절을, <b>-말</b>은 그 말이 든 의결서를 빼고 찾습니다. 말의 앞부분이 같으면 찾습니다(공동행위 → 공동행위를·공동행위의). 붙여 쓴 말의 가운데(‘부당한공동행위’ 속 ‘공동행위’)는 놓칠 수 있습니다.</div>
        <div>검색어 없이 위반유형·조치·연도만 골라 목록을 볼 수도 있습니다.</div>
        <div>원문 PDF를 열 때만 공정위 사이트에서 그 한 건을 받습니다.</div>
      </div></div>`;
    $("#detail").innerHTML = `<div class="panel empty"><b>왼쪽에서 의결서를 고르면 여기에 나옵니다</b>찾는 말이 나온 쪽을 모아 보여 주고, 쪽마다 본문을 읽을 수 있습니다.</div>`;
  }

  async function runDecSearch() {
    st.decSel = null; st.decDoc = null; st.decPage = null;
    const key = decParams().toString();
    $("#results").innerHTML = `<div class="loading">${st.lastQuery ? `'${esc(st.lastQuery)}'` : "고른 조건으로"} 의결서 찾는 중</div>`;
    let res;
    try {
      res = await api(`/api/decision/search?${decParams({ snip: 0 })}`);
    } catch (e) {
      $("#results").innerHTML = `<div class="welcome">${alertBox(e)}</div>`;
      return;
    }
    if (decParams().toString() !== key) return;          // 그사이 다른 검색을 했으면 버림
    st.decRes = res; st.decList = res.results;
    renderDecResults();
    fillSnippets(res.results);
  }

  function snipHtml(x) {
    if (x.snippets === undefined) return `<div class="snip wait">앞뒤 문장 불러오는 중</div>`;
    return x.snippets.map((s) => `<div class="snip"><span class="pg">${s.page}쪽</span>${s.pre ? "… " : ""}${segMark(s.segs)}${s.post ? " …" : ""}</div>`).join("");
  }

  async function fillSnippets(list) {    // 목록을 먼저 띄우고 앞뒤 문장은 10건씩 위에서부터 채운다(느린 디스크 대비)
    const q = st.lastQuery;
    const need = list.filter((x) => x.snippets === undefined && x.hit_pages !== undefined);
    for (let i = 0; q && i < need.length; i += 10) {
      const part = need.slice(i, i + 10);
      let r = null;
      try { r = await api(`/api/decision/snippets?${new URLSearchParams({ q, ids: part.map((x) => x.id).join(",") })}`); } catch { r = null; }
      if (st.lastQuery !== q) return;
      part.forEach((x) => {
        x.snippets = (r && r.snippets[String(x.id)]) || [];
        document.querySelectorAll(`[data-snips="${x.id}"]`).forEach((el) => (el.innerHTML = snipHtml(x)));
      });
    }
  }

  function decItemHtml(x, i) {
    const multi = st.decRes.terms.length > 1;
    const bad = x.status !== "정상" ? `<span class="tag off" title="${esc(x.status)}">확인 필요</span>` : "";
    const where = x.hit_pages === undefined ? `${fmtInt(x.pages)}쪽`
      : `${x.title_hit ? "사건명·번호에 있음 · " : ""}${x.hit_pages ? `본문 ${fmtInt(x.hit_pages)}쪽에 나옴${multi ? `(모두 함께 ${fmtInt(x.together_pages)}쪽)` : ""}` : "본문에는 없음"} / 전체 ${fmtInt(x.pages)}쪽`;
    return `<button class="hit src-FTC${st.decSel === x.id ? " sel" : ""}" data-dec="${i}">
      <div class="t">${esc(x.name)}</div>
      <div class="m"><span class="tag FTC">${esc(x.no)}</span><span class="tag plain">${esc(x.date)}</span>
        ${x.types.map((t) => `<span class="tag">${esc(t)}</span>`).join("")}${x.acts.length ? `<span class="tag plain">${esc(x.acts.join(" · "))}</span>` : ""}${bad}</div>
      ${x.hit_pages !== undefined ? `<div data-snips="${x.id}">${snipHtml(x)}</div>` : ""}
      <div class="why">${where}</div></button>`;
  }

  function decMoreHtml() {
    const left = st.decRes.count - st.decList.length;
    return left > 0 ? `<div class="more-wrap"><button class="btn btn-ghost" id="btnDecMore" type="button" style="width:100%">더 보기 (${fmtInt(left)}건 남음)</button></div>` : "";
  }

  function renderDecResults() {
    const res = st.decRes;
    const terms = res.terms.length ? `<span class="lab">찾은 말</span>${res.terms.map((t) => `<b>${esc(t)}</b>`).join(" · ")}${res.terms.length > 1 ? " (모두 든 의결서)" : ""}` : "";
    const excl = res.exclude.length ? `<span class="lab">${terms ? " · " : ""}뺀 말</span>${res.exclude.map(esc).join(", ")}` : "";
    const ign = res.ignored.length ? `<div class="note warn">한 글자이거나 너무 많아 빼고 찾은 말: ${res.ignored.map(esc).join(", ")}</div>` : "";
    const facet = !st.dec.type && res.facets.types.length > 1
      ? `<div class="row"><span class="lab">위반유형</span>${res.facets.types.slice(0, 8).map(([n, c]) => `<button class="chip" type="button" data-dectype="${esc(n)}">${esc(n)} <span class="n">${fmtInt(c)}</span></button>`).join("")}</div>` : "";
    const head = `<div class="results-head"><h2><b>${fmtInt(res.count)}</b>건 의결서 <span class="note">${res.sort === "hits" ? "많이 나온 순" : "최신순"}</span></h2>
      ${terms || excl || ign || facet ? `<div class="expansion">${terms || excl ? `<div class="row">${terms}${excl}</div>` : ""}${ign}${facet}</div>` : ""}</div>`;
    if (!st.decList.length) {
      $("#results").innerHTML = head + `<div class="welcome"><p>맞는 의결서가 없습니다. 말을 줄이거나(앞부분만, 예: 공동행위), 따옴표 구절을 낱말로 나눠 쓰거나, 필터를 풀어 보세요.</p></div>`;
      return;
    }
    $("#results").innerHTML = head + `<div class="list">${st.decList.map(decItemHtml).join("")}</div>${decMoreHtml()}`;
  }

  async function loadDecMore(btn) {      // 다음 30건을 목록 끝에 붙인다(스크롤 위치 유지)
    btn.disabled = true; btn.textContent = "불러오는 중";
    const key = decParams().toString();
    let more;
    try {
      more = await api(`/api/decision/search?${decParams({ snip: 0, offset: st.decList.length })}`);
    } catch (e) {
      toast(`${e.label || "오류"}: ${e.message}`);
      btn.disabled = false; btn.textContent = "더 보기";
      return;
    }
    if (decParams().toString() !== key) return;
    const start = st.decList.length;
    st.decList = st.decList.concat(more.results);
    $("#results .list").insertAdjacentHTML("beforeend", more.results.map((x, j) => decItemHtml(x, start + j)).join(""));
    const wrap = btn.closest(".more-wrap");
    if (wrap) wrap.outerHTML = decMoreHtml();
    fillSnippets(more.results);
  }

  async function openDec(x, page) {
    st.decSel = x.id; st.decDoc = null; st.decPage = null;
    document.querySelectorAll(".hit[data-dec]").forEach((b) => b.classList.toggle("sel", (st.decList[+b.dataset.dec] || {}).id === x.id));
    $("#detail").innerHTML = `<div class="panel"><div class="loading">의결서를 여는 중</div></div>`;
    let d;
    try {
      d = await api(`/api/decision/doc?${new URLSearchParams({ id: x.id, q: decQ() })}`);
    } catch (e) {
      $("#detail").innerHTML = `<div class="panel">${alertBox(e)}</div>`;
      return;
    }
    if (st.decSel !== x.id) return;
    st.decDoc = d;
    renderDecDoc();
    const first = page && d.page_list.includes(page) ? page : d.hits.length ? d.hits[0].page : d.page_list[0];
    if (first) await openDecPage(first, false);
    writeHash();
  }

  function renderDecDoc() {
    const d = st.decDoc, nT = d.terms.length;
    const cases = (d.case_no || "").split(", ").filter(Boolean);      // 한 의결서에 사건번호가 수십 개인 경우가 있어 앞 3개만
    const caseTxt = cases.length > 3 ? `${cases.slice(0, 3).join(", ")} 외 ${cases.length - 3}건` : cases.join(", ");
    const facts = [["의결번호", d.no], ["의결일", d.date], ["사건번호", caseTxt, cases.length > 3 ? cases.join(", ") : ""],
      ["대표위반유형", d.types.join(", ")], ["대표조치유형", d.acts.join(", ")], ["분량", `${fmtInt(d.pages)}쪽`]]
      .map(([k, v, full]) => `<div class="fact"><div class="k">${esc(k)}</div><div class="v"${full ? ` title="${esc(full)}"` : ""}>${esc(v || "-")}</div></div>`).join("");
    const warn = d.status !== "정상" ? `<div class="alert k-bad_request"><b>확인 필요</b>${esc(d.status)}. 공정위 사이트에 올라온 원본 파일의 문제라, 아래 본문이 이 사건 의결서가 아닐 수 있습니다.</div>` : "";
    const together = d.hits.filter((h) => h.together).length;
    const hitList = d.hits.map((h) => `<button type="button" class="dec-hit" data-decpage="${h.page}"><span class="pg">${h.page}쪽</span>${
      nT > 1 ? (h.together ? `<span class="tag FTC">모두</span> ` : `<span class="tag">${esc(h.terms.map((i) => d.terms[i]).join(", "))}</span> `) : ""}${
      h.segs ? `${h.pre ? "… " : ""}${segMark(h.segs)}${h.post ? " …" : ""}` : ""}</button>`).join("");
    const hits = !nT ? "" : `<div class="panel">
        <div class="q-head"><h3>찾는 말이 나온 쪽</h3><span class="note">${d.hits.length ? `${fmtInt(d.hits.length)}쪽${nT > 1 ? ` · 모두 함께 나온 쪽 ${fmtInt(together)}` : ""}`
          : d.title_hit ? "사건명·번호에만 있고 본문에는 없습니다" : "본문에 없습니다"}</span></div>
        ${hitList ? `<div class="dec-hits">${hitList}</div>` : ""}</div>`;
    $("#detail").innerHTML = `
      <div class="panel">
        <div class="d-head"><div><span class="tag FTC">공정위 의결서</span><h2>${esc(d.name)}</h2><div class="d-path">${esc(d.source_label)}</div></div></div>
        <div class="facts-grid">${facts}</div>${warn}
        <div class="dg-actions">
          <a class="btn btn-strong" href="/api/decision/pdf?id=${d.id}" target="_blank" rel="noopener">원문 PDF 열기</a>
          <a class="btn" href="${esc(d.site_url)}" target="_blank" rel="noopener noreferrer">공정위 사이트에서 찾기</a>
        </div>
      </div>
      ${hits}
      <div class="panel" id="decViewer"></div>`;
  }

  async function openDecPage(p, scroll = true) {
    const d = st.decDoc;
    if (!d || !p) return;
    const v = $("#decViewer");
    let r;
    try {
      r = await api(`/api/decision/page?${new URLSearchParams({ id: d.id, p, q: decQ() })}`);
    } catch (e) {
      v.innerHTML = alertBox(e);
      return;
    }
    if (st.decDoc !== d) return;
    st.decPage = r;
    document.querySelectorAll(".dec-hit").forEach((b) => b.classList.toggle("sel", +b.dataset.decpage === r.page));
    renderDecPage();
    if (scroll) v.scrollIntoView({ block: "start", behavior: "smooth" });
    writeHash();
  }

  function renderDecPage() {
    const d = st.decDoc, r = st.decPage, pl = r.page_list, i = pl.indexOf(r.page);
    const hp = d.hits.map((h) => h.page);
    const nextHit = hp.find((p) => p > r.page), prevHit = [...hp].reverse().find((p) => p < r.page);
    const b = (label, p, ok) => `<button class="btn btn-sm" type="button" data-decpage="${ok ? p : ""}" ${ok ? "" : "disabled"}>${label}</button>`;
    $("#decViewer").innerHTML = `<div class="viewer-head">
        <h3>${r.page}쪽 <span class="note">/ ${fmtInt(d.pages)}쪽 · PDF 쪽 번호</span></h3>
        <div class="actions">
          ${b("이전 쪽", pl[i - 1], i > 0)}${b("다음 쪽", pl[i + 1], i >= 0 && i < pl.length - 1)}
          ${hp.length ? b("◀ 나온 쪽", prevHit, prevHit !== undefined) + b("나온 쪽 ▶", nextHit, nextHit !== undefined) : ""}
          <label class="check">쪽 <input type="text" class="yr" id="decGo" inputmode="numeric" value="${r.page}" aria-label="갈 쪽"></label>
          <button class="btn btn-sm btn-ghost" type="button" id="btnDecCite" title="의결번호·의결일·사건명·쪽을 한 줄로 복사">인용 복사</button>
        </div></div>
      <div class="page-text">${segMark(r.segs)}</div>`;
  }

  async function copyText(s) {
    try { await navigator.clipboard.writeText(s); return true; } catch { /* 주소가 https·127.0.0.1이 아니면 막힘 → 옛 방식 */ }
    const ta = document.createElement("textarea");
    ta.value = s; document.body.appendChild(ta); ta.select();
    let ok = false;
    try { ok = document.execCommand("copy"); } catch { ok = false; }
    ta.remove();
    return ok;
  }

  function decRerun() {                  // 필터를 바꾸면 같은 검색어로 다시
    if (st.lastQuery || decHasFilter()) runSearch(st.lastQuery);
    else { st.decRes = null; renderDecWelcome(); writeHash(); }
  }

  // ------------------------------------------------------------ 의결서 AI 질문(근거 쪽을 붙인 답)
  const DEC_AI_EXAMPLES = ["입찰담합에서 조사에 협력한 사업자의 과징금 감경은 어떻게 적용했나요?",
    "온라인 플랫폼 사건에서 관련시장을 어떻게 획정했나요?", "재판매가격유지행위의 정당한 이유를 인정한 사례가 있나요?",
    "기업결합 심사에서 수입 압력을 경쟁제한성 판단에 어떻게 반영했나요?", "하도급대금 감액에 정당한 사유가 있다고 본 예가 있나요?"];
  const DEC_AI_NOTE = "AI 질문은 질문과, 이 프로그램이 고른 의결서 쪽 본문을 고른 AI(Claude·OpenAI)로 보냅니다. 공정위로는 아무것도 가지 않습니다.";
  const DEC_WORD_NOTE = "검색은 이 프로그램 안 색인에서만 합니다(검색어가 밖으로 나가지 않음).";
  const decQ = () => (st.decAi && st.decAiRes ? st.decAiRes.highlight : st.lastQuery) || "";   // 본문 강조·나온 쪽에 쓸 말

  async function loadModelSelect(sel, storeKey, url) {      // AI 모델 고르는 칸(고른 값은 이 브라우저에 기억)
    if (!sel || sel.options.length) return;
    try {
      const r = await api(url);
      let saved = null;
      try { saved = localStorage.getItem(storeKey); } catch { saved = null; }
      const ok = (id) => r.models.some((m) => m.id === id && m.available);
      sel.innerHTML = r.models.map((m) => `<option value="${esc(m.id)}" ${m.available ? "" : "disabled"}>${esc(m.label)}${m.id === r.default ? " · 기본" : ""}${m.available ? "" : " (키 없음)"}</option>`).join("");
      // 기억한 모델이 목록에서 빠졌거나(예: 의결서 질문의 Haiku) 키가 없으면 기본 → 그것도 안 되면 쓸 수 있는 첫 모델
      sel.value = ok(saved) ? saved : ok(r.default) ? r.default : ((r.models.find((m) => m.available) || {}).id || r.default);
    } catch { /* 목록을 못 받아도 서버 기본 모델로 동작 */ }
  }

  function setDecAi(on) {
    st.decAi = on;
    document.querySelectorAll("[data-decmode]").forEach((b) => b.classList.toggle("on", (b.dataset.decmode === "ai") === on));
    $("#decModelWrap").hidden = !on;
    $("#decSortSeg").hidden = on;
    $("#decNote").textContent = on ? DEC_AI_NOTE : DEC_WORD_NOTE;
    $("#q").maxLength = on ? 500 : 200;
    $("#q").placeholder = on ? "의결서에 대해 문장으로 묻기 (예: 입찰담합에서 조사협력 감경은 어떻게 적용했나요?)"
      : "의결서 본문·사건명에서 찾을 말 (예: 자진신고 감경, \"관련매출액\" 산정, 2019서경)";
    if (on) loadModelSelect($("#decModel"), "datahub.decModel", "/api/decision/models");
  }

  function renderDecAiWelcome() {
    fillDecFilters();
    const info = decInfo();
    $("#results").innerHTML = `<div class="welcome">
      <h1>의결서에 대해 문장으로 물어보세요</h1>
      <p>AI가 질문을 의결서 용어로 풀어 이 프로그램 색인에서 관련 쪽을 모으고, 그 쪽에 적힌 내용만으로 답합니다. 문장마다 근거 쪽 번호가 붙고, 누르면 그 쪽을 바로 봅니다.</p>
      ${st.status && !info.available ? `<div class="note warn">의결서 색인이 없거나 불러오는 중이라 지금은 쓸 수 없습니다.</div>` : ""}
      <div class="examples">${DEC_AI_EXAMPLES.map((x) => `<button class="chip" data-q="${esc(x)}">${esc(x)}</button>`).join("")}</div>
      <div class="facts">
        <div>질문 한 번(11/17 실제로 잰 값): 기본 GPT-6.1 Sol 30~55초·약 70원, GPT-6.1 Terra 20~35초·약 30원, Claude Sonnet 5.5 50~95초·약 100원. 11/17 시험에서 근거 대조가 가장 정확했던 것이 Sol이라 기본으로 했습니다.</div>
        <div>위 칸에서 위반유형·조치·연도를 고르면 그 안에서만 찾습니다(고르지 않으면 AI가 질문에서 짐작).</div>
        <div>답은 보낸 쪽(최대 20쪽)에 근거한 요약입니다. 인용 전에 근거 쪽과 원문 PDF를 확인하세요.</div>
      </div></div>`;
    $("#detail").innerHTML = `<div class="panel empty"><b>근거 쪽을 누르면 여기에 나옵니다</b>그 의결서에서 찾는 말이 나온 쪽과 본문을 봅니다.</div>`;
  }

  async function runDecAi(q) {
    st.decAiRes = null; st.decSel = null; st.decDoc = null; st.decPage = null;
    renderAiBrief(null);
    const short = q.length > 40 ? q.slice(0, 40) + "…" : q;
    $("#results").innerHTML = `<div class="loading">AI가 의결서에서 '${esc(short)}'의 근거를 찾는 중(20초~1분 반)</div>`;
    $("#detail").innerHTML = `<div class="panel empty"><b>답을 만드는 중입니다</b>질문 풀기 → 관련 쪽 모으기 → 그 쪽만 보고 답하기</div>`;
    const d = st.dec;
    let res;
    try {
      res = await api("/api/decision/ask", { method: "POST", body: JSON.stringify({ question: q, model: ($("#decModel") || {}).value || null,
        type: d.type, act: d.act, y1: d.y1 || 0, y2: d.y2 || 0 }) });
    } catch (e) {
      $("#results").innerHTML = `<div class="welcome">${alertBox(e)}</div>`;
      $("#detail").innerHTML = "";
      return;
    }
    if (st.lastQuery !== q || !st.decAi) return;
    st.decAiRes = res;
    renderDecAiSources();
    renderDecAiBrief();
    $("#detail").innerHTML = `<div class="panel empty"><b>왼쪽 근거 쪽이나 답 속 번호를 누르세요</b>그 의결서의 해당 쪽 본문이 여기에 나옵니다.</div>`;
  }

  const refChips = (text) => esc(text).replace(/\[(\d{1,3})\]/g, (m, n) => `<button type="button" class="ref" data-decsrc="${n}" title="근거 ${n}번 쪽 보기">${n}</button>`);

  function renderDecAiSources() {
    const r = st.decAiRes, used = r.sources.filter((s) => s.cited).length;
    const head = `<div class="results-head"><h2><b>${fmtInt(r.sources.length)}</b>개 근거 쪽 <span class="note">답에 쓴 것 ${fmtInt(used)}개</span></h2></div>`;
    if (!r.sources.length) {
      $("#results").innerHTML = head + `<div class="welcome"><p>${esc(r.summary)}</p></div>`;
      return;
    }
    $("#results").innerHTML = head + `<div class="list">${r.sources.map((s) => `
      <button class="hit src-FTC${s.cited ? "" : " uncited"}" data-decsrc="${s.n}">
        <div class="t"><span class="ai-rank">${s.n}</span>${esc(s.name)}</div>
        <div class="m"><span class="tag FTC">${esc(s.no)}</span><span class="tag plain">${esc(s.date)}</span><span class="tag">${s.page}쪽</span>
          ${s.cited ? "" : `<span class="tag plain">답에 안 씀</span>`}${s.status !== "정상" ? `<span class="tag off" title="${esc(s.status)}">확인 필요</span>` : ""}</div>
        <div class="snip">${s.pre ? "… " : ""}${segMark(s.segs)}${s.post ? " …" : ""}</div></button>`).join("")}</div>`;
  }

  function renderDecAiBrief() {
    const el = $("#aiBrief"), r = st.decAiRes;
    if (!el || !r) return;
    const u = r.usage || {}, f = r.filters || {};
    const cond = [f.types && f.types.length ? f.types.join("·") : "", f.acts && f.acts.length ? f.acts.join("·") : "",
      f.y1 || f.y2 ? `${f.y1 || ""}~${f.y2 || ""}년` : ""].filter(Boolean).join(", ");
    const condTxt = cond ? `${f.from_user ? "고른 조건" : "AI가 짐작한 조건"}: ${esc(cond)}` : (f.widened ? "AI가 짐작한 조건으로는 없어 조건 없이 찾음" : "조건 없음");
    el.innerHTML = `<div class="ab-head"><h3>AI 답 <span class="note">${r.answerable ? "" : "· 근거 쪽만으로는 충분히 답하지 못함"}</span></h3>
        <button type="button" class="btn btn-sm btn-ghost" id="decAbFold">${st.abFolded ? "펼치기" : "접기"}</button></div>
      <div class="ab-body">
        ${r.understanding ? `<div class="ab-und"><b>이렇게 이해했습니다</b> ${esc(r.understanding)}</div>` : ""}
        <div class="ab-sum">${refChips(r.summary || "")}</div>
        ${r.points.length ? `<ul class="ab-points">${r.points.map((p) => `<li>${refChips(p.text)}</li>`).join("")}</ul>` : ""}
        ${r.gaps ? `<div class="ab-row"><b>더 확인할 것</b>${refChips(r.gaps)}</div>` : ""}
        <div class="note">답은 근거 쪽에 적힌 내용만으로 만든 요약입니다. 인용하기 전에 근거 쪽과 원문 PDF를 확인하세요.</div>
        <div class="ab-meta">찾은 말 ${r.searches.map((s) => `${esc(s.query)}(${s.docs}건)`).join(" · ")} · ${condTxt}
          · AI ${esc(r.model_label || r.model)} · ${esc(r.elapsed_s)}초 · 토큰 ${fmtInt(u.input_tokens || 0)}+${fmtInt(u.output_tokens || 0)}${u.usd_estimate != null ? ` · 약 $${esc(u.usd_estimate)}` : ""}${r.dropped_refs ? ` · 없는 근거 번호 ${r.dropped_refs}개 지움` : ""}</div>
      </div>`;
    el.classList.toggle("folded", !!st.abFolded);
    el.hidden = false;
  }

  function openDecSource(n) {
    const s = st.decAiRes && st.decAiRes.sources[n - 1];
    if (!s) return;
    document.querySelectorAll(".hit[data-decsrc]").forEach((b) => b.classList.toggle("sel", +b.dataset.decsrc === n));
    openDec({ id: s.id }, s.page);
  }

  // ------------------------------------------------------------ 다운로드 목록
  function loadCart() {
    try { return JSON.parse(localStorage.getItem("datahub.cart") || "[]"); } catch { return []; }
  }
  function saveCart() {
    try { localStorage.setItem("datahub.cart", JSON.stringify(st.cart)); } catch { /* 저장 못 해도 화면은 동작 */ }
  }
  function sameSpec(a, b) {
    if (!a || !b) return false;
    const k = (s) => JSON.stringify([s.source, s.id, s.cycle, s.start, s.end, s.sel, s.kind, s.fs_div, s.reprt, s.flow, s.scope]);
    return k(a) === k(b);
  }
  function renderDock() {
    const dock = $("#dock");
    if (!st.cart.length) { dock.hidden = true; return; }
    dock.hidden = false;
    const list = st.dockOpen ? `<div class="dock-list">${st.cart.map((c, i) => `<div class="dock-item"><div class="n">${esc(c.name)}<small>${esc(c.source)} ${esc(c.cycle_label)} ${esc(c.start)} ~ ${esc(c.end)}, ${esc(c.selection_text || "")}</small></div><button class="btn btn-sm btn-ghost" data-rm="${i}" type="button">빼기</button></div>`).join("")}</div>` : "";
    dock.innerHTML = `${list}<div class="dock-bar"><div class="lab">다운로드 목록 <b>${st.cart.length}</b>건</div>
      <button class="btn btn-ghost" id="dockToggle" type="button">${st.dockOpen ? "목록 접기" : "목록 보기"}</button>
      <button class="btn btn-ghost" id="dockClear" type="button">비우기</button>
      <button class="btn btn-primary" id="dockZip" type="button">ZIP으로 모두 받기</button></div>`;
  }

  // ------------------------------------------------------------ 이벤트
  document.addEventListener("click", async (ev) => {
    const t = ev.target.closest("button, a");
    if (!t) return;
    if (t.dataset.q) { runSearch(t.dataset.q); return; }
    if (t.dataset.mode) { if (t.dataset.mode !== st.mode) setMode(t.dataset.mode); $("#q").focus(); return; }
    if (t.dataset.corp !== undefined) { openCorp(st.corps[+t.dataset.corp]); return; }
    if (t.dataset.dec !== undefined) { openDec(st.decList[+t.dataset.dec]); return; }
    if (t.dataset.decsrc !== undefined) { openDecSource(+t.dataset.decsrc); return; }
    if (t.dataset.decmode) {
      const on = t.dataset.decmode === "ai";
      if (on !== st.decAi) {
        setDecAi(on);
        st.lastQuery = ""; $("#q").value = ""; st.decRes = null; st.decAiRes = null; st.decSel = null; st.decDoc = null; st.decPage = null;
        renderAiBrief(null);
        (on ? renderDecAiWelcome : renderDecWelcome)();
        writeHash();
      }
      $("#q").focus();
      return;
    }
    if (t.id === "decAbFold") { st.abFolded = !st.abFolded; renderDecAiBrief(); return; }
    if (t.dataset.decpage !== undefined) { if (t.dataset.decpage) openDecPage(+t.dataset.decpage); return; }
    if (t.dataset.decsort) { st.dec.sort = t.dataset.decsort; syncDecFilters(); decRerun(); return; }
    if (t.dataset.dectype !== undefined) { st.dec.type = t.dataset.dectype; syncDecFilters(); decRerun(); return; }
    if (t.id === "btnDecMore") { loadDecMore(t); return; }
    if (t.id === "btnDecCite") {
      const d = st.decDoc, r = st.decPage;
      const ok = await copyText(`공정거래위원회 ${d.no}(${d.date}), ${d.name}, ${r.page}쪽`);
      toast(ok ? "인용을 복사했습니다" : "복사하지 못했습니다");
      return;
    }
    if (t.classList.contains("hit")) { openHit(st.results[+t.dataset.i]); return; }
    if (t.dataset.dtab) { st.dtab = t.dataset.dtab; renderDartQuery(); $("#previewPanel").innerHTML = `<div class="empty"><b>미리보기</b>조건을 고르고 '미리보기'를 누르세요.</div>`; st.preview = null; st.previewErr = null; return; }
    if (t.dataset.fsdiv) { st.fs.fs_div = t.dataset.fsdiv; renderDartQuery(); return; }
    if (t.dataset.scope) { st.fs.scope = t.dataset.scope; renderDartQuery(); return; }
    if (t.dataset.reprt) {
      st.fs.reprt = t.dataset.reprt;
      const y = today.getFullYear() - (t.dataset.reprt === "11011" ? 1 : 0);   // 분기·반기 보고서는 올해 것도 나와 있을 수 있음
      const span = (+st.fs.end - +st.fs.start) || 4;
      st.fs.end = String(y); st.fs.start = String(y - span);
      renderDartQuery(); return;
    }
    if (t.dataset.flow) { st.fs.flow = t.dataset.flow; renderDartQuery(); return; }
    if (t.dataset.fsspan) { const n = +t.dataset.fsspan; st.fs.start = String(Math.max(2015, +st.fs.end - n + 1)); renderDartQuery(); return; }
    if (t.dataset.dlspan) {
      const e = new Date(today), s0 = new Date(today); s0.setMonth(s0.getMonth() - +t.dataset.dlspan);
      st.dlist.start = iso(s0); st.dlist.end = iso(e); renderDartQuery(); return;
    }
    if (t.dataset.dunit) { st.dunit = t.dataset.dunit; renderDartPreview(); return; }
    if (t.id === "btnDart") { runDartPreview(); return; }
    if (t.id === "btnMore") { loadMore(t); return; }
    if (t.id === "abFold") { st.abFolded = !st.abFolded; renderAiBrief(st.aiRes); return; }
    if (t.dataset.pvflip) { const d = t.dataset.pvflip; pivotChange(st.preview, (pv) => {
      const from = pv.rows.includes(d) ? pv.rows : pv.cols, to = from === pv.rows ? pv.cols : pv.rows;
      from.splice(from.indexOf(d), 1); to.push(d); }); return; }
    if (t.dataset.pvup) { const d = t.dataset.pvup; pivotChange(st.preview, (pv) => {
      const side = pv.rows.includes(d) ? pv.rows : pv.cols, i = side.indexOf(d);
      if (i > 0) { side.splice(i, 1); side.splice(i - 1, 0, d); } }); return; }
    if (t.dataset.pvswap) { pivotChange(st.preview, (pv) => { const r = pv.rows; pv.rows = pv.cols; pv.cols = r; }); return; }
    if (t.dataset.pvreset) { st.pivot = null; rerenderPreview(); return; }
    if (t.dataset.egroup !== undefined) { if (t.dataset.egroup) openEgroup(t.dataset.egroup, t.dataset.egym || null); return; }
    if (t.dataset.egcorp) { openCorp({ corp_code: t.dataset.egcorp, name: t.dataset.egname || t.dataset.egcorp }); return; }
    if (t.dataset.egdl) {
      const gg = st.egroup.group, kind = t.dataset.egdl;
      const spec = { source: "EGROUP", kind, id: kind === "groups" ? "ALL" : gg.code, ym: kind === "changes" ? null : gg.ym, cycle: "A", sel: {} };
      t.disabled = true;
      try { const r = await downloadPost("/api/download", { spec, format: t.dataset.fmt, shape: "long" }); toast(`받았습니다: ${r.name}`); }
      catch (e) { toast(`${e.label || "오류"}: ${e.message}`); }
      t.disabled = false;
      return;
    }
    if (t.id === "btnDgFile") {
      t.disabled = true; const label = t.textContent; t.textContent = "받는 중";
      try {
        const r = await downloadGet(`/api/datago/file?id=${encodeURIComponent(st.detail.id)}`);
        toast(`받았습니다: ${r.name}`);
      } catch (e) {
        toast(`${e.label || "오류"}: ${e.message}`);
      }
      t.disabled = false; t.textContent = label;
      return;
    }
    if (t.id === "btnIndRefresh") {
      t.disabled = true;
      try { await api("/api/dart/industry/refresh", { method: "POST" }); } catch (e) { toast(`${e.label || "오류"}: ${e.message}`); }
      loadInd(); return;
    }
    if (t.dataset.indreprt) { st.ind.reprt = t.dataset.indreprt; renderDartQuery(); return; }
    if (t.dataset.indfs) { st.ind.fs_div = t.dataset.indfs; renderDartQuery(); return; }
    if (t.dataset.source !== undefined) {
      st.source = t.dataset.source;
      t.parentElement.querySelectorAll("button").forEach((b) => b.classList.toggle("on", b === t));
      if (st.lastQuery) runSearch(st.lastQuery);
      return;
    }
    if (t.dataset.cycle !== undefined) {
      st.cycle = t.dataset.cycle;
      t.parentElement.querySelectorAll("button").forEach((b) => b.classList.toggle("on", b === t));
      if (st.lastQuery) runSearch(st.lastQuery);
      return;
    }
    if (t.dataset.qcycle) {
      const c = st.detail.cycles.find((x) => x.cycle === t.dataset.qcycle);
      st.spec.cycle = c.cycle; st.spec.start = c.default_start; st.spec.end = c.default_end;
      renderQuery();
      return;
    }
    if (t.dataset.span) {
      const c = st.detail.cycles.find((x) => x.cycle === st.spec.cycle);
      if (t.dataset.span === "all") { st.spec.start = c.start; st.spec.end = c.end; }
      else {
        const n = +t.dataset.span * PER_YEAR[c.cycle];
        st.spec.end = c.end;
        let s = shift(c.cycle, c.end, -(n - 1));
        const a = canon(c.cycle, s), b = canon(c.cycle, c.start);
        if (a && b && cmpPeriod(a, b) < 0) s = c.start;
        st.spec.start = s;
      }
      $("#pStart").value = st.spec.start; $("#pEnd").value = st.spec.end;
      updateEstimate();
      return;
    }
    if (t.dataset.all) { const dim = st.detail.dims.find((x) => x.key === t.dataset.all); st.sel[dim.key] = new Set(dim.items.map((i) => i.code)); refreshDim(dim.key); return; }
    if (t.dataset.none) { st.sel[t.dataset.none] = new Set(); refreshDim(t.dataset.none); return; }
    if (t.id === "btnPreview") { runPreview(); return; }
    if (t.dataset.dl) {
      const fmt = t.dataset.dl;
      const shape = ($("#csvShape") || {}).value || "long";
      t.disabled = true;
      try {
        const r = await downloadPost("/api/download", { spec: st.previewSpec, format: fmt, shape, pivot: st.pivot });
        toast(`받았습니다: ${r.name}`);
      } catch (e) {
        toast(`${e.label || "오류"}: ${e.message}`);
      }
      t.disabled = false;
      return;
    }
    if (t.id === "btnCart") {
      const p = st.preview;
      st.cart.push({ spec: st.previewSpec, name: p.name, source: p.source, cycle_label: p.cycle_label, start: p.start, end: p.end, selection_text: p.selection_text, pivot: st.pivot });
      saveCart(); renderDock(); rerenderPreview();
      toast("다운로드 목록에 담았습니다");
      return;
    }
    if (t.dataset.rm !== undefined) { st.cart.splice(+t.dataset.rm, 1); saveCart(); renderDock(); rerenderPreview(); return; }
    if (t.id === "dockToggle") { st.dockOpen = !st.dockOpen; renderDock(); return; }
    if (t.id === "dockClear") { st.cart = []; st.dockOpen = false; saveCart(); renderDock(); rerenderPreview(); return; }
    if (t.id === "dockZip") {
      t.disabled = true; t.textContent = "받는 중";
      try {
        const r = await downloadPost("/api/download/zip", { specs: st.cart.map((c) => ({ ...c.spec, name: c.name, pivot: c.pivot || null })) });
        toast(r.failed ? `받았습니다. 실패한 항목: ${r.failed}` : `받았습니다: ${r.name}`);
      } catch (e) {
        toast(`${e.label || "오류"}: ${e.message}`);
      }
      renderDock();
    }
  });

  document.addEventListener("change", (ev) => {
    const t = ev.target;
    if (t.matches("input[type=checkbox][data-dim]")) {
      const s = st.sel[t.dataset.dim];
      if (t.checked) s.add(t.value); else s.delete(t.value);
      const box = t.closest("[data-dimbox]");
      const dim = st.detail.dims.find((x) => x.key === t.dataset.dim);
      box.querySelector(".cnt").textContent = `${fmtInt(s.size)} / ${fmtInt(dim.items.length)}개 선택`;
      updateEstimate();
    }
    if (t.id === "optSyn") { st.syn = t.checked; if (st.lastQuery) runSearch(st.lastQuery); }
    if (t.id === "optRecent") { st.recent = t.checked; if (st.lastQuery) runSearch(st.lastQuery); }
    if (t.id === "egYm") { openEgroup(st.egroup.group.code, t.value); return; }
    if (t.id === "aiModel") { try { localStorage.setItem("datahub.aiModel", t.value); } catch { /* 기억 못 해도 동작 */ } }
    if (t.id === "optListed") { st.listedOnly = t.checked; if (st.lastQuery) runSearch(st.lastQuery); }
    if (t.id === "decModel") { try { localStorage.setItem("datahub.decModel", t.value); } catch { /* 기억 못 해도 동작 */ } return; }
    if (t.id === "decType" || t.id === "decAct") { st.dec[t.id === "decType" ? "type" : "act"] = t.value; if (!st.decAi) decRerun(); else writeHash(); return; }
    if (t.id === "decY1" || t.id === "decY2") {
      const v = t.value.trim(), ok = v === "" || /^\d{4}$/.test(v);
      t.classList.toggle("bad", !ok);
      if (ok) { st.dec[t.id === "decY1" ? "y1" : "y2"] = v; if (!st.decAi) decRerun(); else writeHash(); }
      return;
    }
    if (t.id === "decGo") {
      const pl = (st.decPage || {}).page_list || [], n = +t.value.trim();
      const p = pl.find((x) => x >= n) ?? pl[pl.length - 1];
      if (p) openDecPage(p, false);
      return;
    }
    if (t.dataset && t.dataset.sj) { if (t.checked) st.fs.sj.add(t.dataset.sj); else st.fs.sj.delete(t.dataset.sj); }
    if (t.dataset && t.dataset.indlv) { st.ind.prefix = t.dataset.indlv; }
  });

  document.addEventListener("input", (ev) => {
    const t = ev.target;
    if (t.dataset && t.dataset.filter) { refreshDim(t.dataset.filter); return; }
    if (t.id === "fsStart" || t.id === "fsEnd") {
      const v = t.value.trim(), ok = /^\d{4}$/.test(v) && +v >= 2015;
      t.classList.toggle("bad", !ok);
      st.fs[t.id === "fsStart" ? "start" : "end"] = v;
      return;
    }
    if (t.id === "indYear") {
      const v = t.value.trim();
      t.classList.toggle("bad", !(/^\d{4}$/.test(v) && +v >= 2015));
      st.ind.year = v;
      return;
    }
    if (t.id === "dlStart" || t.id === "dlEnd") {
      const v = t.value.trim(), ok = /^\d{4}[-./]?\d{2}[-./]?\d{2}$/.test(v);
      t.classList.toggle("bad", !ok);
      st.dlist[t.id === "dlStart" ? "start" : "end"] = v;
      return;
    }
    if (t.id === "pStart" || t.id === "pEnd") {
      const v = t.value.trim();
      const ok = !!canon(st.spec.cycle, v);
      t.classList.toggle("bad", !ok);
      if (ok) st.spec[t.id === "pStart" ? "start" : "end"] = show(st.spec.cycle, canon(st.spec.cycle, v));
      else st.spec[t.id === "pStart" ? "start" : "end"] = v;
      updateEstimate();
    }
  });

  $("#searchForm").addEventListener("submit", (ev) => { ev.preventDefault(); runSearch($("#q").value); });
  document.addEventListener("keydown", (ev) => {
    if (ev.key === "/" && document.activeElement.tagName !== "INPUT") { ev.preventDefault(); $("#q").focus(); }
  });

  // 주소에 검색어·통계표를 담아 둔다(즐겨찾기·동료에게 보내기): #q=CPI&src=KOSIS&id=101/DT_1J22003
  function writeHash() {
    const p = new URLSearchParams();
    if (st.mode === "dart" || st.mode === "ai" || st.mode === "dec") p.set("m", st.mode);
    if (st.mode === "dec" && st.decAi) p.set("ai", "1");
    if (st.lastQuery) p.set("q", st.lastQuery);
    if (st.mode === "dart") { if (st.corp) p.set("corp", st.corp.corp_code); }
    else if (st.mode === "dec") {
      const d = st.dec;
      [["t", d.type], ["a", d.act], ["y1", d.y1], ["y2", d.y2]].forEach(([k, v]) => v && p.set(k, v));
      if (d.sort !== "date") p.set("s", d.sort);
      if (st.decSel && !st.decAi) p.set("d", st.decSel);
      if (st.decPage && !st.decAi) p.set("p", st.decPage.page);
    }
    else if (st.hit) { p.set("src", st.hit.source); p.set("id", st.hit.id); }
    history.replaceState(null, "", p.toString() ? `#${p}` : location.pathname);
  }
  const _runSearch = runSearch;
  runSearch = async (q) => { await _runSearch(q); writeHash(); };
  const _openHit = openHit;
  openHit = async (h) => { const pr = _openHit(h); writeHash(); return pr; };

  async function fromHash() {
    const p = new URLSearchParams(location.hash.slice(1));
    if (p.get("m") === "dart") { setMode("dart", true); renderDartWelcome(); }
    if (p.get("m") === "dec") {      // 의결서: 조건·검색어·연 의결서·쪽까지 되살림
      setMode("dec", true);
      const y = (v) => (/^\d{4}$/.test(v || "") ? v : "");
      st.dec = { type: p.get("t") || "", act: p.get("a") || "", y1: y(p.get("y1")), y2: y(p.get("y2")), sort: p.get("s") === "hits" ? "hits" : "date" };
      if (p.get("ai") === "1") {    // AI 질문은 저절로 묻지 않는다(비용) — 질문만 채워 둔다
        setDecAi(true); renderDecAiWelcome(); syncDecFilters();
        if (p.get("q")) $("#q").value = p.get("q");
        return;
      }
      renderDecWelcome();
      const q = p.get("q") || "";
      if (q || decHasFilter()) await runSearch(q);
      const id = +p.get("d");
      if (id) openDec(st.decList.find((x) => x.id === id) || { id }, +p.get("p") || null);
      return;
    }
    if (p.get("m") === "ai") {        // AI는 저절로 묻지 않는다(비용) — 질문만 채워 둔다
      setMode("ai", true); renderAiWelcome();
      if (p.get("q")) $("#q").value = p.get("q");
      return;
    }
    const q = p.get("q");
    if (!q) return;
    await runSearch(q);
    if (st.mode === "dart") {
      const cc = p.get("corp");
      if (!cc) return;
      openCorp(st.corps.find((x) => x.corp_code === cc) || { corp_code: cc, name: cc });
      return;
    }
    const src = p.get("src"), id = p.get("id");
    if (!id) return;
    let h = st.results.find((x) => x.source === src && x.id === id);
    if (!h) h = { source: src, id, name: id, items: [] };
    openHit(h);
  }

  loadStatus().then(fromHash);
  renderDock();
  $("#q").focus();
})();
