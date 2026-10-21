"""Public preferences with Windows Credential Manager or macOS Keychain.

The JSON preference file contains only a provider and an allowlisted model.
Keys are never returned by public_config(), written to JSON, or logged.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import threading

from runtime_paths import PERSISTENT_ROOT
import mac_credentials

SETTINGS_PATH = PERSISTENT_ROOT / "providers.json"
DEFAULT_PROVIDER = "sarashina"
CREDENTIAL_PREFIX = "StudioYebisu/JapaneseEditor/generator/"
_LOCK = threading.RLock()

PROVIDERS = {
    "sarashina": {
        "label": "Sarashina", "default_model": "sarashina3-mini",
        "models": ("sarashina3-mini", "sarashina3-nano"), "transport": "sarashina-api", "max_text_chars": 1200,
        "max_document_chars": 12000, "chunking": "paragraphs",
        "auth_method": "api_key", "env": ("SARASHINA_API_KEY",), "setup_url": "https://chat.sarashina.ai/dashboard/api-keys",
        "notice": "入力全体12,000字まで。段落・文末で1回1,200字以内に分け、文章と校正方針をSarashina APIに順番に送ります。分割の判断にAIは使いません。修正候補はローカルDeciderで意味の変化を確認します。利用規約第11条は入出力の学習等への利用を定めています。",
    },
    "gemini": {
        "label": "Gemini 3.8 Flash（Antigravity CLI）", "default_model": "gemini-3.8-flash-high",
        "models": ("gemini-3.8-flash-high",), "transport": "antigravity-cli", "max_text_chars": 12000,
        "auth_method": "google_login", "env": (),
        "setup_url": "https://antigravity.google/docs/install?tab=cli",
        "notice": "Google公式のAntigravity CLIでGemini 3.8 Flash（High）へ文章と校正方針を送信します。AI Pro / Ultra は契約したGoogleアカウントでCLIへログインしてください。利用枠とモデルの提供状況はアカウントにより異なります。",
    },
    "claude": {
        "label": "Claude（既存 CLI）", "default_model": "claude-opus-5-5",
        "models": ("claude-opus-5-5",), "transport": "claude-cli", "max_text_chars": 12000,
        "auth_method": "existing_cli", "env": (), "setup_url": "https://code.claude.com/docs/en/setup",
        "notice": "既存のClaude Codeの認証と利用枠で文章を送信し、修正案を作ります。",
    },
}


class ProviderSettingsError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _provider(provider):
    if not isinstance(provider, str) or provider not in PROVIDERS:
        raise ProviderSettingsError("invalid_provider", "対応する生成サービスを選んでください。")
    return PROVIDERS[provider]


def _preference(payload):
    if not isinstance(payload, dict) or set(payload) - {"version", "provider", "model"}:
        raise ProviderSettingsError("invalid_settings", "生成設定にはサービス名とモデル名だけを指定してください。APIキーは専用の保存操作を使います。")
    provider = payload.get("provider", DEFAULT_PROVIDER)
    metadata = _provider(provider)
    model = payload.get("model", metadata["default_model"])
    if model not in metadata["models"]:
        raise ProviderSettingsError("invalid_model", "選んだサービスの公式対応モデルを指定してください。別モデルには切り替えません。")
    return {"version": 1, "provider": provider, "model": model}


def read_settings():
    with _LOCK:
        try:
            raw = SETTINGS_PATH.read_text("utf-8")
            if len(raw) > 16000:
                raise ValueError("oversized settings")
            data = json.loads(raw)
            # The official successor CLI names the explicit Flash reasoning
            # variant. Migrate only our previous saved default, not requests.
            if isinstance(data, dict) and data.get("provider") == "gemini" and data.get("model") == "gemini-3.8-flash":
                data = {**data, "model": "gemini-3.8-flash-high"}
            return _preference(data)
        except FileNotFoundError:
            return _preference({})
        except (OSError, ValueError, TypeError) as exc:
            if isinstance(exc, ProviderSettingsError):
                raise
            raise ProviderSettingsError("invalid_settings", "生成設定を読み取れません。サービスとモデルを選び直して保存してください。") from None


def save_settings(payload):
    selected = _preference(payload)
    with _LOCK:
        temporary = None
        try:
            SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=SETTINGS_PATH.parent,
                                             prefix="providers-", suffix=".tmp", delete=False) as stream:
                temporary = Path(stream.name)
                json.dump(selected, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
            temporary.replace(SETTINGS_PATH)
        except OSError:
            raise ProviderSettingsError("save_failed", "生成設定を保存できませんでした。保存先の利用状態を確認してください。") from None
        finally:
            if temporary is not None and temporary.exists():
                try:
                    temporary.unlink()
                except OSError:
                    pass
    return public_config()


def _windows_api():
    if os.name != "nt":
        raise ProviderSettingsError("credential_store_unavailable", "APIキーの保存にはWindows資格情報マネージャーを使います。この環境では環境変数を指定してください。")

    class Credential(ctypes.Structure):
        _fields_ = [("Flags", wintypes.DWORD), ("Type", wintypes.DWORD),
                    ("TargetName", wintypes.LPWSTR), ("Comment", wintypes.LPWSTR),
                    ("LastWritten", wintypes.FILETIME), ("CredentialBlobSize", wintypes.DWORD),
                    ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)), ("Persist", wintypes.DWORD),
                    ("AttributeCount", wintypes.DWORD), ("Attributes", ctypes.c_void_p),
                    ("TargetAlias", wintypes.LPWSTR), ("UserName", wintypes.LPWSTR)]

    pointer = ctypes.POINTER(Credential)
    try:
        api = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
    except OSError:
        raise ProviderSettingsError("credential_store_unavailable", "Windows 資格情報マネージャーを利用できません。") from None
    api.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(pointer)]
    api.CredReadW.restype = wintypes.BOOL
    api.CredWriteW.argtypes = [pointer, wintypes.DWORD]
    api.CredWriteW.restype = wintypes.BOOL
    api.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
    api.CredDeleteW.restype = wintypes.BOOL
    api.CredFree.argtypes = [ctypes.c_void_p]
    api.CredFree.restype = None
    return api, Credential


def _credential_read(provider, *, presence_only=False):
    if sys.platform == "darwin":
        try:
            return mac_credentials.read(provider, presence_only=presence_only)
        except mac_credentials.KeychainError as exc:
            if exc.code == "credential_framework_unavailable":
                return False if presence_only else None
            raise ProviderSettingsError(exc.code, str(exc)) from None
    if os.name != "nt":
        return False if presence_only else None
    api, credential_type = _windows_api()
    pointer = ctypes.POINTER(credential_type)()
    if not api.CredReadW(CREDENTIAL_PREFIX + provider, 1, 0, ctypes.byref(pointer)):
        if ctypes.get_last_error() == 1168:
            return False if presence_only else None
        raise ProviderSettingsError("credential_store_unavailable", "保存したAPIキーを資格情報マネージャーから読み取れません。")
    try:
        credential = pointer.contents
        if presence_only:
            return bool(credential.CredentialBlobSize)
        return ctypes.string_at(credential.CredentialBlob, credential.CredentialBlobSize).decode("utf-8")
    except (ValueError, UnicodeError):
        raise ProviderSettingsError("credential_invalid", "保存したAPIキーを読み取れません。キーを保存し直してください。") from None
    finally:
        api.CredFree(pointer)


def _validated_key(key):
    if not isinstance(key, str):
        raise ProviderSettingsError("invalid_key", "APIキーを入力してください。")
    key = key.strip()
    if not key or len(key) > 2048 or any(not 33 <= ord(character) <= 126 for character in key):
        raise ProviderSettingsError("invalid_key", "APIキーの空欄・長さ・空白や改行を確認してください。")
    return key


def credential_status(provider):
    metadata = _provider(provider)
    if provider == "gemini":
        import gemini_cli
        return gemini_cli.status()
    if provider == "claude":
        return {"configured": bool(shutil.which("claude")), "credential_source": "existing_cli"}
    try:
        if _credential_read(provider, presence_only=True):
            return {"configured": True, "credential_source": "macos_keychain" if sys.platform == "darwin" else "windows_credential_manager"}
    except ProviderSettingsError as exc:
        return {"configured": False, "credential_source": "unavailable", "credential_error": str(exc)}
    for name in metadata["env"]:
        if os.environ.get(name):
            return {"configured": True, "credential_source": "environment"}
    return {"configured": False, "credential_source": "none"}


def api_key(provider):
    metadata = _provider(provider)
    if provider != "sarashina":
        raise ProviderSettingsError("invalid_provider", "APIキーはSarashina用です。Geminiは公式CLIのGoogleログイン、Claudeは既存CLIの認証を使います。")
    saved = _credential_read(provider)
    if saved:
        return _validated_key(saved)
    for name in metadata["env"]:
        if os.environ.get(name):
            return _validated_key(os.environ[name])
    raise ProviderSettingsError("not_configured", "選んだサービスのAPIキーが未設定です。設定画面で保存するか、対応する環境変数を設定してください。")


def save_api_key(provider, key):
    _provider(provider)
    if provider != "sarashina":
        raise ProviderSettingsError("invalid_provider", "APIキーの保存はSarashina用です。Geminiは公式CLIへGoogleアカウントでログインしてください。")
    key = _validated_key(key)
    if sys.platform == "darwin":
        try:
            mac_credentials.save(provider, key)
        except mac_credentials.KeychainError as exc:
            raise ProviderSettingsError(exc.code, str(exc)) from None
        return public_config()
    api, credential_type = _windows_api()
    blob = key.encode("utf-8")
    buffer = (ctypes.c_ubyte * len(blob)).from_buffer_copy(blob)
    credential = credential_type()
    credential.Type, credential.Persist = 1, 2
    credential.TargetName, credential.UserName = CREDENTIAL_PREFIX + provider, "api-key"
    credential.CredentialBlobSize, credential.CredentialBlob = len(blob), buffer
    try:
        if not api.CredWriteW(ctypes.byref(credential), 0):
            raise ProviderSettingsError("credential_save_failed", "APIキーを資格情報マネージャーへ保存できませんでした。")
    finally:
        ctypes.memset(buffer, 0, len(blob))
    return public_config()


def remove_api_key(provider):
    _provider(provider)
    if provider != "sarashina":
        raise ProviderSettingsError("invalid_provider", "CLIの認証はこのアプリから削除しません。")
    if sys.platform == "darwin":
        try:
            mac_credentials.remove(provider)
        except mac_credentials.KeychainError as exc:
            raise ProviderSettingsError(exc.code, str(exc)) from None
        return public_config()
    api, _ = _windows_api()
    if not api.CredDeleteW(CREDENTIAL_PREFIX + provider, 1, 0) and ctypes.get_last_error() != 1168:
        raise ProviderSettingsError("credential_delete_failed", "保存したAPIキーを削除できませんでした。")
    # An independently set environment variable may remain active. The returned
    # public status makes that source visible; we never delete user env vars.
    return public_config()


def public_config():
    error = None
    try:
        selected = read_settings()
    except ProviderSettingsError as exc:
        selected = {"version": 1, "provider": None, "model": None}
        error = str(exc)
    items = []
    for provider, metadata in PROVIDERS.items():
        status = credential_status(provider)
        model = selected["model"] if selected["provider"] == provider else metadata["default_model"]
        items.append({"id": provider, "label": metadata["label"], "model": model,
                      "models": list(metadata["models"]), "transport": metadata["transport"],
                      "max_text_chars": metadata["max_text_chars"], "max_document_chars": metadata.get("max_document_chars", metadata["max_text_chars"]),
                      "chunking": metadata.get("chunking"), "setup_url": metadata["setup_url"], "auth_method": metadata["auth_method"],
                      "notice": metadata["notice"], "available": status["configured"], **status})
    native = os.name == "nt" or (sys.platform == "darwin" and mac_credentials.available())
    store = {"available": native,
             "type": "macos_keychain" if sys.platform == "darwin" and native else "windows_credential_manager" if os.name == "nt" else "environment_only",
             "notice": "APIキーはOSの資格情報ストアへ保存します。設定JSONには含めません。" if native else "この環境ではAPIキーの保存を利用できません。対応する環境変数を設定してください。"}
    return {**selected, "providers": items, "secret_store": store, **({"configuration_error": error} if error else {})}
