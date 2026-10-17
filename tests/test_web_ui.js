"use strict";

// Offline frontend regression tests. Run with: node --test tests/test_web_ui.js
// The real application script executes against a small DOM/fetch fixture; no
// external generation, local model, browser, or credential store is contacted.
const { test, afterEach } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const web = path.join(__dirname, "..", "web");
const script = fs.readFileSync(path.join(web, "app.js"), "utf8");
const html = fs.readFileSync(path.join(web, "index.html"), "utf8");
const activeHarnesses = new Set();
afterEach(() => { for (const h of activeHarnesses) h.stopBusy(); activeHarnesses.clear(); });

class Element {
  constructor(tag = "div", id = "") {
    this.tagName = tag.toUpperCase(); this.id = id; this.children = [];
    this.hidden = false; this.disabled = false; this.checked = false;
    this.dataset = {}; this.listeners = {}; this._value = ""; this._text = "";
    this.open = false; this.scrollTop = 0; this.scrollLeft = 0; this.fields = {};
    this.attributes = {};
  }
  get value() { return this.tagName === "PROGRESS" ? this.attributes.value ?? 0 : this._value; }
  set value(value) {
    if (this.tagName === "PROGRESS") { this.attributes.value = Number(value); return; }
    this._value = ["source", "output", "context"].includes(this.id)
      ? String(value).replace(/\r\n?/g, "\n") : String(value);
  }
  get textContent() { return this._text + this.children.map((child) => child.textContent || "").join(""); }
  set textContent(value) { this._text = String(value); this.children = []; }
  append(...items) { this.children.push(...items); }
  replaceChildren(...items) {
    this._text = ""; this.children = [...items];
    if (this.tagName === "SELECT") this._value = items[0]?.value || "";
  }
  addEventListener(name, fn) { this.listeners[name] = fn; }
  querySelector(selector) {
    if (selector === "button") return this.querySelectorAll("button")[0];
    return this.fields[selector] ||= new Element();
  }
  querySelectorAll(selector) {
    return this.children.flatMap((child) => [
      ...(selector === "button" && child.tagName === "BUTTON" ? [child] : []),
      ...(child.querySelectorAll ? child.querySelectorAll(selector) : []),
    ]);
  }
  setAttribute(name, value) { this.attributes[name] = String(value); this[name] = value; }
  removeAttribute(name) { delete this.attributes[name]; delete this[name]; }
  hasAttribute(name) { return Object.hasOwn(this.attributes, name); }
  focus() {} showModal() { this.open = true; } close() { this.open = false; }
  click() { return this.listeners.click?.({ target: this }); } remove() {}
}

function configFixture() {
  return {
    profiles: [{ id: "fiction", label: "小説", description: "作風を残す" }],
    strengths: [{ id: "natural", label: "自然に" }], max_text: 12000,
    stages: { rules: "ready", generator: "available", decider: "ready" },
    decider: { available: true, loaded: true, status: "ready", device: "cpu", precision_override: true },
    generation: {
      provider: "gemini", model: "gemini-3.8-flash-high", secret_store: { available: true },
      providers: [
        { id: "gemini", label: "Gemini 3.8 Flash", available: true, cli_installed: true, cli_status: "ready", models: ["gemini-3.8-flash-high"], max_text_chars: 12000, notice: "Google公式CLI", auth_method: "google_login" },
        { id: "sarashina", label: "Sarashina", available: true, configured: true, models: ["sarashina"], max_text_chars: 4000, notice: "API", auth_method: "api_key" },
        { id: "claude", label: "Claude CLI", available: true, models: ["claude-model"], max_text_chars: 12000, notice: "CLI", auth_method: "existing_cli" },
      ],
    },
  };
}

