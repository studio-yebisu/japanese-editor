"""Explicit Gemini CLI or official Sarashina API; never switch providers."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
import json
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

import provider_settings
import gemini_cli
import progress

MAX_RESPONSE_BYTES = 2_000_000
MAX_REQUEST_BYTES = 1_000_000
TIMEOUT_SECONDS = 180
SARASHINA_ENDPOINT = "https://api.sarashina.ai/v1/chat/completions"
SARASHINA_INTERVAL_SECONDS = 10.0
SARASHINA_OUTPUT_TOKENS = 4000
_SARASHINA_LOCK = threading.Lock()
_SARASHINA_NEXT_REQUEST_AT = 0.0
_DEADLINE = ContextVar("sarashina_generation_deadline", default=None)
SARASHINA_PROMPT = (
    "Yomiyasuに基づく日本語校正を依頼します。次のJSONのsourceが校正する原文です。"
    "style_notesは文体の希望です。規則資料の例文ではなくsourceの全文を読み、"
    "明確な誤字・脱字・助詞の重複を検出してeditsに入れてください。小説の作風は保ちます。"
    "回答は指定のJSONだけです。editsの各案にはbefore・after・reason・ruleの4項目を書いてください。\n\n"
)
SARASHINA_PASSAGE_PROMPT = (
    "Yomiyasuに基づいてsourceの日本語を校正してください。style_notesは文体の希望です。"
    "明確な誤字・脱字・助詞や語の重複を見直し、意味と語り口を保ってください。"
    "corrected_textには校正後のsource全文だけを入れます。説明や修正一覧は入れません。"
    "sourceの一部だけを返したり、別の文章や資料の例文を追加したりしないでください。"
    "段落と改行、コードとURLを保ちます。変更不要ならsourceをそのまま返します。"
    "不明な点はquestions、構成の助言はsuggestionsへ。それ以外は空配列。指定JSONだけを返してください。\n\n"
)


class GenerationError(ValueError):
    def __init__(self, code, message, diagnostics=None):
        super().__init__(message)
        self.code = code
        self.diagnostics = diagnostics


@contextmanager
def generation_budget(deadline):
    """One wall-clock budget across requests, lock waits, and cooldowns."""
    token = _DEADLINE.set(deadline)
    try:
        yield
    finally:
        _DEADLINE.reset(token)


def _remaining():
    deadline = _DEADLINE.get()
    if deadline is None:
        return None
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise GenerationError("generation_deadline", "Sarashina校正の時間上限に達しました。途中の修正案は採用せず、原文を残しています。文章を分けて再実行してください。")
    return remaining


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


@contextmanager
def _sarashina_slot():
    """One request at a time, then a ten-second application-wide cooldown.

    This prevents our own split requests from using mini's six-RPM allowance
    in a burst. Other apps can still consume the account allowance; HTTP 429
    remains an explicit failure, with no retry or provider/model fallback.
    """
    global _SARASHINA_NEXT_REQUEST_AT
    waiting = not _SARASHINA_LOCK.acquire(blocking=False)
    if waiting:
        progress.report("generation", message="generation_chunk_wait")
        remaining = _remaining()
        if remaining is None:
            _SARASHINA_LOCK.acquire()
        elif not _SARASHINA_LOCK.acquire(timeout=remaining):
            _remaining()
            raise GenerationError("generation_deadline", "Sarashinaの送信待ちが時間上限に達しました。原文は残っています。")
    try:
        _remaining()
        remaining = _SARASHINA_NEXT_REQUEST_AT - time.monotonic()
        if remaining > 0:
            waiting = True
            progress.report("generation", message="generation_chunk_wait")
            while remaining > 0:
                budget = _remaining()
                time.sleep(remaining if budget is None else min(remaining, budget))
                remaining = _SARASHINA_NEXT_REQUEST_AT - time.monotonic()
            _remaining()
        if waiting:
            progress.report("generation", message="generation_chunk")
        try:
            yield
        finally:
            # Start cooldown after the actual attempt has ended, including an
            # HTTP/network failure. Never count a failed request as a success.
            _SARASHINA_NEXT_REQUEST_AT = time.monotonic() + SARASHINA_INTERVAL_SECONDS
    finally:
        _SARASHINA_LOCK.release()


def _post_json(endpoint, body, key, provider):
    expected = SARASHINA_ENDPOINT if provider == "sarashina" else None
    if endpoint != expected or expected is None:
        raise GenerationError("invalid_endpoint", "公式の生成API以外には送信しません。")
    try:
        encoded = json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (ValueError, TypeError, UnicodeError):
        raise GenerationError("invalid_request", "生成用の入力をJSONとして扱えません。") from None
    if len(encoded) > MAX_REQUEST_BYTES:
        raise GenerationError("input_too_long", "校正方針を含む送信データがアプリの上限を超えています。内容は切り詰めていません。")
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    headers.update({"Authorization": "Bearer " + key})
    request = Request(endpoint, data=encoded, headers=headers, method="POST")
    # No environment proxy, redirect, URL key, automatic retry, or provider
    # switch. API error bodies may echo private text, so they are never surfaced.
    opener = build_opener(ProxyHandler({}), _NoRedirect())
    try:
        with _sarashina_slot():
            remaining = _remaining()
            timeout = TIMEOUT_SECONDS if remaining is None else min(TIMEOUT_SECONDS, remaining)
            with opener.open(request, timeout=timeout) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
            _remaining()
        if len(raw) > MAX_RESPONSE_BYTES:
            raise GenerationError("response_too_large", "生成結果がアプリの受信上限を超えています。原文は変更していません。")
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("not an object")
        return data
    except HTTPError as exc:
        messages = {400: "入力または生成設定を確認してください。", 401: "APIキーの認証を確認してください。",
                    403: "モデルへのアクセス権とAPIキーの制限を確認してください。",
                    404: "指定モデルの提供状況を確認してください。別モデルには切り替えていません。",
                    429: "利用枠またはレート制限に達しました。時間をおいて手動で再試行してください。"}
        message = messages.get(exc.code, "生成サービスが応答を完了できませんでした。時間をおいて再試行してください。")
        raise GenerationError("http_error", f"生成APIでエラーが発生しました（HTTP {exc.code}）。{message}") from None
    except (URLError, OSError, TimeoutError):
        raise GenerationError("connection_error", "生成APIに接続できませんでした。認証情報や原文を含むエラーの詳細は表示していません。") from None
    except GenerationError:
        raise
    except (ValueError, UnicodeError, RecursionError):
        raise GenerationError("invalid_response", "生成APIの応答形式を読み取れませんでした。") from None


def _validate_schema(value, schema):
    kind = schema.get("type")
    if kind == "object":
        if not isinstance(value, dict) or any(key not in value for key in schema.get("required", [])):
            raise ValueError("invalid object")
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False and set(value) - set(properties):
            raise ValueError("unexpected field")
        for key, item in value.items():
            if key in properties:
                _validate_schema(item, properties[key])
    elif kind == "array":
        if not isinstance(value, list) or len(value) > schema.get("maxItems", 1000):
            raise ValueError("invalid array")
        for item in value:
            _validate_schema(item, schema.get("items", {}))
    elif kind == "string":
        if not isinstance(value, str) or not schema.get("minLength", 0) <= len(value) <= schema.get("maxLength", 100000):
            raise ValueError("invalid string")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError("invalid enum")


def _proposal(text, schema):
    try:
        value = json.loads(text)
        if not isinstance(value, dict):
            raise ValueError("not an object")
        _validate_schema(value, schema)
        return value
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise GenerationError("invalid_proposal", "修正案が指定したJSON形式と一致しません。原文は変更していません。") from None


def _identity(provider, requested, reported):
    if not isinstance(reported, str):
        raise GenerationError("identity_missing", "生成に使ったモデル名を応答から確認できません。修正案を保留します。")
    same = reported == requested
    if not same:
        raise GenerationError("identity_mismatch", "要求したモデルと応答のモデル名が一致しません。修正案を保留します。")
    return {"requested_provider": provider, "requested_model": requested,
            "reported_provider": provider, "reported_model": reported,
            "transport": provider_settings.PROVIDERS[provider]["transport"],
            "identity_basis": "api_response_model",
            "exact_checkpoint_verified": False,
            "model_version_differs": not same}


def resolve_selection(payload):
    """Resolve saved choice and explicit overrides without reading credentials."""
    try:
        selected = provider_settings.read_settings()
        provider = payload.get("provider", selected["provider"])
        metadata = provider_settings._provider(provider)
        model = payload.get("model", selected["model"] if provider == selected["provider"] else metadata["default_model"])
        provider_settings._preference({"provider": provider, "model": model})
    except provider_settings.ProviderSettingsError as exc:
        raise GenerationError(exc.code, str(exc)) from None
    return provider, model, metadata


def generate(payload, instructions, schema):
    """Return {proposal, identity}; engine owns semantic/patch validation.

    payload must contain source (or text), plus optional style_notes/profile.
    A provider/model override is explicit and validated; none means saved choice.
    Claude remains delegated to the pre-existing engine adapter.
    """
    if not isinstance(payload, dict) or not isinstance(instructions, str) or not isinstance(schema, dict):
        raise GenerationError("invalid_request", "原文・校正方針・出力形式を指定してください。")
    provider, model, metadata = resolve_selection(payload)
    source = payload.get("source", payload.get("text"))
    if not isinstance(source, str) or not source.strip():
        raise GenerationError("invalid_input", "確認する原文を入力してください。")
    if len(source) > metadata["max_text_chars"]:
        raise GenerationError("input_too_long", f"この生成サービスのアプリ内上限は原文{metadata['max_text_chars']:,}字です。文章を分けてください。内容は切り詰めていません。")
    if provider == "claude":
        raise GenerationError("existing_adapter_required", "Claude は既存の Claude Code アダプターで実行してください。別サービスには切り替えません。")
    if provider == "gemini":
        try:
            return gemini_cli.generate({**payload, "model": model}, instructions, schema)
        except gemini_cli.GeminiCliError as exc:
            raise GenerationError(exc.code, str(exc)) from None
    try:
        key = provider_settings.api_key(provider)
    except provider_settings.ProviderSettingsError as exc:
        raise GenerationError(exc.code, str(exc)) from None
    data_payload = {key_: value for key_, value in payload.items() if key_ not in ("provider", "model")}
    try:
        prompt = json.dumps(data_payload, ensure_ascii=False, allow_nan=False)
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise GenerationError("invalid_request", "生成用の入力をJSONとして扱えません。") from None
    started = time.monotonic()
    passage = "corrected_text" in schema.get("properties", {})
    adapter = SARASHINA_PASSAGE_PROMPT if passage else SARASHINA_PROMPT
    if passage:
        instructions += ("\nこの呼出しの出力契約: before/afterの修正一覧は返しません。"
                         "指定JSONのcorrected_textに校正後のsource全文を返してください。"
                         "原文の意味・段落・改行・作風を保ち、誤字や重複も点検してください。")
    response = _post_json(SARASHINA_ENDPOINT, {
        "model": model, "messages": [{"role": "system", "content": instructions}, {"role": "user", "content": adapter + prompt}],
        "max_completion_tokens": SARASHINA_OUTPUT_TOKENS, "temperature": 0.7, "n": 1, "stream": False,
        "response_format": {"type": "json_schema", "json_schema": {"name": "japanese_editor", "schema": schema, "strict": True}},
    }, key, provider)
    identity = _identity(provider, model, response.get("model"))
    choices = response.get("choices")
    if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
        raise GenerationError("generation_incomplete", "生成が完了していません。文章を分けて再実行してください。途中の文章は採用していません。")
    finish = choices[0].get("finish_reason")
    # Allowlisted counts and hashes only. Never retain manuscript, API errors,
    # authorization, or arbitrary upstream metadata as application diagnostics.
    usage = response.get("usage")
    diagnostics = {"elapsed_seconds": round(time.monotonic() - started, 3),
                   "temperature": 0.7,
                   "max_completion_tokens": SARASHINA_OUTPUT_TOKENS,
                   "finish_reason": finish if finish in ("stop", "length", "tool_calls", "content_filter") else "unknown",
                   "prompt_adapter_sha256": hashlib.sha256(adapter.encode()).hexdigest(),
                   "usage": {name: usage[name] for name in ("prompt_tokens", "completion_tokens", "total_tokens")
                             if isinstance(usage, dict) and type(usage.get(name)) is int and 0 <= usage[name] <= 10000000}}
    if finish == "length":
        raise GenerationError("generation_length", "Sarashinaの出力上限に達しました。途中の文章は採用していません。", diagnostics)
    if finish != "stop":
        raise GenerationError("generation_incomplete", "生成が完了していません。途中の文章は採用していません。", diagnostics)
    message = choices[0].get("message")
    if not isinstance(message, dict):
        raise GenerationError("invalid_response", "生成APIから本文を読み取れませんでした。")
    text = message.get("content")
    proposal = _proposal(text, schema)
    try:
        content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    except UnicodeError:
        raise GenerationError("invalid_proposal", "修正案に不正なUnicode文字が含まれています。原文は変更していません。") from None
    edits = proposal.get("edits", [])
    no_ops = sum(isinstance(edit, dict) and edit.get("before") == edit.get("after") for edit in edits)
    diagnostics.update(raw_edit_count=len(edits), unchanged_edit_count=no_ops,
                       output_mode="corrected_passage" if passage else "patches",
                       content_characters=len(text), content_sha256=content_hash)
    return {"proposal": proposal, "identity": identity, "diagnostics": diagnostics}


generate_generation = generate
