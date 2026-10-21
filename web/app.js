"use strict";

(() => {
  const byId = (id) => document.getElementById(id);
  const ui = {
    quickProvider: byId("quick-provider"),
    provider: byId("provider"), providerModel: byId("provider-model"), apiKey: byId("api-key"),
    providerNotice: byId("provider-notice"), credentialStatus: byId("credential-status"),
    saveProvider: byId("save-provider"), removeKey: byId("remove-api-key"), providerSetup: byId("provider-setup"),
    geminiLogin: byId("login-gemini"), geminiSettings: byId("gemini-login-settings"), keySettings: byId("api-key-settings"),
    installCodex: byId("install-codex"), codexStatus: byId("codex-status"), releaseDecider: byId("release-decider"),
    aiConnectionsOpen: byId("ai-connections-open"), aiConnections: byId("ai-connections"),
    checkAiConnection: byId("check-ai-connection"), aiConnectionStatus: byId("ai-connection-status"),
    showMcpConfig: byId("show-mcp-config"), mcpConfigBox: byId("mcp-config-box"),
    mcpConfigText: byId("mcp-config-text"), copyMcpConfig: byId("copy-mcp-config"),
    deciderDevice: byId("decider-device"), saveDeciderDevice: byId("save-decider-device"), deciderDeviceStatus: byId("decider-device-status"),
    deciderBackend: byId("decider-backend"), saveDeciderBackend: byId("save-decider-backend"), deciderBackendStatus: byId("decider-backend-status"),
    source: byId("source"), sourceHelp: byId("source-help"), context: byId("context"), profile: byId("profile"), strength: byId("strength"),
    profileDescription: byId("profile-description"), count: byId("character-count"),
    generateButton: byId("analyze-generate"), generationToggle: byId("enable-generation"),
    deciderToggle: byId("enable-decider"), generationLabel: byId("generation-option-label"), generationTransmission: byId("generation-transmission"),
    deciderRequirement: byId("decider-requirement"),
    rulesStage: byId("rules-stage"), generatorStage: byId("generator-stage"),
    deciderStage: byId("decider-stage"), generationNotice: byId("generation-notice"),
    deciderNote: byId("decider-note"), refreshDecider: byId("refresh-decider"),
    status: byId("status-text"), error: byId("error-box"), errorMessage: byId("error-message"),
    retryConfig: byId("retry-config"), empty: byId("review-empty"), results: byId("review-results"),
    reviewCount: byId("review-count"), summary: byId("run-summary"),
    findings: byId("findings-list"), findingsCount: byId("findings-count"),
    edits: byId("edits-list"), editsCount: byId("edits-count"),
    editGuidance: byId("edits-guidance"), questions: byId("questions-list"),
    questionsSection: byId("questions-section"), questionsCount: byId("questions-count"),
    suggestions: byId("suggestions-list"), suggestionsSection: byId("suggestions-section"),
    suggestionsCount: byId("suggestions-count"),
    notes: byId("run-notes"), outputPanel: byId("output-panel"), output: byId("output"),
    outputSummary: byId("output-summary"), changedCount: byId("changed-count"), meaningWarning: byId("meaning-warning"),
    copy: byId("copy-result"), download: byId("download-result"), exportNote: byId("export-note"),
    manualCopy: byId("manual-copy"), manualCopyText: byId("manual-copy-text"), selectCopyText: byId("select-copy-text"),
    settingsOpen: byId("settings-open"), settingsClose: byId("settings-close"), settingsDialog: byId("settings-dialog"),
    setupPrompt: byId("setup-prompt"), sourceHighlight: byId("source-highlight"), preview: byId("output-preview"),
    outputTitle: byId("output-title"), comparisonSummary: byId("comparison-summary"), reviewDetails: byId("review-details"),
    errorSettings: byId("error-settings"), errorLogin: byId("error-login"), busyNote: byId("busy-note"),
    progressBox: byId("operation-progress"), progressBar: byId("progress-bar"), progressMessage: byId("progress-message"),
    settingsFeedback: byId("settings-feedback"),
    yomiyasuVersion: byId("yomiyasu-version"), yomiyasuStatus: byId("yomiyasu-update-status"), yomiyasuDetail: byId("yomiyasu-update-detail"),
    onboarding: byId("sarashina-onboarding"), resourceError: byId("decider-resource-error"), deciderGuide: byId("decider-setup-guide"),
    motion: byId("edit-motion"), motionLabel: byId("edit-motion-label"),
  };

  let config = null;
  let deciderDeviceDirty = false;
  let deciderBackendDirty = false;
  let run = null;
  let activeRequest = null;
  let activeTask = null;
  let activeExport = null;
  let refreshingCapability = false;
  let settingsBusy = false;
  let pendingQuickSwitch = null;
  let codexAvailable = false;
  let revision = 0;
  let sourceRaw = ui.source.value;
  let outputRaw = "";
  let busyTimer = null;
  let activeProgress = null;
  let yomiyasuTimer = null;

  const sample = "この機能を利用することが可能です。保存件数の上限は3件です。";

  // Textareas normalize line endings. Keep the exact imported text separately;
  // offsets from the server and export hashes always refer to this raw string.
  function displayText(raw) { return raw.replace(/\r\n?/g, "\n"); }

  function rawOffset(raw, displayOffset) {
    let index = 0;
    let offset = 0;
    while (index < raw.length && offset < displayOffset) {
      index += raw[index] === "\r" && raw[index + 1] === "\n" ? 2 : 1;
      offset += 1;
    }
    return index;
  }

  function syncRawText(raw, nextDisplay) {
    const previous = displayText(raw);
    if (previous === nextDisplay) return raw;
    let start = 0;
    while (start < previous.length && start < nextDisplay.length && previous[start] === nextDisplay[start]) start += 1;
    let tail = 0;
    while (tail < previous.length - start && tail < nextDisplay.length - start
      && previous[previous.length - tail - 1] === nextDisplay[nextDisplay.length - tail - 1]) tail += 1;
    const eol = raw.match(/\r\n|\r|\n/)?.[0] || "\n";
    const inserted = nextDisplay.slice(start, nextDisplay.length - tail).replace(/\n/g, eol);
    return raw.slice(0, rawOffset(raw, start)) + inserted + raw.slice(rawOffset(raw, previous.length - tail));
  }

  function currentSource() {
    sourceRaw = syncRawText(sourceRaw, ui.source.value);
    return sourceRaw;
  }

  function setSourceText(text) {
    sourceRaw = text;
    ui.source.value = text;
  }

  function applyEdits(source, edits) {
    return [...edits].sort((a, b) => b.start - a.start || b.end - a.end)
      .reduce((text, edit) => text.slice(0, edit.start) + edit.after + text.slice(edit.end), source);
  }

  function changedSpan(before, after) {
    let start = 0;
    while (start < before.length && start < after.length && before[start] === after[start]) start += 1;
    // Do not split a surrogate pair when two different characters share a high surrogate.
    if (start && /[\uD800-\uDBFF]/.test(before[start - 1])) start -= 1;
    let tail = 0;
    while (tail < before.length - start && tail < after.length - start
      && before[before.length - tail - 1] === after[after.length - tail - 1]) tail += 1;
    if (tail && /[\uDC00-\uDFFF]/.test(before[before.length - tail])) tail -= 1;
    return { start, beforeEnd: before.length - tail, afterEnd: after.length - tail };
  }

  function comparisonParts(source, edits, side) {
    const parts = [];
    let cursor = 0;
    [...edits].sort((a, b) => a.start - b.start || a.end - b.end).forEach((edit) => {
      parts.push({ text: source.slice(cursor, edit.start), changed: false });
      const span = changedSpan(edit.before, edit.after);
      const text = side === "before" ? edit.before : edit.after;
      const end = side === "before" ? span.beforeEnd : span.afterEnd;
      parts.push({ text: text.slice(0, span.start), changed: false });
      if (span.start !== span.beforeEnd || span.start !== span.afterEnd) {
        parts.push({ text: text.slice(span.start, end), changed: true });
      }
      parts.push({ text: text.slice(end), changed: false });
      cursor = edit.end;
    });
    parts.push({ text: source.slice(cursor), changed: false });
    return parts;
  }

  function paintComparison(element, parts, className) {
    element.replaceChildren(...parts.map((part) => {
      if (!part.changed) return document.createTextNode(part.text);
      const mark = node("mark", `${className}${part.text ? "" : " change-anchor"}`, part.text);
      if (!part.text) mark.setAttribute("aria-label", className === "change-added" ? "削除した位置" : "挿入する位置");
      return mark;
    }));
  }

  function syncSourceScroll() {
    ui.sourceHighlight.scrollTop = ui.source.scrollTop;
    ui.sourceHighlight.scrollLeft = ui.source.scrollLeft;
  }

  function openSettings(focusProvider = false) {
    if (!ui.settingsDialog.open) ui.settingsDialog.showModal();
    ui.settingsFeedback.textContent = ui.error.hidden ? "" : ui.errorMessage.textContent;
    if (focusProvider) ui.provider.focus({ preventScroll: true });
    if (config?.yomiyasu_update) refreshYomiyasu();
  }

  function renderYomiyasu(data) {
    if (!data || typeof data.active_version !== "string") return;
    const updateProgress = byId("yomiyasu-update-progress");
    if (updateProgress) updateProgress.hidden = !["checking", "downloading"].includes(data.state);
    ui.yomiyasuVersion.textContent = data.active_version;
    ui.yomiyasuStatus.textContent = typeof data.message === "string" ? data.message : "更新状態を確認できません。保存済みの版を使います。";
    const checked = data.checked_at ? new Date(data.checked_at) : null;
    ui.yomiyasuDetail.textContent = checked && Number.isFinite(checked.getTime()) ? `最終確認：${checked.toLocaleString("ja-JP")}` : "";
    if (data.skill_sync === "modified") ui.yomiyasuDetail.textContent += " Codex用の資料に変更があるため、そのコピーは保持しています。";
    if (data.skill_sync === "deferred") ui.yomiyasuDetail.textContent += " Codex用の資料の同期は保留中です。アプリを起動し直すと再確認します。";
  }

  async function refreshYomiyasu(remaining = 45) {
    if (yomiyasuTimer !== null) clearTimeout(yomiyasuTimer);
    yomiyasuTimer = null;
    try {
      const data = await readJson(await fetch("/api/yomiyasu/status", { headers: { Accept: "application/json" }, cache: "no-store" }));
      renderYomiyasu(data);
      if (["checking", "downloading"].includes(data.state) && remaining > 0) {
        yomiyasuTimer = setTimeout(() => refreshYomiyasu(remaining - 1), 2000);
      }
    } catch {
      // Update status never blocks editing, setup, or exporting a pinned result.
    }
  }

  function newRequestId() {
    return globalThis.crypto?.randomUUID?.() || `web-${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
  }

  function progressIsCurrent(operation) {
    return activeProgress === operation && operation.revision === revision && !operation.controller?.signal.aborted;
  }

  function renderProgress(operation, data) {
    if (!progressIsCurrent(operation)) return;
    renderEditMotion(operation, data);
    const message = typeof data.message === "string" && data.message ? data.message : operation.message;
    const countKnown = data.stage === "decider" && Number.isInteger(data.completed) && Number.isInteger(data.total)
      && data.total > 0 && data.completed >= 0 && data.completed <= data.total;
    ui.progressBox.hidden = false;
    ui.progressBar.removeAttribute("value");
    ui.progressBar.max = 1;
    if (countKnown && data.completed > 0) {
      ui.progressBar.max = data.total;
      ui.progressBar.value = data.completed;
      ui.progressMessage.textContent = `意味判定 ${data.completed} / ${data.total} 件 · ${message}`;
    } else if (countKnown) {
      ui.progressMessage.textContent = `意味判定 0 / ${data.total} 件 · モデルの準備と最初の判定を待っています。${message}`;
    } else if (data.stage === "generation" && Number.isInteger(data.chunk_index) && Number.isInteger(data.chunk_total)
      && data.chunk_index > 0 && data.chunk_index <= data.chunk_total) {
      ui.progressMessage.textContent = `段落の校正 ${data.chunk_index} / ${data.chunk_total} · ${message}`;
    } else if (data.stage === "generation" && Number.isInteger(data.attempt) && Number.isInteger(data.max_attempts)
      && data.attempt > 1 && data.attempt <= data.max_attempts && data.max_attempts <= 3) {
      ui.progressMessage.textContent = `${message}（${data.attempt} / ${data.max_attempts} 回目）`;
    } else {
      ui.progressMessage.textContent = message;
    }
    if (data.status === "completed") {
      ui.progressBar.max = 1;
      ui.progressBar.value = 1;
    }
  }

  async function pollProgress(operation) {
    if (!progressIsCurrent(operation) || !operation.requestId) return;
    const pollController = new AbortController();
    operation.pollController = pollController;
    let terminal = false;
    try {
      const response = await fetch(`/api/progress/${encodeURIComponent(operation.requestId)}`, {
        headers: { Accept: "application/json" }, cache: "no-store", signal: pollController.signal,
      });
      // The first poll can arrive before the POST has registered its request ID.
      if (!response.ok) return;
      const data = await response.json();
      if (!progressIsCurrent(operation) || pollController.signal.aborted) return;
      renderProgress(operation, data);
      terminal = ["completed", "failed"].includes(data.status);
    } catch {
      // Keep the last real stage if a progress-only request is unavailable.
      // The operation's POST still owns success and error reporting.
    } finally {
      if (operation.pollController === pollController) operation.pollController = null;
      if (progressIsCurrent(operation) && !terminal) {
        operation.pollTimer = setTimeout(() => pollProgress(operation), 1000);
      }
    }
  }

  function startBusy(message = "処理しています。", options = {}) {
    stopBusy();
    const operation = { message, revision, ...options, pollController: null, pollTimer: null };
    activeProgress = operation;
    renderProgress(operation, { status: "running", message });
    const started = performance.now();
    const tick = () => {
      const seconds = Math.floor((performance.now() - started) / 1000);
      ui.busyNote.hidden = false;
      ui.busyNote.textContent = `${seconds} 秒経過${seconds >= 30 ? " · 処理を続けています" : ""}`;
    };
    tick();
    busyTimer = setInterval(tick, 1000);
    if (operation.requestId) operation.pollTimer = setTimeout(() => pollProgress(operation), 1000);
    return operation;
  }

  function stopBusy(operation) {
    if (operation && activeProgress !== operation) return;
    clearEditMotion(activeProgress);
    if (activeProgress?.pollTimer != null) clearTimeout(activeProgress.pollTimer);
    activeProgress?.pollController?.abort();
    activeProgress = null;
    if (busyTimer !== null) clearInterval(busyTimer);
    busyTimer = null;
    ui.busyNote.hidden = true;
    ui.busyNote.textContent = "";
    ui.progressBox.hidden = true;
    ui.progressMessage.textContent = "";
    ui.progressBar.removeAttribute("value");
  }

  function clearEditMotion(operation) {
    ui.motion.hidden = true;
    ui.sourceHelp.textContent = "原文はそのまま残ります";
    ui.sourceHighlight.removeAttribute("data-motion");
    ui.preview.removeAttribute("data-motion");
    if (operation?.motionPainted && !run) {
      ui.sourceHighlight.textContent = currentSource() + "\n";
      ui.preview.replaceChildren(); ui.empty.hidden = false;
      ui.outputTitle.textContent = "校正後";
      ui.outputSummary.textContent = "";
      ui.comparisonSummary.textContent = "";
    }
  }

  function renderEditMotion(operation, data) {
    // Progress contains only hash + positions, never a cached manuscript.
    // The original remains editable; a revision/abort invalidates this view.
    const motion = data.motion;
    if (data.status !== "running" || data.stage !== "decider" || !motion
      || !operation.sourceHash || motion.source_hash !== operation.sourceHash
      || motion.offset_encoding !== "utf-16" || !Array.isArray(motion.edits)
      || !motion.edits.length || motion.edits.length > 50) return;
    const source = operation.source;
    const states = ["pending", "checking", "executed", "error", "not_run"];
    const seen = new Set();
    const entries = [...motion.edits].sort((a, b) => a.start - b.start || a.end - b.end);
    let previous = 0;
    for (const edit of entries) {
      if (!Number.isInteger(edit.index) || edit.index < 0 || edit.index >= 50 || seen.has(edit.index)
        || !Number.isInteger(edit.start) || !Number.isInteger(edit.end)
        || edit.start < previous || edit.end < edit.start || edit.end > source.length
        || !states.includes(edit.status)
        || [edit.start, edit.end].some((offset) => offset > 0 && offset < source.length
          && /[\uD800-\uDBFF]/.test(source[offset - 1]) && /[\uDC00-\uDFFF]/.test(source[offset]))) return;
      seen.add(edit.index); previous = edit.end;
    }
    const key = JSON.stringify(entries);
    if (operation.motionKey === key) return;
    operation.motionKey = key; operation.motionPainted = true;
    let cursor = 0;
    const parts = [], mirror = [];
    let focusMark = null, focusMirror = null;
    for (const edit of entries) {
      parts.push(document.createTextNode(source.slice(cursor, edit.start)));
      mirror.push(document.createTextNode(source.slice(cursor, edit.start)));
      const text = source.slice(edit.start, edit.end);
      const mark = node("mark", `motion-span motion-${edit.status}${text ? "" : " change-anchor"}`, text);
      parts.push(mark);
      const echo = node("mark", `motion-echo motion-${edit.status}${text ? "" : " change-anchor"}`, text);
      mirror.push(echo);
      if (edit.status === "checking") { focusMark = mark; focusMirror = echo; }
      cursor = edit.end;
    }
    parts.push(document.createTextNode(source.slice(cursor)), document.createTextNode("\n"));
    mirror.push(document.createTextNode(source.slice(cursor)));
    ui.sourceHighlight.replaceChildren(...parts); syncSourceScroll();
    ui.preview.replaceChildren(...mirror); ui.empty.hidden = true;
    ui.sourceHighlight.setAttribute("data-motion", "checking");
    ui.preview.setAttribute("data-motion", "checking");
    ui.outputTitle.textContent = "意味を照合しています";
    ui.outputSummary.textContent = "入力文の修正候補の位置を表示しています。修正後の本文は判定終了後に表示します。";
    const done = entries.filter((edit) => edit.status === "executed").length;
    const checking = entries.find((edit) => edit.status === "checking");
    if (checking && operation.focusedEdit !== checking.index && Number.isFinite(focusMark?.offsetTop)) {
      operation.focusedEdit = checking.index;
      ui.source.scrollTop = Math.max(0, focusMark.offsetTop - ui.source.clientHeight / 3);
      syncSourceScroll();
      ui.preview.scrollTop = Math.max(0, focusMirror.offsetTop - ui.preview.clientHeight / 3);
    }
    ui.motionLabel.textContent = checking
      ? `修正候補 ${checking.index + 1} を照合中 · 判定済み ${done} / ${entries.length}`
      : `修正箇所を確認しています · 判定済み ${done} / ${entries.length}`;
    ui.motion.hidden = false;
    ui.comparisonSummary.textContent = "赤ペンが示すのは生成済みの候補です。緑の本文は処理完了後に表示します。";
  }

  function node(tag, className, text) {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (text !== undefined) element.textContent = String(text);
    return element;
  }

  function setStatus(message) {
    ui.status.textContent = message;
    if (ui.settingsDialog.open) ui.settingsFeedback.textContent = message;
  }

  function showError(message, retry = false, settingsAction = false) {
    ui.errorMessage.textContent = message;
    ui.retryConfig.hidden = !retry;
    ui.errorSettings.hidden = !settingsAction;
    ui.errorLogin.hidden = !settingsAction || selectedGenerator()?.id !== "gemini"
      || !selectedGenerator()?.cli_installed || selectedGenerator()?.cli_status === "unsupported_version";
    ui.error.hidden = false;
    if (ui.settingsDialog.open) ui.settingsFeedback.textContent = message;
  }

  function clearError() {
    ui.error.hidden = true;
    ui.errorMessage.textContent = "";
    ui.retryConfig.hidden = true;
    ui.errorSettings.hidden = true;
    ui.errorLogin.hidden = true;
    ui.settingsFeedback.textContent = "";
  }

  function stage(element, label, ready = false) {
    element.textContent = label;
    element.className = `stage-badge ${ready ? "stage-ready" : "stage-muted"}`;
  }

  function deciderReady() {
    return config?.stages.decider === "ready" && config.decider?.available !== false
      && !["busy", "loading"].includes(config.decider?.status);
  }

  function decisionErrorMessage(error) {
    if (typeof error === "string") return error;
    return typeof error?.message === "string" ? error.message : "";
  }

  function isInputRejected(decision) {
    return decision?.status === "input_rejected"
      || ["invalid_input", "request_too_long"].includes(decision?.error?.code);
  }

  function precisionDeviceLabel(identity) {
    if (identity?.runtime_backend === "llama_cpp" || identity?.quantization === "Q8_0") {
      const layers = Number.isInteger(identity.gpu_layers) && identity.gpu_layers > 0
        ? `・GPU ${identity.gpu_layers}/${(identity.total_layers || 32) + 1}層${identity.cpu_assisted ? "・CPU併用" : ""}` : "";
      return `公式Q8 / ${deviceLabel(identity.device)}${layers}${identity.oom_retries ? `・メモリ不足で${identity.oom_retries}回再試行` : ""}`;
    }
    return identity?.precision_override === true ? "CPU BF16（精度変更）" : deviceLabel(identity?.device);
  }

  function deviceLabel(device) {
    if (device === "cpu") return "CPU";
    if (device === "auto") return "GPU優先・読み込み時に決定";
    if (device === "metal") return "Apple GPU（Metal）";
    if (typeof device === "string" && device.startsWith("cuda")) return device.toUpperCase();
    return typeof device === "string" && device ? device : "ローカル";
  }

  function renderDeciderCapability() {
    const capability = config?.decider;
    const backend = capability?.runtime_backend || "pytorch";
    if (!deciderBackendDirty) ui.deciderBackend.value = backend;
    if (capability?.device && !deciderDeviceDirty) ui.deciderDevice.value = capability.requested_device || (capability.device === "cuda" ? "cuda:0" : capability.device);
    for (const option of ui.deciderDevice.querySelectorAll("option")) option.disabled = backend !== "llama_cpp" && ["auto", "metal"].includes(option.value);
    const state = config?.stages.decider;
    const latestError = decisionErrorMessage(capability?.error);
    if (activeTask === "judge") {
      stage(ui.deciderStage, "ローカルで判定中");
      ui.deciderNote.textContent = "この PC で判定しています。入力を変えると結果を表示しませんが、推論自体は続く場合があります。";
    } else if (state === "busy" || capability?.status === "busy") {
      stage(ui.deciderStage, "別の判定を実行中");
      ui.deciderNote.textContent = "共有しているローカルDeciderが判定中です。完了してから接続状態を更新してください。";
    } else if (state === "loading" || capability?.status === "loading") {
      stage(ui.deciderStage, "モデル読み込み中");
      ui.deciderNote.textContent = "ローカルモデルを読み込んでいます。しばらくしてから接続状態を更新してください。";
    } else if (deciderReady()) {
      stage(ui.deciderStage, latestError ? "前回エラー・再試行可" : capability?.loaded ? "ローカルで読込済み" : "初回の判定で読み込む", !latestError);
      const model = typeof capability?.model === "string" ? capability.model : "Decider";
      ui.deciderNote.textContent = `${model} / ${precisionDeviceLabel(capability)}。${latestError ? "前の試行でエラーがありました。設定は有効なので、対象を確認して再試行できます。" : capability?.loaded ? "モデルは読み込み済みです。" : "初回はモデルを読み込むため時間がかかります。"}判定はこの PC で行います。意味の保持を保証するものではありません。`;
    } else if (state === "unavailable") {
      stage(ui.deciderStage, "利用できません");
      ui.deciderNote.textContent = requiresDecider()
        ? "Sarashinaの校正にはDeciderの意味判定が必要です。モデルと接続の設定を確認してください。"
        : "ローカル判定は利用できません。修正候補はご自身で原文と読み比べられます。";
    } else if (state === "not_connected") {
      stage(ui.deciderStage, "未接続");
      ui.deciderNote.textContent = requiresDecider()
        ? "Sarashinaの校正にはDeciderへの接続が必要です。設定後に接続状態を更新してください。"
        : "Decider は未接続です。モデルによる判定はまだ行っていません。";
    } else {
      stage(ui.deciderStage, "確認できません");
      ui.deciderNote.textContent = "Decider の状態を読み取れません。接続状態を更新して確認できます。";
    }
    if (latestError && activeTask !== "judge") {
      ui.deciderNote.append(node("br"), node("strong", "", "直近のエラー："), node("span", "", latestError));
    }
    const details = capability?.error?.details;
    const resources = [];
    for (const [label, free, required] of [
      ["空きRAM", details?.ram_available_bytes, details?.ram_required_bytes],
      ["コミット余裕", details?.commit_available_bytes, details?.commit_required_bytes],
      ["空きVRAM", details?.cuda_available_bytes, details?.cuda_required_bytes],
    ]) {
      if ([free, required].every((value) => Number.isFinite(value) && value >= 0 && value < 2 ** 60)) {
        resources.push(`${label} ${(free / 2 ** 30).toFixed(1)}GiB / 必要 ${(required / 2 ** 30).toFixed(1)}GiB`);
      }
    }
    ui.resourceError.hidden = resources.length === 0;
    ui.resourceError.textContent = resources.length ? `直近の実行時：${resources.join(" · ")}` : "";
  }

  function selectedGenerator() {
    return config?.generation?.providers.find((item) => item.id === ui.provider.value);
  }

  function generatorLabel() { return selectedGenerator()?.label || "選択したサービス"; }

  function requiresDecider() {
    return ui.generationToggle.checked && selectedGenerator()?.id === "sarashina";
  }

  function deciderConfigured() {
    // Busy/loading are configured runtimes; their queue and timeout are handled
    // by the server. Only a missing/disabled/unusable configuration blocks send.
    return config?.decider?.available === true
      && !["disabled", "not_configured", "error"].includes(config.decider.status);
  }

  function syncDeciderRequirement() {
    const required = requiresDecider();
    if (required) ui.deciderToggle.checked = true;
    ui.deciderToggle.disabled = !config || settingsBusy || required;
    ui.deciderRequirement.hidden = !required;
  }

  function requiredJudgmentPending(result = run) {
    if (!result || !(result.data.requirements?.needs_decider === true
      || (result.snapshot.generate && result.snapshot.provider === "sarashina"))) return false;
    return result.data.edits.some((edit) => edit.decision?.status !== "executed"
      || !["preserved", "shifted", "needs_review"].includes(edit.decision.choice));
  }

  function renderGenerator() {
    const provider = selectedGenerator();
    ui.quickProvider.value = ui.provider.value;
    const ready = provider?.available === true;
    const gemini = provider?.id === "gemini";
    const api = provider?.auth_method === "api_key";
    if (config) config.stages.generator = ready ? "available" : "unavailable";
    stage(ui.generatorStage, gemini ? ready ? "CLI 導入済み" : provider?.cli_status === "unsupported_version" ? "CLIのバージョンを確認" : provider?.cli_installed ? "Google ログイン待ち" : "CLI 未導入"
      : ready ? api ? "キー設定済み" : "CLI 検出済み" : api ? "API キー未設定" : "CLI 未検出", ready);
    ui.generateButton.querySelector(".button-main").textContent = "校正する";
    const shortLabel = { gemini: "Gemini", sarashina: "Sarashina", claude: "Claude" }[provider?.id] || provider?.label || "AI";
    ui.generationLabel.textContent = `${shortLabel}校正`;
    ui.generationTransmission.textContent = ui.generationToggle.checked ? "原文を送信" : "送信しません";
    ui.generateButton.querySelector(".button-detail").textContent = ui.generationToggle.checked
      ? `${provider?.label || "選択したサービス"}へ文章を送信` : "外部へ送信せず、規則を確認";
    ui.setupPrompt.hidden = !config || !ui.generationToggle.checked || (ready && (!requiresDecider() || deciderConfigured()));
    ui.setupPrompt.textContent = !ready ? api ? "APIキーを設定" : "接続を設定" : "PPDDの準備が必要";
    if (requiresDecider() && !deciderConfigured()) ui.deciderGuide.open = true;
    ui.providerNotice.textContent = provider?.notice || "修正案を作るサービスを選んでください。";
    ui.credentialStatus.textContent = gemini
      ? ready ? "公式Antigravity CLIを利用できます。認証・利用枠・モデルへのアクセスは修正案を作るときに確認します。"
        : provider?.cli_status === "unsupported_version" ? `導入済みのAntigravity CLIは ${provider.cli_version} です。対応するバージョンを導入してください。`
        : provider?.cli_installed ? "公式CLIは導入済みです。Antigravity CLIを開き、Googleアカウントでログインしてください。"
          : "公式Antigravity CLIが見つかりません。下の手順で導入後、アプリを開き直してください。"
      : provider?.configured
        ? `接続設定あり（${provider.credential_source === "environment" ? "環境変数" : provider.credential_source === "existing_cli" ? "既存の CLI 認証" : "OS の資格情報ストア"}）。実際に利用できるかは修正案を作るときに確認します。`
        : api ? "SarashinaのAPIキーは未設定です。キーを取得して入力してください。" : "Claude CLIが見つかりません。";
    ui.geminiSettings.hidden = !gemini;
    ui.keySettings.hidden = !api;
    ui.onboarding.hidden = !api || provider?.configured === true;
    if (!api) ui.apiKey.value = "";
    ui.apiKey.disabled = !api || config?.generation?.secret_store?.available === false;
    ui.removeKey.disabled = !api || settingsBusy;
    ui.geminiLogin.disabled = !gemini || !provider?.cli_installed || provider?.cli_status === "unsupported_version" || settingsBusy || Boolean(activeRequest);
    ui.providerSetup.textContent = api ? provider?.configured ? "キーの管理・再発行（Sarashina）" : "APIキーを取得（Sarashina）" : "導入とログインの案内";
    if (provider?.setup_url) ui.providerSetup.href = provider.setup_url;
    ui.generationNotice.textContent = ready
      ? `${provider.notice} 原文は自動保存しません。`
      : "修正案を作るサービスを設定してください。設定しなくても規則の点検やCodex・Claudeからの校正は利用できます。";
    if (api && config?.generation?.secret_store?.notice && config.generation.secret_store.available === false) {
      ui.credentialStatus.textContent += ` ${config.generation.secret_store.notice}`;
    }
    syncDeciderRequirement();
    updateCount();
  }

  function fillProviderModels(preferred) {
    const provider = selectedGenerator();
    ui.providerModel.replaceChildren(...(provider?.models || []).map((model) => {
      const option = node("option", "", model); option.value = model; return option;
    }));
    if (preferred && provider?.models.includes(preferred)) ui.providerModel.value = preferred;
    ui.providerModel.disabled = !provider || settingsBusy;
    renderGenerator();
  }

  function loadProviders(generation) {
    const selected = ui.provider.value || generation.provider;
    fillOptions(ui.provider, generation.providers);
    fillOptions(ui.quickProvider, generation.providers.map((provider) => ({
      id: provider.id, label: { sarashina: "Sarashina", gemini: "Gemini", claude: "Claude" }[provider.id] || provider.label,
    })));
    ui.provider.value = generation.providers.some((item) => item.id === selected) ? selected : generation.provider;
    fillProviderModels(generation.provider === ui.provider.value ? generation.model : undefined);
  }

  async function switchQuickProvider() {
    const previous = { provider: ui.provider.value, model: ui.providerModel.value, judge: ui.deciderToggle.checked };
    const target = config?.generation?.providers.find((item) => item.id === ui.quickProvider.value);
    if (!target || settingsBusy || activeRequest || activeExport || refreshingCapability || target.id === previous.provider) {
      ui.quickProvider.value = previous.provider;
      return;
    }
    const preferred = target.models.includes(target.model) ? target.model : target.models[0];
    if (!target.available || (target.id === "sarashina" && !deciderConfigured())) {
      pendingQuickSwitch = previous;
      ui.provider.value = target.id;
      fillProviderModels(preferred);
      setStatus(`${generatorLabel()}を使うための設定を確認してください。原文はそのままです。`);
      openSettings(true);
      updateButtons();
      return;
    }
    settingsBusy = true;
    updateButtons(); clearError();
    const operation = startBusy("校正AIを切り替えています。");
    try {
      // Switching never submits an API key or manuscript. Only explicit setup
      // can change credentials; the current result stays intact on failure.
      const generation = await readJson(await fetch("/api/settings", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ provider: target.id, model: preferred }),
      }));
      config.generation = generation;
      ui.provider.value = generation.provider;
      loadProviders(generation);
      invalidate();
      setStatus(`校正AIを${generatorLabel()}に切り替えました。原文はそのままです。`);
    } catch (error) {
      ui.quickProvider.value = previous.provider;
      showError(`校正AIを切り替えられませんでした。${error.message}`);
    } finally {
      stopBusy(operation); settingsBusy = false; renderGenerator(); updateButtons();
    }
  }

  async function saveProvider(remove = false) {
    if (!config || settingsBusy || activeRequest || activeExport || refreshingCapability) return;
    settingsBusy = true; updateButtons(); clearError();
    const operation = startBusy(remove ? "保存したAPIキーを削除しています。" : "修正案を作るサービスの設定を保存しています。");
    const payload = { provider: ui.provider.value, model: ui.providerModel.value };
    if (remove) payload.remove_key = true;
    else if (selectedGenerator()?.auth_method === "api_key" && ui.apiKey.value.trim()) payload.api_key = ui.apiKey.value.trim();
    try {
      const generation = await readJson(await fetch("/api/settings", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) }));
      config.generation = generation; loadProviders(generation);
      if (pendingQuickSwitch) { pendingQuickSwitch = null; invalidate(); }
      setStatus(remove ? "保存したAPIキーを削除しました。環境変数のキーは変更していません。" : "修正案を作るサービスの設定を保存しました。原文はそのままです。");
    } catch (error) { showError(error.message); }
    finally { stopBusy(operation); ui.apiKey.value = ""; settingsBusy = false; renderGenerator(); updateButtons(); }
  }

  async function updateCodexStatus() {
    try {
      const state = await readJson(await fetch("/api/integrations/codex"));
      codexAvailable = state.available === true;
      ui.codexStatus.textContent = state.needs_update ? "Codexに別の版・保存先、または無効な登録が残っています。この版へ登録し直してから、新しい会話で確認してください。"
        : state.current_registration && state.skill_installed ? "この版をCodexに登録済みです。新しい会話から利用できます。"
        : state.available ? "このPCのCodexへ、MCPと校正スキルを登録できます。既存設定はバックアップします。" : "Codex CLIが見つかりません。導入案内または下のMCP設定で登録してください。";
      ui.installCodex.disabled = !codexAvailable || Boolean(activeRequest) || Boolean(activeExport) || settingsBusy;
    } catch { ui.codexStatus.textContent = "Codex の登録状態を確認できませんでした。"; }
  }

  function updateButtons() {
    const ready = Boolean(config && currentSource().trim() && !activeRequest);
    ui.generateButton.disabled = !ready || settingsBusy || Boolean(activeExport) || refreshingCapability;
    ui.generateButton.querySelector(".button-main").textContent = "校正する";
    ui.generationToggle.disabled = !config || settingsBusy;
    syncDeciderRequirement();
    const providerLocked = !config || Boolean(activeRequest) || Boolean(activeExport) || settingsBusy || refreshingCapability;
    ui.quickProvider.disabled = providerLocked;
    ui.provider.disabled = providerLocked;
    ui.providerModel.disabled = providerLocked;
    ui.settingsClose.disabled = settingsBusy;
    ui.geminiLogin.disabled = selectedGenerator()?.id !== "gemini" || !selectedGenerator()?.cli_installed || selectedGenerator()?.cli_status === "unsupported_version" || Boolean(activeRequest) || settingsBusy;
    ui.saveProvider.disabled = !config || Boolean(activeRequest) || Boolean(activeExport) || settingsBusy || refreshingCapability;
    ui.releaseDecider.disabled = !config?.decider?.loaded || Boolean(activeRequest) || Boolean(activeExport) || settingsBusy;
    ui.deciderDevice.disabled = !deciderConfigured() || Boolean(activeRequest) || Boolean(activeExport) || settingsBusy || refreshingCapability;
    ui.saveDeciderDevice.disabled = ui.deciderDevice.disabled || ["busy", "loading"].includes(config?.decider?.status);
    ui.deciderBackend.disabled = !config || Boolean(activeRequest) || Boolean(activeExport) || settingsBusy || refreshingCapability;
    ui.saveDeciderBackend.disabled = ui.deciderBackend.disabled || ["busy", "loading"].includes(config?.decider?.status);
    ui.installCodex.disabled = !codexAvailable || Boolean(activeRequest) || Boolean(activeExport) || settingsBusy;
    ui.checkAiConnection.disabled = !config || Boolean(activeRequest) || Boolean(activeExport) || settingsBusy || refreshingCapability;
    ui.showMcpConfig.disabled = !config || settingsBusy;
    ui.copy.disabled = !run || requiredJudgmentPending() || Boolean(activeExport) || Boolean(activeRequest) || settingsBusy;
    ui.download.disabled = ui.copy.disabled;
    ui.refreshDecider.disabled = !config || Boolean(activeRequest) || Boolean(activeExport) || refreshingCapability || settingsBusy;
    ui.edits.querySelectorAll("button").forEach((button) => { button.disabled = Boolean(activeRequest); });
    byId("load-sample").disabled = Boolean(ui.source.value);
    byId("load-sample").title = ui.source.value ? "入力欄が空のときに例文を入れられます。" : "例文を入力する";
  }

  // The selected service may accept less text than local rules (e.g. Sarashina).
  function textLimit() {
    const provider = selectedGenerator();
    return ui.generationToggle.checked ? provider?.max_document_chars || provider?.max_text_chars || 12000 : config?.max_text || 12000;
  }

  function updateCount() {
    const provider = selectedGenerator();
    const count = Array.from(currentSource()).length;
    const limit = config ? textLimit() : 0;
    const over = Boolean(limit) && count > limit;
    const chunk = ui.generationToggle.checked && provider?.chunking === "paragraphs"
      ? ` · ${provider.max_text_chars.toLocaleString("ja-JP")}字以内ずつ` : "";
    ui.count.textContent = limit
      ? `${count.toLocaleString("ja-JP")} / ${limit.toLocaleString("ja-JP")} 字${over ? "（上限超過）" : ""}${chunk}`
      : `${count.toLocaleString("ja-JP")} 字`;
    ui.count.className = over ? "is-over" : "";
    ui.count.setAttribute("data-over-limit", String(over));
  }

  // Structured counts only; never infer held proposals from note wording.
  function heldCount(data) {
    return Number.isInteger(data?.held_edit_count) && data.held_edit_count > 0 ? data.held_edit_count : 0;
  }

  function riskFlags(edit) {
    return Array.isArray(edit.risk_flags) ? edit.risk_flags.filter((flag) => typeof flag === "string") : [];
  }

  function showHeldNotes() {
    ui.reviewDetails.open = true;
    ui.notes.scrollIntoView?.({ block: "nearest" });
    ui.notes.focus({ preventScroll: true });
  }

  function invalidate(message = "原文または設定が変わりました。もう一度「校正する」を押してください。") {
    revision += 1;
    const hadResults = Boolean(run || activeRequest || activeExport);
    if (activeRequest) activeRequest.abort();
    if (activeExport) activeExport.abort();
    activeRequest = null;
    activeTask = null;
    activeExport = null;
    stopBusy();
    run = null;
    ui.results.hidden = true;
    ui.outputPanel.hidden = false;
    ui.empty.hidden = false;
    ui.reviewDetails.open = false;
    ui.reviewDetails.hidden = true;
    ui.reviewCount.textContent = "まだ校正していません";
    ui.findings.replaceChildren();
    ui.edits.replaceChildren();
    ui.questions.replaceChildren();
    ui.suggestions.replaceChildren();
    outputRaw = "";
    ui.output.value = "";
    ui.preview.replaceChildren();
    ui.sourceHighlight.textContent = currentSource() + "\n";
    syncSourceScroll();
    ui.outputTitle.textContent = "校正後";
    ui.outputSummary.textContent = "";
    ui.changedCount.textContent = "";
    ui.meaningWarning.hidden = true;
    ui.meaningWarning.textContent = "";
    ui.comparisonSummary.textContent = "";
    ui.exportNote.hidden = true;
    ui.exportNote.textContent = "";
    ui.manualCopy.hidden = true;
    ui.manualCopyText.value = "";
    clearError();
    renderDeciderCapability();
    updateButtons();
    if (hadResults) setStatus(message);
  }

  function updateProfileDescription() {
    const profile = config?.profiles.find((item) => item.id === ui.profile.value);
    ui.profileDescription.textContent = profile?.description || "用途に合わせて文章を確認します。";
  }

  function fillOptions(select, values) {
    select.replaceChildren(...values.map((item) => {
      const option = document.createElement("option");
      option.value = item.id;
      option.textContent = item.label;
      return option;
    }));
    select.disabled = false;
  }

  async function readJson(response) {
    let data;
    try { data = await response.json(); }
    catch { throw new Error(`サーバーの応答を読み取れませんでした（HTTP ${response.status}）。`); }
    if (!response.ok) throw new Error(typeof data.error === "string" ? data.error : `リクエストに失敗しました（HTTP ${response.status}）。`);
    return data;
  }

  async function loadConfig() {
    clearError();
    const operation = startBusy("設定と接続状態を読み込んでいます。");
    setStatus("設定を読み込んでいます。");
    try {
      const next = await readJson(await fetch("/api/config", { headers: { Accept: "application/json" } }));
      if (!Array.isArray(next.profiles) || !next.profiles.length || !Array.isArray(next.strengths) || !next.strengths.length || !next.stages) {
        throw new Error("文章の用途や接続状態を読み取れませんでした。");
      }
      config = next;
      renderYomiyasu(next.yomiyasu_update);
      if (next.yomiyasu_update) refreshYomiyasu();
      fillOptions(ui.profile, next.profiles);
      fillOptions(ui.strength, next.strengths);
      const natural = next.strengths.find((item) => item.id === "natural");
      if (natural) ui.strength.value = natural.id;
      updateProfileDescription();
      stage(ui.rulesStage, next.stages.rules === "ready" ? "利用できます" : "未準備", next.stages.rules === "ready");
      loadProviders(next.generation);
      renderDeciderCapability();
      await updateCodexStatus();
      setStatus("文章を入力し、「校正する」を押してください。");
    } catch (error) {
      config = null;
      stage(ui.rulesStage, "読み込み失敗");
      stage(ui.generatorStage, "確認できません");
      renderDeciderCapability();
      ui.generationNotice.textContent = "設定を読み込めていません。上の案内から設定を読み直してください。";
      showError(`設定の読み込みに失敗しました。${error.message}`, true);
      setStatus("設定を読み直してから、もう一度お試しください。");
    }
    stopBusy(operation);
    updateButtons();
  }

  async function refreshDeciderCapability(announce = true) {
    if (!config || activeRequest || activeExport || refreshingCapability) return;
    refreshingCapability = true;
    const operation = startBusy("生成サービスとDeciderの接続状態を確認しています。");
    updateButtons();
    if (announce) clearError();
    try {
      const next = await readJson(await fetch("/api/config", { headers: { Accept: "application/json" } }));
      if (!next.stages) throw new Error("接続状態を読み取れませんでした。");
      config = { ...config, generation: next.generation, decider: next.decider, stages: { ...config.stages, decider: next.stages.decider } };
      loadProviders(next.generation);
      renderDeciderCapability();
      if (announce) setStatus("生成サービスとDeciderの接続状態を更新しました。原文と校正結果はそのままです。");
      return true;
    } catch (error) {
      if (announce) showError(`接続状態を更新できませんでした。${error.message}`);
      return false;
    } finally {
      refreshingCapability = false;
      stopBusy(operation);
      updateButtons();
    }
  }

  function validateEdits(edits, source) {
    if (!Array.isArray(edits)) throw new Error("修正候補の形式を読み取れませんでした。");
    const ids = new Set();
    const validated = edits.map((edit) => {
      if (!edit || typeof edit.id !== "string" || ids.has(edit.id) || !Number.isInteger(edit.start) || !Number.isInteger(edit.end)
        || edit.start < 0 || edit.end < edit.start || edit.end > source.length
        || typeof edit.before !== "string" || typeof edit.after !== "string"
        || source.slice(edit.start, edit.end) !== edit.before) {
        throw new Error("原文と修正候補の位置が一致しません。もう一度「校正する」を押してください。");
      }
      const splitsPair = (offset) => offset > 0 && offset < source.length
        && /[\uD800-\uDBFF]/.test(source[offset - 1]) && /[\uDC00-\uDFFF]/.test(source[offset]);
      if (splitsPair(edit.start) || splitsPair(edit.end)) throw new Error("文字の途中に修正位置があります。もう一度「校正する」を押してください。");
      ids.add(edit.id);
      return Object.freeze({ ...edit });
    });
    for (let index = 0; index < validated.length; index += 1) {
      if (validated.slice(0, index).some((prior) => editsOverlap(prior, validated[index]))) {
        throw new Error("同じ箇所に重なる修正があるため、結果を反映できません。もう一度校正してください。");
      }
    }
    if (Array.from(applyEdits(source, validated)).length > (config?.max_text || 12000)) {
      throw new Error("修正後の文章が12,000字を超えるため、反映できません。原文は残っています。");
    }
    return validated;
  }

  async function analyze() {
    if (!config || activeRequest || activeExport || settingsBusy || refreshingCapability || !currentSource().trim()) return;
    const generate = ui.generationToggle.checked;
    const judge = requiresDecider() || ui.deciderToggle.checked;
    if (generate && config.stages.generator !== "available") {
      showError("校正するには修正案を作るサービスを設定してください。", false, true);
      setStatus("設定を保存してから、もう一度「校正する」を押してください。");
      openSettings(true);
      return;
    }
    if (requiresDecider() && !deciderConfigured()) {
      showError("Sarashinaの校正にはDeciderの意味判定が必要です。接続状態とモデルの設定を確認してください。", false, true);
      setStatus("原文は送信していません。Deciderの準備ができてから、もう一度「校正する」を押してください。");
      openSettings();
      return;
    }
    const limit = textLimit();
    if (Array.from(currentSource()).length > limit) {
      showError(`この処理では ${limit.toLocaleString("ja-JP")} 字までです。原文を分けてください。`);
      return;
    }
    // Any input or setting change clears `run` through invalidate(), so a run
    // still shown here belongs to the current inputs. Keep it only to recover
    // from this attempt's failure; stale or aborted responses never restore it.
    const previous = requiredJudgmentPending() ? null : run;
    invalidate();
    const requestRevision = revision;
    const source = currentSource();
    const profile = ui.profile.value;
    const strength = ui.strength.value;
    const context = ui.context.value;
    const provider = ui.provider.value;
    const model = ui.providerModel.value;
    const current = { source, profile, strength, context, generate, judge, provider, model };
    const restorable = previous && Object.keys(current).every((key) => previous.snapshot[key] === current[key]) ? previous : null;
    const controller = new AbortController();
    activeRequest = controller;
    activeTask = "analyze";
    clearError();
    const requestId = newRequestId();
    const operation = startBusy(generate ? `${generatorLabel()} の校正を開始しています。` : "規則を確認しています。", { requestId, controller, source });
    ui.sourceHelp.textContent = "校正中に編集すると結果表示を中止します。受理済みの処理は続く場合があります。";
    if (globalThis.crypto?.subtle) {
      globalThis.crypto.subtle.digest("SHA-256", new TextEncoder().encode(source)).then((bytes) => {
        if (progressIsCurrent(operation)) operation.sourceHash = Array.from(new Uint8Array(bytes), (value) => value.toString(16).padStart(2, "0")).join("");
      }).catch(() => {}); // Cosmetic motion is optional; analysis/export checks remain mandatory.
    }
    updateButtons();
    ui.reviewCount.textContent = "確認しています";
    setStatus(generate
      ? judge
        ? `${generatorLabel()} に原文を送り、修正後に Decider で意味の変化を確認します。`
        : `${generatorLabel()} に原文を送り、校正しています。意味判定はオフです。`
      : "外部へ送信せず、この PC で規則を確認しています。修正案の生成と意味判定は実行しません。");
    try {
      const response = await fetch("/api/analyze", {
        method: "POST", headers: { "Content-Type": "application/json", Accept: "application/json", "X-Yebisu-Request-Id": requestId },
        body: JSON.stringify({ text: source, profile, strength, context, generate, judge,
          ...(generate ? { provider, model } : {}) }), signal: controller.signal,
      });
      const data = await readJson(response);
      if (revision !== requestRevision || controller.signal.aborted) return;
      if (data.offset_encoding !== "utf-16" || typeof data.source_hash !== "string"
        || data.profile !== profile || data.strength !== strength || !data.stages
        || !Array.isArray(data.findings) || !Array.isArray(data.questions)) {
        throw new Error("校正結果の形式が一致しません。ページを読み直して、もう一度「校正する」を押してください。");
      }
      const edits = validateEdits(data.edits, source);
      run = {
        snapshot: Object.freeze({ source, profile, strength, context, generate, judge, provider, model, sourceHash: data.source_hash }),
        data: { ...data, edits },
      };
      renderResults();
      const held = heldCount(data);
      const heldText = held ? `保留した修正案が ${held} 件あり、その箇所は原文のままです。理由は「変更の詳細」の注記をご覧ください。` : "";
      setStatus(requiredJudgmentPending() ? "修正候補を残しました。意味判定が終わっていないため、コピー・保存はできません。接続状態を確認して、もう一度校正してください。"
        : edits.length ? `${edits.length} 件の変更を右の文章へ反映しました。${heldText}コピー・保存できます。`
        : held ? `校正が終わりました。${heldText}`
        : data.generator?.result === "unchanged_proposals_only" ? "AIから有効な修正案を得られませんでした。原文のまま表示しています。見逃しの可能性があります。"
        : generate ? "修正候補は0件でした。原文を表示しています。誤りがないことを保証する結果ではありません。" : "Yomiyasuの規則点検が終わりました。原文をそのまま右に表示しています。");
    } catch (error) {
      if (error.name === "AbortError" || revision !== requestRevision) return;
      if (restorable) {
        run = { ...restorable, restored: true };
        renderResults();
        showError(`今回の校正は失敗しました。${error.message} 右には、同じ原文と設定で前回成功した結果を表示しています。`, false, generate);
        setStatus("今回の校正は失敗したため、前回成功した結果に戻しました。原文と設定は変わっていません。");
        return;
      }
      showError(`校正できませんでした。${error.message}`, false, generate);
      setStatus(generate ? "原文は残っています。エラーの案内を確認して、必要に応じて文章を分けて再実行してください。" : "原文は残っています。入力と接続を確認してください。");
      ui.reviewCount.textContent = "校正できませんでした";
    } finally {
      if (activeRequest === controller) { activeRequest = null; activeTask = null; }
      stopBusy(operation);
      renderDeciderCapability();
      updateButtons();
    }
  }

  function renderResults() {
    ui.empty.hidden = true;
    ui.results.hidden = false;
    ui.outputPanel.hidden = false;
    ui.reviewDetails.hidden = false;
    const { data } = run;
    const judgmentPending = requiredJudgmentPending();
    const held = heldCount(data);
    ui.reviewCount.textContent = `${run.restored ? "前回の結果 · " : ""}${judgmentPending ? "意味判定が未完了 · 候補" : "変更"} ${data.edits.length} 件${held ? ` · 保留 ${held} 件` : ""}`;
    if (held) ui.reviewDetails.open = true;
    const executed = data.edits.filter((edit) => edit.decision?.status === "executed").length;
    const preserved = data.edits.filter((edit) => edit.decision?.status === "executed" && edit.decision.choice === "preserved").length;
    const risky = data.edits.filter((edit) => riskFlags(edit).length).length;
    ui.comparisonSummary.textContent = !data.edits.length ? ""
      : executed === data.edits.length
        ? preserved === executed
          ? `Decider：${executed} 件で意味を保っていると判定${risky ? `（うち ${risky} 件は数値・否定などの変化を検知。詳細を確認）` : ""}`
          : `Decider：${executed} 件を判定・詳細を確認`
        : executed ? `Decider：${executed} / ${data.edits.length} 件を判定` : "Decider：判定未実行";
    const generationLabels = {
      executed: "作成した修正案を右の文章に反映しました。",
      not_requested: "修正案の生成はオフです。規則を確認し、原文をそのまま表示しています。",
      unavailable: "規則の点検結果です。今回は修正案を作成できませんでした。",
      host_supplied: "既存の修正案を点検した結果です。",
    };
    const judgmentLabels = {
      executed: "この PC の Decider で判定しました。モデルの判定は意味の保持を保証するものではありません。",
      partial: "一部の候補は Decider で判定できませんでした。各候補の状態を確認してください。",
      not_run: "今回、Decider による判定は実行していません。",
      unavailable: "今回はローカル Decider を利用できず、判定を実行していません。",
      not_connected: "Decider は未接続です。今回の候補は判定していません。",
      input_rejected: "入力の長さなどのため、Decider の判定を保留した候補があります。各候補の案内をご覧ください。",
      deferred: "Decider の判定を保留しています。各候補の状態と案内をご覧ください。",
    };
    ui.summary.textContent = judgmentPending
      ? "修正候補を右の文章に表示しています。意味判定が終わるまでコピー・保存はできません。"
      : generationLabels[data.stages.generation] || "校正結果を確認してください。";
    if (data.yomiyasu?.rules === "executed") {
      const version = typeof data.yomiyasu.version === "string" ? ` ${data.yomiyasu.version}` : "";
      const guidance = data.yomiyasu.generation_guidance;
      const included = data.stages.generation === "executed" && guidance?.status === "included";
      const note = included
        ? "推敲方針の全文と参照資料を生成モデルへ渡しました。"
        : guidance?.status === "not_sent" ? "このアプリから生成用の方針は送信していません。" : "推敲方針の送信記録はありません。";
      ui.summary.append(node("p", "", `Yomiyasu${version}：規則点検を実行。${note}`));
    }
    ui.summary.append(node("p", "", judgmentLabels[data.stages.decider] || "Decider の実行状態を確認できません。各候補の表示をご覧ください。"));
    if (typeof data.generator_model === "string" && data.generator_model) {
      ui.summary.append(node("p", "", `修正案の生成モデル：${data.generator_model}`));
    }
    renderFindings();
    renderEdits();
    renderQuestions();
    renderSuggestions();
    ui.notes.replaceChildren();
    const notes = Array.isArray(data.notes) ? data.notes.filter((item) => typeof item === "string") : [];
    ui.notes.hidden = notes.length === 0;
    notes.forEach((note) => ui.notes.append(node("p", "", note)));
    updateOutput();
  }

  function renderFindings() {
    const { findings } = run.data;
    ui.findingsCount.textContent = String(findings.length);
    ui.findings.replaceChildren();
    if (!findings.length) {
      ui.findings.append(node("p", "empty-note", "今回の規則点検では、指摘はありません。文章全体の品質を保証するものではありません。"));
      return;
    }
    findings.forEach((finding) => {
      const item = node("div", "finding");
      const severity = { error: "要確認", warning: "確認のすすめ", info: "参考" }[finding.severity] || "確認のすすめ";
      const position = Number.isInteger(finding.line) ? `${finding.line} 行目 · ` : "";
      item.append(node("p", "finding-meta", `${position}${severity}${finding.rule ? ` · ${finding.rule}` : ""}`));
      item.append(node("p", "finding-message", finding.message || "内容を確認してください。"));
      ui.findings.append(item);
    });
  }

  function renderEdits() {
    const { edits } = run.data;
    const judgmentPending = requiredJudgmentPending();
    ui.editsCount.textContent = String(edits.length);
    ui.edits.replaceChildren();
    ui.editGuidance.hidden = edits.length === 0;
    ui.editGuidance.textContent = judgmentPending
      ? "修正候補を右に表示しています。意味判定が終わるまでコピー・保存はできません。"
      : "変更は右の文章に反映済みです。理由と意味判定を確認できます。";
    const held = heldCount(run.data);
    const heldNote = held ? `保留した修正案が ${held} 件あります。その箇所は原文のままで、右の文章には反映していません。理由は下の注記をご覧ください。` : "";
    if (!edits.length) {
      ui.edits.append(node("p", "empty-note", held ? `反映した修正案はありません。${heldNote}`
        : run.data.stages.generation === "not_requested"
          ? "今回は文章を置き換える修正案はありません。サービスを設定すると、修正案の作成も依頼できます。"
          : "文章を置き換える修正案はありません。確認したい点があれば、上の指摘をご覧ください。"));
      return;
    }
    if (held) ui.edits.append(node("p", "empty-note held-note", heldNote));
    edits.forEach((edit, index) => {
      const preserved = edit.decision?.status === "executed" && edit.decision?.choice === "preserved";
      const risks = riskFlags(edit);
      const attention = !preserved || risks.length > 0;
      const card = node("article", `edit-card is-applied${attention ? " has-attention" : ""}${risks.length ? " has-risk" : ""}`);
      card.dataset.editId = edit.id;
      const top = node("div", "edit-card-top");
      top.append(node("h4", "edit-label", `${judgmentPending ? "修正候補" : "変更"} ${index + 1}${edit.rule ? ` · ${edit.rule}` : ""}`));
      top.append(node("span", "edit-state", judgmentPending ? "候補を表示 · 意味判定が未完了" : !preserved ? "変更済み · 意味判定を確認" : risks.length ? "変更済み · 検知した変化を確認" : "変更済み"));
      const diff = node("div", "edit-diff");
      const before = node("div", "diff-part");
      before.append(node("span", "diff-label", "原文"), node("del", "diff-before", edit.before || "（挿入位置）"));
      const after = node("div", "diff-part");
      after.append(node("span", "diff-label", "修正後"), node("ins", "diff-after", edit.after || "（削除）"));
      diff.append(before, after);
      const reason = node("p", "edit-reason");
      reason.append(node("span", "edit-reason-label", "修正案を作った理由"), node("span", "", edit.reason || "変更した箇所を原文と読み比べてください。"));
      card.append(top, diff, reason, renderModelDecision(edit.decision));
      if (edit.decision?.status === "executed" && ["shifted", "needs_review"].includes(edit.decision.choice)) {
        card.append(node("p", "edit-warning", "意味の変化に注意：この変更も右の文章に反映しています。前後の文と読み比べてください。"));
      }
      if (preserved && risks.length) {
        card.append(node("p", "edit-warning", "Decider は意味を保つと判定しましたが、下の変化を機械的に検知しました。判定は意味の保持を保証しません。この変更も右の文章に反映しています。前後の文と読み比べてください。"));
      }
      if (risks.length) {
        const riskNotes = node("div", "risk-notes");
        riskNotes.append(node("p", "risk-notes-title", "確認したい変化"));
        const list = node("ul");
        risks.forEach((flag) => list.append(node("li", "", flag)));
        riskNotes.append(list);
        card.append(riskNotes);
      }
      ui.edits.append(card);
    });
  }

  function renderModelDecision(decision) {
    const labels = { preserved: "意味を保っていると判定", shifted: "意味が変わる可能性", needs_review: "判断保留" };
    const executed = decision?.status === "executed" && Object.hasOwn(labels, decision.choice);
    const kind = executed ? decision.choice.replaceAll("_", "-") : decision?.status === "error" ? "error" : "not-run";
    const box = node("div", `model-decision is-${kind}`);
    const label = executed ? labels[decision.choice] : decision?.status === "error" ? "判定できませんでした" : "未判定";
    box.append(node("p", "model-decision-title", `Decider：${label}`));
    if (executed) {
      const model = typeof decision.model === "string" ? decision.model : "Decider";
      box.append(node("p", "model-decision-meta", `ローカル / ${model} / ${precisionDeviceLabel(decision)}。モデルによる判断です。原文と読み比べてください。`));
      if (decision.scope === "excerpt") {
        box.append(node("p", "model-decision-meta", "判定範囲：変更箇所と前後の抜粋。文書全体の意味保持を判定したものではありません。"));
      } else if (decision.scope === "document") {
        box.append(node("p", "model-decision-meta", "判定範囲：原文の全文と、この候補を反映した文章。"));
      }
      if (decision.quantization === "Q8_0") {
        box.append(node("p", "model-decision-meta", "公式の8bit量子化モデルです。従来版と判定が異なる場合があります。"));
      } else if (decision.precision_override === true) {
        box.append(node("p", "model-decision-meta", "CPU の標準実行（FP32）と異なる精度です。判定結果が一致するかは未検証です。"));
      }
    } else {
      const error = decisionErrorMessage(decision?.error);
      if (error) box.append(node("p", "model-decision-meta", error));
      box.append(node("p", "model-decision-meta", isInputRejected(decision)
        ? "原文を分けて、もう一度「校正する」を押してください。接続状態の更新だけでは解消しません。"
        : decision?.status === "error" || error
          ? requiredJudgmentPending() ? "修正候補は右に残しています。接続状態やエラーを確認して、もう一度校正してください。"
            : "変更は右に反映しています。接続状態やエラーを確認し、必要ならもう一度校正してください。"
          : "Decider による意味の判定は実行していません。"));
    }
    return box;
  }

  function editsOverlap(a, b) {
    if (a.start === a.end && b.start === b.end) return a.start === b.start;
    if (a.start === a.end) return a.start >= b.start && a.start < b.end;
    if (b.start === b.end) return b.start >= a.start && b.start < a.end;
    return a.start < b.end && b.start < a.end;
  }

  function renderQuestions() {
    const { questions } = run.data;
    ui.questionsSection.hidden = questions.length === 0;
    ui.questionsCount.textContent = String(questions.length);
    ui.questions.replaceChildren();
    questions.forEach((question) => {
      const card = node("div", "question-card");
      if (question.quote) card.append(node("blockquote", "question-quote", question.quote));
      card.append(node("p", "question-text", question.question || "この部分の意図を確認してください。"));
      ui.questions.append(card);
    });
  }

  function renderSuggestions() {
    const suggestions = Array.isArray(run.data.suggestions) ? run.data.suggestions : [];
    ui.suggestionsSection.hidden = suggestions.length === 0;
    ui.suggestionsCount.textContent = String(suggestions.length);
    ui.suggestions.replaceChildren();
    suggestions.forEach((suggestion) => {
      const card = node("div", "suggestion-card");
      if (suggestion.quote) card.append(node("blockquote", "suggestion-quote", suggestion.quote));
      card.append(node("p", "suggestion-text", suggestion.suggestion || "この部分の構成を確認してください。"));
      ui.suggestions.append(card);
    });
  }

  function updateOutput() {
    if (!run) return;
    const edits = run.data.edits;
    const judgmentPending = requiredJudgmentPending();
    const held = heldCount(run.data);
    outputRaw = applyEdits(run.snapshot.source, edits);
    ui.output.value = outputRaw;
    paintComparison(ui.preview, comparisonParts(run.snapshot.source, edits, "after"), "change-added");
    paintComparison(ui.sourceHighlight, comparisonParts(run.snapshot.source, edits, "before"), "change-removed");
    ui.sourceHighlight.append(document.createTextNode("\n"));
    syncSourceScroll();
    ui.outputTitle.textContent = judgmentPending ? "修正候補（意味判定が未完了）" : run.restored ? "校正後（前回の結果）" : "校正後";
    ui.preview.setAttribute("aria-label", judgmentPending ? "意味判定が未完了の修正候補の全文" : run.restored ? "前回成功した校正結果の全文" : "変更を反映した文章の全文");
    ui.changedCount.textContent = `${judgmentPending ? "候補" : "変更"} ${edits.length} 件${held ? ` · 保留 ${held} 件` : ""}`;
    const restored = run.restored ? "今回の校正は失敗したため、前回成功した結果を表示しています。" : "";
    ui.outputSummary.textContent = restored + (judgmentPending
      ? "修正候補を表示しています。意味判定が終わるまでコピー・保存はできません。"
      : edits.length ? "変更を反映しました。原文は左に残っています。"
      : held ? `保留した修正案 ${held} 件の箇所を含め、原文のまま表示しています。`
      : "変更はありません。原文と同じ文章です。");
    const shifted = edits.filter((edit) => edit.decision?.status === "executed" && edit.decision.choice === "shifted").length;
    const deferred = edits.filter((edit) => edit.decision?.status === "executed" && edit.decision.choice === "needs_review").length;
    const errors = edits.filter((edit) => edit.decision?.status === "error").length;
    const judged = edits.filter((edit) => edit.decision?.status === "executed"
      && ["preserved", "shifted", "needs_review"].includes(edit.decision.choice)).length;
    const unrun = edits.length - judged - errors;
    const risky = edits.filter((edit) => riskFlags(edit).length).length;
    const notices = [];
    if (shifted) notices.push(`意味が変わる可能性 ${shifted} 件`);
    if (deferred) notices.push(`判断保留 ${deferred} 件`);
    if (errors) notices.push(`意味判定エラー ${errors} 件`);
    if (unrun) notices.push(`意味判定未実行 ${unrun} 件`);
    if (risky) notices.push(`確認したい変化を検知 ${risky} 件`);
    const messages = [];
    if (judgmentPending) messages.push("修正候補は意味判定が未完了です。コピー・保存は保留しています。詳細で判定できなかった理由を確認できます。");
    else if (notices.length) messages.push(`${notices.join(" · ")}。これらの変更も反映済みです。`);
    if (held) messages.push(`保留した修正案 ${held} 件は反映せず、原文のままにしています。`);
    if (!judgmentPending && messages.length) messages.push(held ? "理由は「変更の詳細」の注記で確認できます。" : "詳細で原文と理由を確認できます。");
    ui.meaningWarning.hidden = messages.length === 0;
    ui.meaningWarning.textContent = messages.join("");
    if (held) {
      const link = node("button", "text-button warning-link", "保留の理由を見る");
      link.type = "button";
      link.addEventListener("click", showHeldNotes);
      ui.meaningWarning.append(document.createTextNode(" "), link);
    }
    if (judgmentPending) {
      ui.comparisonSummary.textContent = `Decider：${judged} / ${edits.length} 件を判定。未完了のためコピー・保存は保留`;
    } else if (!edits.length) {
      ui.comparisonSummary.textContent = run.snapshot.judge
        ? held ? `反映した修正案がないため、Decider の意味判定は実行していません（保留 ${held} 件は判定対象外）。`
          : "修正案がないため、Decider の意味判定は実行していません。"
        : "Decider の意味判定はオフです。";
    } else if (!run.snapshot.judge) {
      ui.comparisonSummary.textContent = "Decider の意味判定はオフです。変更の意味は未判定です。";
    }
    updateButtons();
  }

  ui.source.addEventListener("input", () => { currentSource(); updateCount(); invalidate(); });
  ui.source.addEventListener("scroll", syncSourceScroll);
  ui.context.addEventListener("input", () => invalidate());
  ui.profile.addEventListener("change", () => { updateProfileDescription(); invalidate(); });
  ui.strength.addEventListener("change", () => invalidate());
  byId("load-sample").addEventListener("click", () => {
    if (ui.source.value) return;
    setSourceText(sample);
    updateCount();
    invalidate();
    setStatus("例文を入力しました。用途と校正の強さを選んで校正できます。");
    ui.source.focus();
  });
  ui.generateButton.addEventListener("click", analyze);
  ui.generationToggle.addEventListener("change", () => { invalidate(); renderGenerator(); });
  ui.deciderToggle.addEventListener("change", () => { syncDeciderRequirement(); invalidate(); });
  ui.refreshDecider.addEventListener("click", () => refreshDeciderCapability());
  ui.retryConfig.addEventListener("click", loadConfig);
  ui.settingsOpen.addEventListener("click", () => openSettings());
  ui.aiConnectionsOpen.addEventListener("click", () => { openSettings(); ui.aiConnections.scrollIntoView?.({ block: "start" }); ui.aiConnections.focus({ preventScroll: true }); });
  ui.checkAiConnection.addEventListener("click", async () => {
    if (!config || activeRequest || activeExport || settingsBusy || refreshingCapability) return;
    settingsBusy = true; updateButtons();
    const operation = startBusy("MCPの起動と規則点検を確認しています。原稿は使いません。");
    ui.aiConnectionStatus.textContent = "このPCのMCPを起動して点検しています。最大30秒程度かかります。";
    try {
      const state = await readJson(await fetch("/api/integrations/check", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" }));
      if (state.status !== "passed" || state.rules !== "executed" || state.generation !== "not_requested" || state.decider !== "not_run") throw new Error("点検結果を確認できませんでした。");
      ui.aiConnectionStatus.textContent = `MCP v${state.version} · 起動・規則点検を確認しました。AI側で新しい会話を開き、ツールの接続を確認してください。`;
    } catch (error) { ui.aiConnectionStatus.textContent = `接続を確認できませんでした。${error.message}`; }
    finally { stopBusy(operation); settingsBusy = false; updateButtons(); }
  });
  ui.showMcpConfig.addEventListener("click", async () => {
    ui.showMcpConfig.disabled = true;
    try {
      const state = await readJson(await fetch("/api/integrations/mcp"));
      if (!state.mcpServers?.["yebisu-japanese-editor"]?.command) throw new Error("登録用設定を確認できませんでした。");
      ui.mcpConfigText.value = JSON.stringify(state, null, 2);
      ui.mcpConfigBox.hidden = false;
    } catch (error) { ui.aiConnectionStatus.textContent = `MCP設定を読み取れませんでした。${error.message}`; }
    finally { updateButtons(); }
  });
  ui.copyMcpConfig.addEventListener("click", async () => {
    if (!ui.mcpConfigText.value) return;
    ui.mcpConfigText.focus(); ui.mcpConfigText.select();
    try { await navigator.clipboard.writeText(ui.mcpConfigText.value); ui.aiConnectionStatus.textContent = "MCP設定をコピーしました。AI側の設定へ登録してください。"; }
    catch { ui.aiConnectionStatus.textContent = "設定を選択しました。Ctrl+C（Macは⌘C）でコピーし、AI側へ登録してください。"; }
  });
  ui.setupPrompt.addEventListener("click", () => openSettings(true));
  ui.settingsClose.addEventListener("click", () => { if (!settingsBusy) ui.settingsDialog.close(); });
  ui.settingsDialog.addEventListener("click", (event) => { if (event.target === ui.settingsDialog && !settingsBusy) ui.settingsDialog.close(); });
  ui.settingsDialog.addEventListener("cancel", (event) => { if (settingsBusy) event.preventDefault(); });
  ui.settingsDialog.addEventListener("close", () => {
    if (!pendingQuickSwitch) return;
    ui.provider.value = pendingQuickSwitch.provider;
    ui.deciderToggle.checked = pendingQuickSwitch.judge;
    fillProviderModels(pendingQuickSwitch.model);
    pendingQuickSwitch = null;
    renderDeciderCapability(); updateButtons();
    setStatus("校正AIの切り替えをやめました。原文と校正結果はそのままです。");
  });
  ui.errorSettings.addEventListener("click", () => openSettings(true));
  ui.errorLogin.addEventListener("click", () => { openSettings(); ui.geminiLogin.focus(); });
  async function exportResult(kind) {
    if (!run || requiredJudgmentPending() || activeExport || activeRequest || settingsBusy) return;
    const exportRun = run;
    const exportRevision = revision;
    const finalText = outputRaw;
    const controller = new AbortController();
    activeExport = controller;
    const requestId = newRequestId();
    const operation = startBusy("Yomiyasuで全文を点検しています。原文と校正後の文章を確認しています。", { requestId, controller });
    clearError();
    ui.exportNote.hidden = true;
    ui.manualCopy.hidden = true;
    ui.manualCopyText.value = "";
    updateButtons();
    setStatus("Yomiyasuで書き出す文章を点検し、本文を照合しています。");
    let verifiedText = null;
    try {
      const response = await fetch("/api/export", {
        method: "POST", headers: { "Content-Type": "application/json", Accept: "application/json", "X-Yebisu-Request-Id": requestId },
        body: JSON.stringify({ text: exportRun.snapshot.source, final: finalText, profile: exportRun.snapshot.profile,
          yomiyasu_revision: exportRun.data.yomiyasu?.commit }),
        signal: controller.signal,
      });
      const data = await readJson(response);
      if (run !== exportRun || revision !== exportRevision || controller.signal.aborted) return;
      if (data.text !== finalText || data.source_hash !== exportRun.snapshot.sourceHash
        || typeof data.final_hash !== "string" || data.check?.status !== "executed" || data.verification?.status !== "executed") {
        throw new Error("検査した文章と現在の文章が一致しません。もう一度「校正する」を押してください。");
      }
      ui.exportNote.textContent = typeof data.note === "string" ? data.note : "Yomiyasuの全文点検と本文照合が完了しました。意味保持や読みやすさの合格判定ではありません。";
      ui.exportNote.hidden = false;
      verifiedText = data.text;
      if (kind === "copy") {
        if (!navigator.clipboard?.writeText) throw new Error("このブラウザーではクリップボードを利用できません。テキストファイルでの保存をお試しください。");
        let clipboardTimer;
        try {
          await Promise.race([
            navigator.clipboard.writeText(data.text),
            new Promise((_, reject) => {
              clipboardTimer = setTimeout(() => reject(new Error("クリップボードへのコピーが完了しませんでした。このタブを開いてブラウザーの許可を確認するか、テキストファイルで保存してください。")), 15000);
            }),
          ]);
        } finally {
          clearTimeout(clipboardTimer);
        }
        if (run !== exportRun || revision !== exportRevision || controller.signal.aborted) return;
        setStatus("Yomiyasuの全文点検・本文照合を実行し、校正後の文章をコピーしました。");
      } else {
        const blob = new Blob([data.text], { type: "text/plain;charset=utf-8" });
        const url = URL.createObjectURL(blob);
        const link = document.createElement("a");
        link.href = url;
        link.download = "YomiyasuStudio校正結果.txt";
        document.body.append(link);
        link.click();
        link.remove();
        setTimeout(() => URL.revokeObjectURL(url), 1000);
        setStatus("Yomiyasuの全文点検・本文照合を実行し、テキストファイルの保存を開始しました。");
      }
    } catch (error) {
      if (error.name === "AbortError" || run !== exportRun || revision !== exportRevision) return;
      if (kind === "copy" && verifiedText !== null) {
        ui.manualCopyText.value = verifiedText;
        ui.manualCopy.hidden = false;
      }
      showError(`書き出せませんでした。${error.message} 原文と校正結果は残っています。`);
      setStatus("文章は書き出していません。エラーの内容を確認し、もう一度お試しください。");
    } finally {
      if (activeExport === controller) activeExport = null;
      stopBusy(operation);
      updateButtons();
    }
  }

  ui.quickProvider.addEventListener("change", switchQuickProvider);
  ui.provider.addEventListener("change", () => { fillProviderModels(); invalidate(); updateButtons(); });
  ui.providerModel.addEventListener("change", () => invalidate());
  ui.saveProvider.addEventListener("click", () => saveProvider());
  ui.removeKey.addEventListener("click", () => saveProvider(true));
  ui.geminiLogin.addEventListener("click", async () => {
    if (activeRequest || activeExport || settingsBusy || refreshingCapability) return;
    settingsBusy = true; updateButtons(); clearError();
    const operation = startBusy("Googleログイン用の公式CLIを開いています。");
    try {
      const state = await readJson(await fetch("/api/gemini/login", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" }));
      setStatus(state.notice);
    } catch (error) { showError(error.message); }
    finally { stopBusy(operation); settingsBusy = false; renderGenerator(); updateButtons(); }
  });
  ui.installCodex.addEventListener("click", async () => {
    if (activeRequest || activeExport || settingsBusy || refreshingCapability) return;
    settingsBusy = true;
    ui.installCodex.disabled = true; ui.codexStatus.textContent = "Codexへ登録しています。";
    const operation = startBusy("Codexへ校正スキルと接続を登録しています。");
    updateButtons();
    try {
      const state = await readJson(await fetch("/api/integrations/codex", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" }));
      ui.codexStatus.textContent = state.notice;
    } catch (error) { ui.codexStatus.textContent = error.message; }
    finally { stopBusy(operation); settingsBusy = false; updateButtons(); }
  });
  ui.releaseDecider.addEventListener("click", async () => {
    if (activeRequest || activeExport || settingsBusy || refreshingCapability) return;
    settingsBusy = true;
    ui.releaseDecider.disabled = true;
    const operation = startBusy("判定モデルのメモリを解放しています。");
    updateButtons();
    try {
      await readJson(await fetch("/api/decider/release", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" }));
      await refreshDeciderCapability(false); setStatus("判定モデルのメモリを解放しました。次の判定時に読み込みます。");
    } catch (error) { showError(error.message); }
    finally { stopBusy(operation); settingsBusy = false; }
    updateButtons();
  });
  ui.saveDeciderDevice.addEventListener("click", async () => {
    if (activeRequest || activeExport || settingsBusy || refreshingCapability) return;
    const device = ui.deciderDevice.value;
    settingsBusy = true;
    ui.deciderDeviceStatus.textContent = "空きメモリと実行環境を確認しています。モデルは読み込みません。";
    const operation = startBusy("PPDDの実行先と空きメモリを確認しています。");
    updateButtons();
    try {
      const response = await fetch("/api/decider/settings", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ device }) });
      const result = await response.json();
      if (!response.ok) {
        const details = result.details?.details;
        const resources = [];
        for (const [label, free, needed] of [["空きRAM", details?.ram_available_bytes, details?.ram_required_bytes], ["空きVRAM", details?.cuda_available_bytes, details?.cuda_required_bytes], ["コミット余裕", details?.commit_available_bytes, details?.commit_required_bytes]]) {
          if (Number.isFinite(free) && Number.isFinite(needed)) resources.push(`${label} ${(free / 2 ** 30).toFixed(1)}GiB / 必要 ${(needed / 2 ** 30).toFixed(1)}GiB`);
        }
        const releaseHint = config?.decider?.loaded && details?.ram_available_bytes < details?.ram_required_bytes
          ? " 「判定モデルのメモリを解放」してから再試行できます。" : "";
        throw new Error(`${result.error || "設定を保存できませんでした。"}${resources.length ? ` ${resources.join(" · ")}。` : ""} 実行先は変更していません。モデルが解放済みの場合は、次の判定時に読み込みます。${releaseHint}`);
      }
      ui.deciderDeviceStatus.textContent = result.status === "unchanged" ? "この実行先で設定済みです。" : `${deviceLabel(device)}に設定しました。次の判定から使います。`;
    } catch (error) { ui.deciderDeviceStatus.textContent = error.message; }
    finally {
      stopBusy(operation); settingsBusy = false; deciderDeviceDirty = false;
      await refreshDeciderCapability(false); updateButtons();
    }
  });
  ui.deciderDevice.addEventListener("change", () => {
    deciderDeviceDirty = true;
    ui.deciderDeviceStatus.textContent = "実行先の変更は未保存です。「実行先を保存」で確認して反映します。";
  });
  ui.deciderBackend.addEventListener("change", () => {
    deciderBackendDirty = true;
    ui.deciderBackendStatus.textContent = "実行方式の変更は未保存です。切り替える方式の準備を先に行ってください。";
  });
  ui.saveDeciderBackend.addEventListener("click", async () => {
    if (activeRequest || activeExport || settingsBusy || refreshingCapability) return;
    const backend = ui.deciderBackend.value;
    if (!config?.decider?.prepared_backends?.[backend] && backend !== config?.decider?.runtime_backend) {
      ui.deciderBackendStatus.textContent = "この実行方式は未準備です。導入案内に従って準備し、接続状態を更新してください。";
      return;
    }
    settingsBusy = true;
    const operation = startBusy("PPDDの実行方式と空きメモリを確認しています。");
    ui.deciderBackendStatus.textContent = "現在の判定モデルを解放し、切り替え先を確認しています。";
    updateButtons();
    try {
      const response = await fetch("/api/decider/settings", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ backend }) });
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || "実行方式を保存できませんでした。");
      ui.deciderBackendStatus.textContent = result.status === "unchanged" ? "この実行方式で設定済みです。" : `${backend === "llama_cpp" ? "C++・公式Q8" : "従来版・PyTorch"}に設定しました。次の判定から使います。`;
    } catch (error) { ui.deciderBackendStatus.textContent = error.message; }
    finally {
      stopBusy(operation); settingsBusy = false; deciderBackendDirty = false;
      await refreshDeciderCapability(false); updateButtons();
    }
  });
  byId("open-text").addEventListener("change", async (event) => {
    const file = event.target.files?.[0]; if (!file) return;
    try {
      if (file.size > 100000) throw new Error("ファイルが大きすぎます。12,000字までの文章を選んでください。");
      const value = await file.text();
      if (Array.from(value).length > (config?.max_text || 12000)) throw new Error("本文は12,000字までです。文章を分けてください。");
      setSourceText(value); invalidate(); updateCount(); updateButtons(); setStatus("テキストを開きました。元のファイルは変更していません。");
    } catch (error) { showError(error.message); }
    event.target.value = "";
  });

  ui.copy.addEventListener("click", () => exportResult("copy"));
  ui.download.addEventListener("click", () => exportResult("download"));
  ui.selectCopyText.addEventListener("click", () => { ui.manualCopyText.focus(); ui.manualCopyText.select(); });

  updateCount();
  updateButtons();
  window.addEventListener("yebisu-service-reconnected", async () => {
    if (activeRequest || activeExport) return;
    if (!config) return loadConfig();
    if (await refreshDeciderCapability(false)) {
      clearError();
      await updateCodexStatus();
      setStatus("接続が戻りました。原文と校正結果はそのままです。");
    }
  });
  loadConfig();
})();