function analysisFixture(raw, decision = { status: "executed", choice: "preserved" }) {
  const before = "利用することが可能です";
  const start = raw.indexOf(before);
  return {
    offset_encoding: "utf-16", source_hash: "source-hash", profile: "fiction", strength: "natural",
    stages: { rules: "executed", generation: "executed", decider: decision.status === "executed" ? "executed" : "not_run" },
    yomiyasu: { version: "2.0.0", rules: "executed", generation_guidance: { status: "included", sources: [{ path: "SKILL.md" }] } },
    findings: [], questions: [], notes: [],
    edits: [{ id: "e1", start, end: start + before.length, before, after: "利用できます", reason: "回りくどさを整理", decision: { ...decision, scope: "excerpt", device: "cpu" } }],
  };
}

function harness() {
  const elements = new Map();
  const get = (id) => {
    if (!elements.has(id)) {
      const match = html.match(new RegExp(`<([a-z0-9-]+)\\b[^>]*\\bid="${id}"[^>]*>`, "i"));
      assert.ok(match, `The application references missing element #${id}`);
      const element = new Element(match[1], id);
      element.checked = /\bchecked\b/.test(match[0]);
      element.hidden = /\bhidden\b/.test(match[0]);
      elements.set(id, element);
    }
    return elements.get(id);
  };
  const state = { config: configFixture(), analysis: null, analysisError: null, exportMutator: null, exportGate: null, requests: [], clipboard: null, progress: null };
  const fetch = async (url, options = {}) => {
    const body = options.body ? JSON.parse(options.body) : null;
    state.requests.push({ url, body, headers: options.headers, signal: options.signal });
    let data;
    if (url === "/api/config") data = state.config;
    else if (url === "/api/integrations/codex") data = { registered: true, skill_installed: true };
    else if (url === "/api/analyze") {
      if (state.analysisError) return { ok: false, status: 401, json: async () => ({ error: state.analysisError }) };
      data = typeof state.analysis === "function" ? await state.analysis() : state.analysis;
    } else if (url === "/api/export") {
      if (state.exportGate) await state.exportGate();
      data = { text: body.final, source_hash: "source-hash", final_hash: "final-hash", check: { status: "executed" }, verification: { status: "executed" } };
      if (state.exportMutator) state.exportMutator(data);
    } else if (url.startsWith("/api/progress/")) {
      if (state.progress === null) return { ok: false, status: 404, json: async () => ({ error: "not registered yet" }) };
      data = typeof state.progress === "function" ? await state.progress() : state.progress;
    } else throw Error(`Unexpected endpoint ${url}`);
    return { ok: true, status: 200, json: async () => structuredClone(data) };
  };
  const context = {
    AbortController, Blob, URL, performance, setTimeout, clearTimeout, setInterval, clearInterval, fetch,
    navigator: { clipboard: { writeText: async (text) => { state.clipboard = text; } } },
    document: { getElementById: get, createElement: (tag) => new Element(tag), createTextNode: (text) => ({ textContent: text }), body: new Element("body") },
    window: { addEventListener() {} },
  };
  const marker = "  loadConfig();\n})();";
  assert.ok(script.endsWith(`${marker}\n`) || script.endsWith(marker));
  const instrumented = script.replace(marker, `  globalThis.uiTest = {loadConfig, setSourceText, currentSource, invalidate, analyze, exportResult,
    applyEdits, changedSpan, comparisonParts, paintComparison, syncRawText, displayText, validateEdits,
    startBusy, stopBusy, renderProgress, pollProgress,
    getProgress: () => activeProgress, getRun: () => run, getOutput: () => outputRaw};\n})();`);
  vm.runInNewContext(instrumented, context);
  const h = context.uiTest;
  activeHarnesses.add(h);
  const toggle = (id, checked) => { get(id).checked = checked; get(id).listeners.change(); };
  return { state, get, h, toggle };
}

const raw = "😀この機能を利用することが可能です。\r\n保存件数の上限は3件です。\r\n`code` https://example.test";
const corrected = raw.replace("利用することが可能です", "利用できます");

