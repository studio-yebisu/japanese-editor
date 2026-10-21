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
    this._value = this.tagName === "TEXTAREA" || ["source", "output", "context"].includes(this.id)
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
  focus() {} select() { this.selected = true; } showModal() { this.open = true; } close() { this.open = false; this.listeners.close?.(); }
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
        { id: "sarashina", label: "Sarashina", available: true, configured: true, models: ["sarashina3-mini", "sarashina3-nano"], max_text_chars: 2000, max_document_chars: 12000, chunking: "paragraphs", notice: "API", auth_method: "api_key" },
        { id: "gemini", label: "Gemini 3.8 Flash", available: true, cli_installed: true, cli_status: "ready", models: ["gemini-3.8-flash-high"], max_text_chars: 12000, notice: "Google公式CLI", auth_method: "google_login" },
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
  const state = { config: configFixture(), analysis: null, analysisError: null, exportMutator: null, exportGate: null, settingsGate: null, settingsError: false, requests: [], clipboard: null, progress: null, clipboardGate: null, fastClipboardTimeout: false };
  const fetch = async (url, options = {}) => {
    const body = options.body ? JSON.parse(options.body) : null;
    state.requests.push({ url, body, headers: options.headers, signal: options.signal });
    let data;
    if (url === "/api/config") data = state.config;
    else if (url === "/api/integrations/codex") data = { registered: true, skill_installed: true };
    else if (url === "/api/settings") {
      if (state.settingsGate) await state.settingsGate();
      if (state.settingsError) return { ok: false, status: 500, json: async () => ({ error: "設定の保存に失敗" }) };
      state.config.generation = { ...state.config.generation, provider: body.provider, model: body.model };
      data = state.config.generation;
    }
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
    AbortController, Blob, URL, performance,
    setTimeout: (fn, delay) => setTimeout(fn, delay === 15000 && state.fastClipboardTimeout ? 0 : delay),
    clearTimeout, setInterval, clearInterval, fetch,
    navigator: { clipboard: { writeText: async (text) => { if (state.clipboardGate) await state.clipboardGate(); state.clipboard = text; } } },
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

function useSarashina(state) {
  state.config.generation.provider = "sarashina";
  state.config.generation.model = "sarashina3-mini";
}

test("quick provider switch saves only provider/model, preserves the manuscript and invalidates the old result", async () => {
  const { state, get, h } = harness(); await h.loadConfig();
  h.setSourceText(raw); h.invalidate(); state.analysis = analysisFixture(raw); await h.analyze();
  get("api-key").value = "unsaved-key-must-not-be-submitted";
  get("quick-provider").value = "sarashina";
  await get("quick-provider").listeners.change();
  assert.deepEqual(state.requests.find((item) => item.url === "/api/settings").body, { provider: "sarashina", model: "sarashina3-mini" });
  assert.equal(h.currentSource(), raw); assert.equal(h.getRun(), null);
  assert.equal(get("quick-provider").value, "sarashina"); assert.equal(get("provider").value, "sarashina");
  assert.equal(get("enable-decider").disabled, true); assert.equal(get("settings-dialog").open, false);
  assert.match(get("status-text").textContent, /切り替えました/);
});

test("failed quick switch retains the previous result, provider and source", async () => {
  const { state, get, h } = harness(); await h.loadConfig();
  h.setSourceText(raw); h.invalidate(); state.analysis = analysisFixture(raw); await h.analyze();
  const result = h.getRun(); state.settingsError = true;
  get("quick-provider").value = "sarashina"; await get("quick-provider").listeners.change();
  assert.equal(get("quick-provider").value, "gemini"); assert.equal(get("provider").value, "gemini");
  assert.equal(h.getRun(), result); assert.equal(h.getOutput(), corrected); assert.equal(h.currentSource(), raw);
  assert.equal(get("copy-result").disabled, false); assert.match(get("error-message").textContent, /切り替えられませんでした/);
});

test("unconfigured quick switch opens setup and cancelling restores provider, judgment and result", async () => {
  for (const missing of ["key", "decider"]) {
    const { state, get, h, toggle } = harness();
    if (missing === "key") state.config.generation.providers[0].available = false;
    else state.config.decider = { available: false, status: "not_configured" };
    await h.loadConfig(); h.setSourceText(raw); toggle("enable-decider", false);
    state.analysis = analysisFixture(raw); await h.analyze(); const result = h.getRun();
    get("quick-provider").value = "sarashina"; await get("quick-provider").listeners.change();
    assert.equal(get("settings-dialog").open, true); assert.equal(get("provider").value, "sarashina");
    assert.equal(state.requests.filter((item) => item.url === "/api/settings").length, 0);
    get("settings-dialog").close();
    assert.equal(get("quick-provider").value, "gemini"); assert.equal(get("provider").value, "gemini");
    assert.equal(get("enable-decider").checked, false); assert.equal(h.currentSource(), raw); assert.equal(h.getRun(), result);
  }
});

test("saving an unconfigured quick switch commits the choice and clears the old result", async () => {
  const { state, get, h } = harness(); state.config.generation.providers[0].available = false;
  await h.loadConfig(); h.setSourceText(raw); h.invalidate();
  state.analysis = analysisFixture(raw); await h.analyze();
  get("quick-provider").value = "sarashina"; await get("quick-provider").listeners.change();
  await get("save-provider").click(); get("settings-dialog").close();
  assert.equal(get("quick-provider").value, "sarashina"); assert.equal(h.getRun(), null); assert.equal(h.currentSource(), raw);
});

test("quick provider selection is locked during generation, saving and export", async () => {
  const { state, get, h } = harness(); await h.loadConfig(); h.setSourceText(raw); h.invalidate();
  let finishAnalysis; state.analysis = () => new Promise((resolve) => { finishAnalysis = resolve; });
  const analyzing = h.analyze(); assert.equal(get("quick-provider").disabled, true);
  get("quick-provider").value = "sarashina"; await get("quick-provider").listeners.change();
  assert.equal(state.requests.filter((item) => item.url === "/api/settings").length, 0);
  finishAnalysis(analysisFixture(raw)); await analyzing;
  let finishExport; state.exportGate = () => new Promise((resolve) => { finishExport = resolve; });
  const exporting = h.exportResult("copy"); assert.equal(get("quick-provider").disabled, true);
  finishExport(); await exporting;
  let finishSave; state.settingsGate = () => new Promise((resolve) => { finishSave = resolve; });
  get("quick-provider").value = "sarashina"; const saving = get("quick-provider").listeners.change();
  assert.equal(get("quick-provider").disabled, true); assert.equal(get("copy-result").disabled, true);
  finishSave(); await saving; assert.equal(get("quick-provider").disabled, false);
});

test("fresh Sarashina configuration selects mini, shows key setup, and requires meaning judgment", async () => {
  const { state, get, h, toggle } = harness(); useSarashina(state);
  state.config.generation.providers.find((item) => item.id === "sarashina").available = false;
  state.config.generation.providers.find((item) => item.id === "sarashina").configured = false;
  await h.loadConfig();
  assert.equal(get("provider").value, "sarashina"); assert.equal(get("provider-model").value, "sarashina3-mini");
  assert.equal(get("generation-option-label").textContent, "Sarashina校正");
  assert.equal(get("enable-decider").checked, true); assert.equal(get("enable-decider").disabled, true);
  assert.equal(get("decider-requirement").hidden, false); assert.equal(get("api-key-settings").hidden, false);
  assert.equal(get("gemini-login-settings").hidden, true); assert.equal(get("setup-prompt").hidden, false);
  assert.match(get("credential-status").textContent, /SarashinaのAPIキーは未設定/);
  h.setSourceText(raw); h.invalidate(); const before = state.requests.length; await h.analyze();
  assert.equal(state.requests.length, before); assert.equal(get("settings-dialog").open, true);
  toggle("enable-decider", false); assert.equal(get("enable-decider").checked, true);
});

test("Sarashina sends both stages and exports judged candidates without approval buttons", async () => {
  const { state, get, h, toggle } = harness(); useSarashina(state); await h.loadConfig();
  h.setSourceText(raw); h.invalidate(); toggle("enable-decider", false);
  state.analysis = { ...analysisFixture(raw), requirements: { needs_decider: true } }; await h.analyze();
  const body = state.requests.find((item) => item.url === "/api/analyze").body;
  assert.equal(body.provider, "sarashina"); assert.equal(body.model, "sarashina3-mini");
  assert.equal(body.generate, true); assert.equal(body.judge, true);
  assert.equal(h.getOutput(), corrected); assert.equal(get("copy-result").disabled, false);
  assert.equal(get("output-title").textContent, "校正後"); assert.equal(get("edits-list").querySelectorAll("button").length, 0);
  await h.exportResult("copy"); assert.equal(state.clipboard, corrected);
});

test("Sarashina blocks transmission for unconfigured Decider but permits configured busy/loading", async () => {
  for (const status of ["not_configured", "disabled", "error", "busy", "loading"]) {
    const { state, get, h } = harness(); useSarashina(state);
    const configured = ["busy", "loading"].includes(status);
    state.config.decider = { available: configured, status };
    state.config.stages.decider = configured ? status : "not_connected";
    await h.loadConfig(); h.setSourceText(raw); h.invalidate(); state.analysis = analysisFixture(raw);
    await h.analyze();
    assert.equal(state.requests.some((item) => item.url === "/api/analyze"), configured, status);
    if (!configured) {
      assert.match(get("error-message").textContent, /Sarashinaの校正にはDeciderの意味判定が必要/);
      assert.equal(get("settings-dialog").open, true); assert.match(get("status-text").textContent, /原文は送信していません/);
    }
  }
});

test("rules-only mode and switching from Sarashina to Gemini keep optional judging available", async () => {
  const { state, get, h, toggle } = harness(); useSarashina(state); await h.loadConfig();
  h.setSourceText(raw); h.invalidate(); toggle("enable-generation", false); toggle("enable-decider", false);
  assert.equal(get("enable-decider").disabled, false); assert.equal(get("decider-requirement").hidden, true);
  state.analysis = { ...analysisFixture(raw), edits: [], stages: { rules: "executed", generation: "not_requested", decider: "not_run" } };
  await h.analyze(); let body = state.requests.filter((item) => item.url === "/api/analyze").at(-1).body;
  assert.equal(body.generate, false); assert.equal(body.judge, false); assert.equal(Object.hasOwn(body, "provider"), false);
  assert.equal(get("copy-result").disabled, false);
  toggle("enable-generation", true); assert.equal(get("enable-decider").checked, true); assert.equal(get("enable-decider").disabled, true);
  get("provider").value = "gemini"; get("provider").listeners.change(); toggle("enable-decider", false);
  assert.equal(get("enable-decider").disabled, false); assert.equal(get("enable-decider").checked, false);
  state.analysis = analysisFixture(raw, { status: "not_run" }); await h.analyze();
  body = state.requests.filter((item) => item.url === "/api/analyze").at(-1).body;
  assert.equal(body.provider, "gemini"); assert.equal(body.judge, false); assert.equal(get("copy-result").disabled, false);
});

for (const decision of [undefined, { status: "not_run" }, { status: "error", error: { message: "モデルを読めません" } },
  { status: "input_rejected" }, { status: "executed", choice: "unknown" }]) {
  test(`Sarashina retains raw source and candidates while ${decision?.choice || decision?.status || "missing"} judgment blocks export`, async () => {
    const { state, get, h } = harness(); useSarashina(state); await h.loadConfig(); h.setSourceText(raw); h.invalidate();
    state.analysis = analysisFixture(raw, decision || { status: "not_run" });
    if (!decision) delete state.analysis.edits[0].decision;
    // A response without a requirements receipt must still honor the bound
    // Sarashina request, rather than silently making judgment optional.
    if (decision?.status !== "not_run") state.analysis.requirements = { needs_decider: true };
    await h.analyze();
    assert.equal(h.currentSource(), raw); assert.equal(h.getOutput(), corrected); assert.ok(h.getRun());
    assert.equal(get("copy-result").disabled, true); assert.equal(get("download-result").disabled, true);
    assert.match(get("output-title").textContent, /修正候補/); assert.match(get("meaning-warning").textContent, /意味判定が未完了/);
    assert.match(get("status-text").textContent, /コピー・保存はできません/);
    assert.doesNotMatch(get("status-text").textContent, /校正が終わりました|コピー・保存できます/);
    assert.doesNotMatch(get("comparison-summary").textContent, /意味を保っていると判定/);
    const before = state.requests.length; await h.exportResult("copy"); await h.exportResult("download");
    assert.equal(state.requests.length, before); assert.equal(state.clipboard, null);
  });
}

test("Sarashina partial judgment blocks export; backend requirements also apply to other providers", async () => {
  for (const provider of ["sarashina", "gemini"]) {
    const { state, get, h } = harness();
    if (provider === "sarashina") useSarashina(state);
    await h.loadConfig(); h.setSourceText(raw); h.invalidate(); state.analysis = analysisFixture(raw);
    const before = "3件"; const start = raw.indexOf(before);
    state.analysis.edits.push({ id: "e2", start, end: start + before.length, before, after: "三件", decision: { status: "not_run" } });
    state.analysis.requirements = { needs_decider: true }; state.analysis.stages.decider = "partial";
    await h.analyze(); assert.equal(get("copy-result").disabled, true);
    assert.match(get("comparison-summary").textContent, /1 \/ 2 件を判定/);
    assert.match(get("output-title").textContent, /修正候補/);
  }
});

test("Sarashina executed warnings and zero-edit results remain exportable", async () => {
  for (const choice of ["shifted", "needs_review", "no_edits"]) {
    const { state, get, h } = harness(); useSarashina(state); await h.loadConfig(); h.setSourceText(raw); h.invalidate();
    state.analysis = analysisFixture(raw, { status: "executed", choice }); state.analysis.requirements = { needs_decider: true };
    if (choice === "no_edits") { state.analysis.edits = []; state.analysis.stages.decider = "not_run"; }
    await h.analyze(); assert.equal(get("copy-result").disabled, false);
    if (choice !== "no_edits") { assert.equal(get("meaning-warning").hidden, false); assert.match(get("meaning-warning").textContent, /反映済み/); }
    await h.exportResult("copy"); assert.equal(state.clipboard, choice === "no_edits" ? raw : corrected);
  }
});

test("unchanged-only Sarashina responses preserve original and explicitly warn about missed edits", async () => {
  const { state, get, h } = harness(); useSarashina(state); await h.loadConfig(); h.setSourceText(raw); h.invalidate();
  state.analysis = analysisFixture(raw); state.analysis.edits = []; state.analysis.stages.decider = "not_run";
  state.analysis.generator = { ...state.analysis.generator, result: "unchanged_proposals_only" };
  await h.analyze();
  assert.equal(h.getOutput(), raw);
  assert.match(get("status-text").textContent, /有効な修正案を得られませんでした/);
  assert.match(get("status-text").textContent, /見逃し/);
});

test("pending clipboard permission cannot lock export forever or discard the result", async () => {
  const { state, get, h } = harness(); await h.loadConfig();
  h.setSourceText(raw); h.invalidate(); state.analysis = analysisFixture(raw); await h.analyze();
  state.clipboardGate = () => new Promise(() => {});
  state.fastClipboardTimeout = true;
  await h.exportResult("copy");
  assert.equal(state.clipboard, null);
  assert.equal(h.getOutput(), corrected);
  assert.match(get("error-message").textContent, /クリップボード.*コピーが完了しません/);
  assert.equal(get("download-result").disabled, false);
  assert.equal(get("operation-progress").hidden, true);
  assert.equal(get("manual-copy").hidden, false);
  assert.equal(get("manual-copy-text").value, corrected.replace(/\r\n/g, "\n"));
  get("select-copy-text").click();
  assert.equal(get("manual-copy-text").selected, true);
  await h.exportResult("download");
  assert.match(get("status-text").textContent, /保存を開始/);
  h.setSourceText("別の原文"); h.invalidate();
  assert.equal(get("manual-copy").hidden, true);
  assert.equal(get("manual-copy-text").value, "");
});

test("oversized corrected response never replaces the source or enables export", async () => {
  const { state, get, h } = harness(); await h.loadConfig();
  const source = "あ".repeat(11999) + "語";
  h.setSourceText(source); h.invalidate();
  state.analysis = { ...analysisFixture(raw), edits: [{ id: "e1", start: 11999, end: 12000,
    before: "語", after: "単語", reason: "test" }] };
  await h.analyze();
  assert.equal(h.currentSource(), source);
  assert.equal(h.getRun(), null);
  assert.equal(get("copy-result").disabled, true);
  assert.equal(get("download-result").disabled, true);
  assert.match(get("error-message").textContent, /修正後.*12,000/);
});

test("held overflow keeps its explanation and exports the unchanged original", async () => {
  const { state, get, h } = harness(); await h.loadConfig();
  const source = "🙂".repeat(11999) + "語";
  h.setSourceText(source); h.invalidate();
  state.analysis = { ...analysisFixture(raw), edits: [], notes: ["12,000字を超える修正案を保留しました。原文は残しています。"] };
  await h.analyze();
  assert.equal(h.currentSource(), source);
  assert.equal(h.getOutput(), source);
  assert.match(get("run-notes").textContent, /12,000.*保留/);
  await h.exportResult("copy");
  assert.equal(state.clipboard, source);
});

test("structured held_edit_count is surfaced on the main view and leads to the existing notes", async () => {
  const { state, get, h } = harness(); await h.loadConfig(); h.setSourceText(raw); h.invalidate();
  state.analysis = { ...analysisFixture(raw), edits: [], held_edit_count: 2, notes: ["コードまたはURLに重なる修正案を保留しました。"] };
  await h.analyze();
  assert.equal(h.getOutput(), raw); assert.equal(get("copy-result").disabled, false);
  assert.equal(get("meaning-warning").hidden, false);
  assert.match(get("meaning-warning").textContent, /保留した修正案 2 件は反映せず/);
  assert.match(get("changed-count").textContent, /保留 2 件/);
  assert.match(get("output-summary").textContent, /保留した修正案 2 件/);
  assert.doesNotMatch(get("output-summary").textContent, /変更はありません/);
  assert.match(get("status-text").textContent, /保留した修正案が 2 件/);
  assert.doesNotMatch(get("status-text").textContent, /変更はありません/);
  assert.match(get("comparison-summary").textContent, /保留 2 件は判定対象外/);
  assert.match(get("edits-list").textContent, /保留した修正案が 2 件/);
  assert.equal(get("review-details").open, true);
  assert.match(get("run-notes").textContent, /コードまたはURLに重なる修正案を保留/);
  get("review-details").open = false;
  const link = get("meaning-warning").querySelectorAll("button")[0];
  assert.equal(link.textContent, "保留の理由を見る"); link.click();
  assert.equal(get("review-details").open, true);
  assert.equal(get("edits-list").querySelectorAll("button").length, 0);
});

test("held proposals alongside applied edits are counted without blocking automatic output", async () => {
  const { state, get, h } = harness(); await h.loadConfig(); h.setSourceText(raw); h.invalidate();
  state.analysis = { ...analysisFixture(raw), held_edit_count: 1 }; await h.analyze();
  assert.equal(h.getOutput(), corrected); assert.equal(get("copy-result").disabled, false);
  assert.match(get("meaning-warning").textContent, /保留した修正案 1 件/);
  assert.doesNotMatch(get("meaning-warning").textContent, /反映済み/);
  assert.match(get("status-text").textContent, /1 件の変更を右の文章へ反映しました。保留した修正案が 1 件/);
  assert.match(get("edits-list").textContent, /保留した修正案が 1 件/);
  assert.match(get("review-count").textContent, /変更 1 件 · 保留 1 件/);
});

test("held wording in notes or a malformed count never invents held proposals", async () => {
  const { state, get, h } = harness(); await h.loadConfig(); h.setSourceText(raw); h.invalidate();
  state.analysis = { ...analysisFixture(raw), edits: [], held_edit_count: "2", notes: ["修正案を保留しました。"] };
  await h.analyze();
  assert.equal(get("meaning-warning").hidden, true); assert.equal(get("review-details").open, false);
  assert.doesNotMatch(get("changed-count").textContent, /保留/);
  assert.match(get("comparison-summary").textContent, /^修正案がないため.*実行していません/);
});

test("risk flags stay visible when Decider says preserved; decision and automatic output are unchanged", async () => {
  const { state, get, h } = harness(); await h.loadConfig(); h.setSourceText(raw); h.invalidate();
  state.analysis = analysisFixture(raw); state.analysis.edits[0].risk_flags = ["数値の変化"];
  await h.analyze();
  assert.equal(h.getOutput(), corrected); assert.equal(get("copy-result").disabled, false);
  assert.equal(h.getRun().data.edits[0].decision.choice, "preserved");
  assert.equal(get("meaning-warning").hidden, false);
  assert.match(get("meaning-warning").textContent, /確認したい変化を検知 1 件。これらの変更も反映済み/);
  assert.match(get("comparison-summary").textContent, /1 件で意味を保っていると判定（うち 1 件は/);
  const card = get("edits-list").children.find((child) => child.tagName === "ARTICLE");
  assert.match(card.className, /has-attention/); assert.match(card.className, /has-risk/);
  assert.match(card.textContent, /Decider：意味を保っていると判定/);
  assert.match(card.textContent, /変更済み · 検知した変化を確認/);
  assert.match(card.textContent, /意味の保持を保証しません/);
  assert.match(card.textContent, /数値の変化/);
  assert.equal(get("edits-list").querySelectorAll("button").length, 0);
});

test("a failed retry with unchanged inputs restores the previous success and says this attempt failed", async () => {
  const { state, get, h } = harness(); await h.loadConfig(); h.setSourceText(raw); h.invalidate();
  state.analysis = analysisFixture(raw); await h.analyze();
  const first = h.getRun();
  state.analysisError = "一時的に利用できません。"; await h.analyze();
  assert.equal(state.requests.filter((item) => item.url === "/api/analyze").length, 2);
  assert.equal(h.getRun().restored, true); assert.equal(h.getRun().snapshot, first.snapshot);
  assert.equal(h.getOutput(), corrected); assert.equal(h.currentSource(), raw);
  assert.match(get("output-title").textContent, /前回の結果/);
  assert.match(get("output-summary").textContent, /今回の校正は失敗したため、前回成功した結果/);
  assert.match(get("error-message").textContent, /今回の校正は失敗しました。一時的に利用できません。/);
  assert.match(get("status-text").textContent, /前回成功した結果に戻しました/);
  assert.match(get("review-count").textContent, /^前回の結果/);
  assert.equal(get("copy-result").disabled, false);
  await h.exportResult("copy"); assert.equal(state.clipboard, corrected);
  state.analysisError = null; await h.analyze();
  assert.equal(h.getRun().restored, undefined); assert.equal(get("output-title").textContent, "校正後");
});

test("previous results never return after edits, setting changes, stale failures, or a first failure", async () => {
  {
    const { state, get, h } = harness(); await h.loadConfig(); h.setSourceText(raw); h.invalidate();
    state.analysisError = "失敗"; await h.analyze();
    assert.equal(h.getRun(), null); assert.equal(h.getOutput(), ""); assert.equal(get("copy-result").disabled, true);
    assert.match(get("error-message").textContent, /^校正できませんでした/);
  }
  {
    const { state, get, h } = harness(); await h.loadConfig(); h.setSourceText(raw); h.invalidate();
    state.analysis = analysisFixture(raw); await h.analyze();
    get("source").value = get("source").value.replace("3件", "4件"); get("source").listeners.input();
    state.analysisError = "失敗"; await h.analyze();
    assert.equal(h.getRun(), null); assert.equal(h.getOutput(), "");
    assert.doesNotMatch(get("error-message").textContent, /前回/);
  }
  {
    const { state, get, h, toggle } = harness(); await h.loadConfig(); h.setSourceText(raw); h.invalidate();
    state.analysis = analysisFixture(raw); await h.analyze();
    toggle("enable-decider", false); toggle("enable-decider", true);
    state.analysisError = "失敗"; await h.analyze();
    assert.equal(h.getRun(), null); assert.equal(get("copy-result").disabled, true);
  }
  {
    const { state, get, h, toggle } = harness(); await h.loadConfig(); h.setSourceText(raw); h.invalidate();
    state.analysis = analysisFixture(raw); await h.analyze();
    let fail; state.analysis = () => new Promise((_, reject) => { fail = reject; });
    const pending = h.analyze(); toggle("enable-decider", false); toggle("enable-decider", true);
    fail(new Error("late")); await pending;
    assert.equal(h.getRun(), null); assert.equal(h.getOutput(), "");
    assert.equal(get("error-box").hidden, true); assert.equal(get("output-title").textContent, "校正後");
  }
});

test("settings button keeps an accessible name and the selected service limit is shown before sending", async () => {
  assert.match(html, /<button id="settings-open"[^>]*\baria-label="設定"/);
  const { get, h, toggle } = harness(); await h.loadConfig();
  h.setSourceText("あ".repeat(12001));
  get("provider").value = "sarashina"; get("provider").listeners.change();
  assert.equal(get("character-count").textContent, "12,001 / 12,000 字（上限超過） · 2,000字以内ずつ");
  assert.equal(get("character-count").className, "is-over");
  h.setSourceText("あ".repeat(4001)); get("source").listeners.input();
  toggle("enable-generation", false);
  assert.equal(get("character-count").textContent, "4,001 / 12,000 字");
  assert.equal(get("character-count").className, "");
});

test("saved Gemini settings retain independent toggles and export exact corrected raw text", async () => {
  const { state, get, h } = harness(); await h.loadConfig();
  assert.equal(get("provider").value, "gemini"); assert.equal(get("provider-model").value, "gemini-3.8-flash-high");
  assert.equal(get("enable-decider").disabled, false); assert.equal(get("decider-requirement").hidden, true);
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
  state.config.generation.providers.find((item) => item.id === "gemini").available = false;
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
  assert.doesNotMatch(get("edits-list").textContent, /意味を保っていると判定/);
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
    assert.doesNotMatch(get("edits-list").textContent, /意味を保っていると判定/);
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
  assert.match(get("progress-message").textContent, /Yomiyasuで全文を点検しています/);
  const request = state.requests.find((item) => item.url === "/api/export");
  assert.equal(request.headers["X-Yebisu-Request-Id"], operation.requestId);
  assert.equal(get("copy-result").disabled, true); release(); await pending;
  assert.equal(state.clipboard, corrected); assert.equal(get("operation-progress").hidden, true);
});