test("default toggles submit generate/judge and immediately render/export exact corrected raw text", async () => {
  const { state, get, h } = harness(); await h.loadConfig();
  assert.equal(get("enable-generation").checked, true); assert.equal(get("enable-decider").checked, true);
  h.setSourceText(raw); h.invalidate(); state.analysis = analysisFixture(raw); await h.analyze();
  const request = state.requests.find((item) => item.url === "/api/analyze");
  assert.equal(request.body.generate, true); assert.equal(request.body.judge, true);
  assert.equal(request.body.text, raw); assert.equal(request.body.provider, "gemini");
  assert.equal(h.getOutput(), corrected); assert.equal(get("output-preview").textContent, corrected);
  assert.equal(h.currentSource(), raw); assert.equal(get("copy-result").disabled, false);
  assert.equal(get("download-result").disabled, false); assert.equal(get("meaning-warning").hidden, true);
  assert.equal(get("edits-list").querySelectorAll("button").length, 0);
  assert.match(get("run-summary").textContent, /Yomiyasu 2\.0\.0：規則点検を実行/);
  assert.match(get("run-summary").textContent, /推敲方針の全文と参照資料を生成モデルへ渡しました/);
  await h.exportResult("copy");
  assert.equal(state.clipboard, corrected);
  assert.match(get("export-note").textContent, /Yomiyasuの全文点検と本文照合/);
  assert.match(get("export-note").textContent, /合格判定ではありません/);
  const exported = state.requests.find((item) => item.url === "/api/export").body;
  assert.equal(exported.text, raw); assert.equal(exported.final, corrected);
  assert.equal(get("analyze-generate").querySelector(".button-main").textContent, "校正する");
  assert.doesNotMatch(html, /accept-all|skip-pending|採用|未選択/);
});

test("generation off skips provider setup; judge-on with no proposals reports not-run and unchanged output", async () => {
  const { state, get, h, toggle } = harness();
  state.config.generation.providers[0].available = false;
  await h.loadConfig(); h.setSourceText(raw); h.invalidate();
  assert.equal(get("setup-prompt").hidden, false);
  const before = state.requests.length; await h.analyze();
  assert.equal(state.requests.length, before); assert.equal(get("settings-dialog").open, true);
  get("settings-dialog").close(); toggle("enable-generation", false);
  assert.equal(get("setup-prompt").hidden, true);
  state.analysis = { ...analysisFixture(raw), edits: [], stages: { rules: "executed", generation: "not_requested", decider: "not_run" } };
  state.analysis.yomiyasu.generation_guidance = { status: "not_sent", sources: [] };
  await h.analyze();
  const request = state.requests.find((item) => item.url === "/api/analyze").body;
  assert.equal(request.generate, false); assert.equal(request.judge, true);
  assert.equal(Object.hasOwn(request, "provider"), false); assert.equal(Object.hasOwn(request, "model"), false);
  assert.equal(h.getOutput(), raw); assert.equal(get("copy-result").disabled, false);
  assert.match(get("comparison-summary").textContent, /修正案がないため.*実行していません/);
  assert.match(get("run-summary").textContent, /Yomiyasu 2\.0\.0：規則点検を実行/);
  assert.match(get("run-summary").textContent, /生成用の方針は送信していません/);
  assert.doesNotMatch(get("run-summary").textContent, /全文と参照資料を生成モデルへ渡しました/);
  assert.equal(state.requests.some((item) => item.url === "/api/judge"), false);
});

test("each toggle is independent; disabled judging never appears as a successful check", async () => {
  const { state, get, h, toggle } = harness(); await h.loadConfig(); h.setSourceText(raw); h.invalidate();
  toggle("enable-decider", false); state.analysis = analysisFixture(raw, { status: "not_run" }); await h.analyze();
  const body = state.requests.find((item) => item.url === "/api/analyze").body;
  assert.equal(body.generate, true); assert.equal(body.judge, false); assert.equal(h.getOutput(), corrected);
  assert.match(get("meaning-warning").textContent, /意味判定未実行 1 件/);
  assert.match(get("comparison-summary").textContent, /オフ/);
  assert.doesNotMatch(get("edits-list").textContent, /意味を保持と判定/);
  toggle("enable-generation", false); state.analysis = { ...analysisFixture(raw), edits: [], stages: { rules: "executed", generation: "not_requested", decider: "not_run" } }; await h.analyze();
  const last = state.requests.filter((item) => item.url === "/api/analyze").at(-1).body;
  assert.equal(last.generate, false); assert.equal(last.judge, false); assert.equal(h.getOutput(), raw);
});

for (const decision of [{ status: "executed", choice: "shifted" }, { status: "executed", choice: "needs_review" }, { status: "error", error: { message: "モデルを読めません" } }, { status: "not_run" }]) {
  test(`automatic output keeps ${decision.choice || decision.status} visible without an approval gate`, async () => {
    const { state, get, h } = harness(); await h.loadConfig(); h.setSourceText(raw); h.invalidate();
    state.analysis = analysisFixture(raw, decision); await h.analyze();
    assert.equal(h.getOutput(), corrected); assert.equal(h.currentSource(), raw);
    assert.equal(get("meaning-warning").hidden, false); assert.equal(get("copy-result").disabled, false);
    assert.match(get("meaning-warning").textContent, /反映済み/);
    assert.doesNotMatch(get("edits-list").textContent, /意味を保持と判定/);
    assert.match(get("edits-list").textContent, /変更済み/);
    assert.equal(get("edits-list").querySelectorAll("button").length, 0);
  });
}

test("provider labels and request binding follow Sarashina/Claude; changing settings invalidates output", async () => {
  const { state, get, h, toggle } = harness(); await h.loadConfig(); h.setSourceText(raw); h.invalidate();
  for (const [provider, label] of [["sarashina", "Sarashina校正"], ["claude", "Claude校正"]]) {
    get("provider").value = provider; get("provider").listeners.change();
    assert.equal(get("generation-option-label").textContent, label);
    state.analysis = analysisFixture(raw); await h.analyze();
    const body = state.requests.filter((item) => item.url === "/api/analyze").at(-1).body;
    assert.equal(body.provider, provider);
  }
  toggle("enable-decider", false); assert.equal(h.getRun(), null); assert.equal(get("copy-result").disabled, true);
  state.analysis = analysisFixture(raw, { status: "not_run" }); await h.analyze();
  get("source").value = get("source").value.replace("3件", "4件"); get("source").listeners.input();
  assert.equal(h.getRun(), null); assert.equal(h.currentSource(), raw.replace("3件", "4件"));
});

test("late responses are discarded after a toggle change", async () => {
  const { state, get, h, toggle } = harness(); await h.loadConfig(); h.setSourceText(raw); h.invalidate();
  let release; state.analysis = () => new Promise((resolve) => { release = resolve; });
  const pending = h.analyze(); toggle("enable-decider", false); release(analysisFixture(raw)); await pending;
  assert.equal(h.getRun(), null); assert.equal(get("copy-result").disabled, true);
  assert.equal(state.requests.find((item) => item.url === "/api/analyze").signal.aborted, true);
});

test("invalid spans/overlaps fail without changing original or producing exportable output", async () => {
  const { state, get, h } = harness(); await h.loadConfig(); h.setSourceText(raw); h.invalidate();
  state.analysis = analysisFixture(raw);
  state.analysis.edits.push({ ...state.analysis.edits[0], id: "conflict" });
  await h.analyze(); assert.equal(h.getRun(), null); assert.equal(h.currentSource(), raw);
  assert.equal(get("copy-result").disabled, true); assert.match(get("error-message").textContent, /重なる修正/);
  assert.throws(() => h.validateEdits([{ id: "half", start: 1, end: 2, before: raw.slice(1, 2), after: "X" }], raw), /文字の途中/);
  assert.throws(() => h.validateEdits([{ id: "bad", start: 0, end: 1, before: "違う", after: "X" }], raw), /位置が一致/);
});

test("comparison reconstructs exact CRLF/UTF16 text, preserves literals, and covers insertion/deletion", () => {
  const { h } = harness(); const edits = analysisFixture(raw).edits;
  assert.equal(h.comparisonParts(raw, edits, "before").map((part) => part.text).join(""), raw);
  assert.equal(h.comparisonParts(raw, edits, "after").map((part) => part.text).join(""), corrected);
  assert.equal(h.syncRawText(raw, h.displayText(raw)), raw);
  assert.equal(h.syncRawText(raw, h.displayText(raw).replace("3件", "4件")), raw.replace("3件", "4件"));
  assert.equal(h.syncRawText("A\r\nB\nC\rD", "A\nBE\nC\nD"), "A\r\nBE\nC\rD");
  assert.equal(JSON.stringify(h.changedSpan("😀X", "😁X")), JSON.stringify({ start: 0, beforeEnd: 2, afterEnd: 2 }));
  const insert = { start: 1, end: 1, before: "", after: "追加" };
  const remove = { start: 1, end: 3, before: "BC", after: "" };
  assert.equal(h.comparisonParts("ABC", [insert], "after").map((part) => part.text).join(""), "A追加BC");
  assert.equal(h.comparisonParts("ABCD", [remove], "after").map((part) => part.text).join(""), "AD");
  const target = new Element(); h.paintComparison(target, [{ text: "<script>unsafe()</script>", changed: true }], "change-added");
  assert.equal(target.textContent, "<script>unsafe()</script>"); assert.equal(target.children[0].tagName, "MARK");
});

test("export provenance mismatches cannot reach clipboard", async () => {
  const { state, get, h } = harness(); await h.loadConfig(); h.setSourceText(raw); h.invalidate();
  state.analysis = analysisFixture(raw); await h.analyze();
  state.exportMutator = (data) => { data.source_hash = "wrong-source"; }; await h.exportResult("copy");
  assert.equal(state.clipboard, null); assert.equal(h.getOutput(), corrected); assert.equal(h.currentSource(), raw);
  assert.match(get("error-message").textContent, /一致しません/);
});

test("missing guidance receipt never claims the full Yomiyasu guide was sent", async () => {
  const { state, get, h } = harness(); await h.loadConfig(); h.setSourceText(raw); h.invalidate();
  state.analysis = analysisFixture(raw); delete state.analysis.yomiyasu.generation_guidance;
  await h.analyze();
  assert.match(get("run-summary").textContent, /推敲方針の送信記録はありません/);
  assert.doesNotMatch(get("run-summary").textContent, /全文と参照資料を生成モデルへ渡しました/);
  assert.equal(h.getOutput(), corrected);
});

test("unexecuted final checks cannot claim completion or reach clipboard", async () => {
  for (const field of ["check", "verification"]) {
    const { state, get, h } = harness(); await h.loadConfig(); h.setSourceText(raw); h.invalidate();
    state.analysis = analysisFixture(raw); await h.analyze();
    state.exportMutator = (data) => { data[field].status = "prepared_not_executed"; };
    await h.exportResult("copy");
    assert.equal(state.clipboard, null); assert.equal(get("export-note").hidden, true);
    assert.match(get("error-message").textContent, /一致しません/);
    assert.equal(h.getOutput(), corrected); assert.equal(h.currentSource(), raw);
  }
});

test("actual Decider counts determine progress; preparation and cloud generation remain indeterminate", () => {
  const { h, get } = harness(); const operation = h.startBusy("開始しています。");
  assert.equal(get("operation-progress").hidden, false);
  assert.equal(get("progress-bar").hasAttribute("value"), false);
  h.renderProgress(operation, { status: "running", stage: "generation", message: "Geminiで校正中です。", completed: 1, total: 5 });
  assert.equal(get("progress-bar").hasAttribute("value"), false); assert.match(get("progress-message").textContent, /Gemini/);
  h.renderProgress(operation, { status: "running", stage: "decider", completed: 0, total: 5, message: "準備しています。" });
  assert.equal(get("progress-bar").hasAttribute("value"), false); assert.match(get("progress-message").textContent, /最初の判定を待って/);
  h.renderProgress(operation, { status: "running", stage: "decider", completed: 2, total: 5, message: "抜粋を判定中です。" });
  assert.equal(get("progress-bar").max, 5); assert.equal(get("progress-bar").value, 2);
  assert.match(get("progress-message").textContent, /意味判定 2 \/ 5 件/);
  h.renderProgress(operation, { status: "running", stage: "decider", completed: 9, total: 5 });
  assert.equal(get("progress-bar").hasAttribute("value"), false);
  h.stopBusy(operation); assert.equal(get("operation-progress").hidden, true);
});

test("automatic retry attempts are visible while the generation bar stays indeterminate", () => {
  const { h, get } = harness(); const operation = h.startBusy("生成中です。");
  h.renderProgress(operation, { status: "running", stage: "generation", message: "自動再試行を準備しています。", attempt: 2, max_attempts: 3 });
  assert.match(get("progress-message").textContent, /2 \/ 3 回目/);
  assert.equal(get("progress-bar").hasAttribute("value"), false);
  h.renderProgress(operation, { status: "running", stage: "decider", message: "判定しています。", completed: 1, total: 4 });
  assert.doesNotMatch(get("progress-message").textContent, /回目/);
  assert.equal(get("progress-bar").value, 1);
});

test("analysis carries request-scoped progress header and early 404 does not become an operation error", async () => {
  const { state, h, get } = harness(); await h.loadConfig(); h.setSourceText(raw); h.invalidate();
  let release; state.analysis = () => new Promise((resolve) => { release = resolve; });
  const pending = h.analyze(); const operation = h.getProgress();
  const request = state.requests.find((item) => item.url === "/api/analyze");
  assert.equal(typeof request.headers["X-Yebisu-Request-Id"], "string");
  assert.equal(request.headers["X-Yebisu-Request-Id"], operation.requestId);
  clearTimeout(operation.pollTimer); await h.pollProgress(operation);
  assert.equal(get("error-box").hidden, true); assert.equal(get("operation-progress").hidden, false);
  assert.equal(get("progress-bar").hasAttribute("value"), false);
  release(analysisFixture(raw)); await pending;
  assert.equal(get("operation-progress").hidden, true); assert.equal(h.getProgress(), null);
});

test("progress from a superseded operation cannot overwrite a new operation or revive a stopped one", async () => {
  const { state, h, get } = harness();
  let release; state.progress = () => new Promise((resolve) => { release = resolve; });
  const old = h.startBusy("古い処理", { requestId: "old-request" });
  clearTimeout(old.pollTimer); const polling = h.pollProgress(old);
  const current = h.startBusy("現在の処理");
  release({ status: "running", stage: "decider", completed: 4, total: 5, message: "古い返答" }); await polling;
  assert.equal(get("progress-message").textContent, "現在の処理"); assert.equal(get("progress-bar").hasAttribute("value"), false);
  h.stopBusy(old); assert.equal(get("operation-progress").hidden, false);
  h.stopBusy(current); h.renderProgress(old, { status: "completed", message: "古い完了" });
  assert.equal(get("operation-progress").hidden, true);
});

test("export shows inspection progress with its own request ID, then clears progress", async () => {
  const { state, h, get } = harness(); await h.loadConfig(); h.setSourceText(raw); h.invalidate();
  state.analysis = analysisFixture(raw); await h.analyze();
  let release; state.exportGate = () => new Promise((resolve) => { release = resolve; });
  const pending = h.exportResult("copy"); const operation = h.getProgress();
  assert.match(get("progress-message").textContent, /Yomiyasuの全文点検中/);
  const request = state.requests.find((item) => item.url === "/api/export");
  assert.equal(request.headers["X-Yebisu-Request-Id"], operation.requestId);
  assert.equal(get("copy-result").disabled, true); release(); await pending;
  assert.equal(state.clipboard, corrected); assert.equal(get("operation-progress").hidden, true);
});
