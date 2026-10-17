"""Application-owned macOS generic passwords via system frameworks only.

No subprocess, command-line secret, third-party library, or plaintext fallback.
The legacy SecKeychain APIs are deprecated but remain declared by Apple for
macOS; real macOS validation is required before claiming runtime verification.
"""
from __future__ import annotations

import ctypes
import sys

SERVICE = b"StudioYebisu/JapaneseEditor/generator"
_ACCOUNTS = ("gemini", "sarashina")
_NOT_FOUND = -25300


class KeychainError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _frameworks():
    if sys.platform != "darwin":
        raise KeychainError("credential_framework_unavailable", "macOS キーチェーンを利用できません。環境変数でAPIキーを指定できます。")
    try:
        security = ctypes.CDLL("/System/Library/Frameworks/Security.framework/Security")
        core = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        pointer, uint = ctypes.c_void_p, ctypes.c_uint32
        security.SecKeychainFindGenericPassword.argtypes = [pointer, uint, ctypes.c_char_p, uint, ctypes.c_char_p,
                                                           ctypes.POINTER(uint), ctypes.POINTER(pointer), ctypes.POINTER(pointer)]
        security.SecKeychainAddGenericPassword.argtypes = [pointer, uint, ctypes.c_char_p, uint, ctypes.c_char_p,
                                                          uint, pointer, ctypes.POINTER(pointer)]
        security.SecKeychainItemModifyAttributesAndData.argtypes = [pointer, pointer, uint, pointer]
        security.SecKeychainItemDelete.argtypes = [pointer]
        security.SecKeychainItemFreeContent.argtypes = [pointer, pointer]
        for name in ("SecKeychainFindGenericPassword", "SecKeychainAddGenericPassword",
                     "SecKeychainItemModifyAttributesAndData", "SecKeychainItemDelete", "SecKeychainItemFreeContent"):
            getattr(security, name).restype = ctypes.c_int32
        core.CFRelease.argtypes, core.CFRelease.restype = [pointer], None
        return security, core
    except (OSError, AttributeError):
        raise KeychainError("credential_framework_unavailable", "macOS キーチェーンのシステムAPIを利用できません。環境変数でAPIキーを指定できます。") from None


def available():
    try:
        _frameworks()
        return True
    except KeychainError:
        return False


def _account(provider):
    if not isinstance(provider, str) or provider not in _ACCOUNTS:
        raise KeychainError("invalid_provider", "対応する生成サービスを指定してください。")
    return provider.encode("ascii")


def _find(security, account, *, password=False):
    length, data, item = ctypes.c_uint32(), ctypes.c_void_p(), ctypes.c_void_p()
    status = security.SecKeychainFindGenericPassword(None, len(SERVICE), SERVICE, len(account), account,
               ctypes.byref(length) if password else None, ctypes.byref(data) if password else None, ctypes.byref(item))
    return status, length, data, item


def read(provider, *, presence_only=False):
    account = _account(provider)
    security, core = _frameworks()
    status, length, data, item = _find(security, account, password=not presence_only)
    try:
        if status == _NOT_FOUND:
            return False if presence_only else None
        if status != 0:
            raise KeychainError("credential_store_unavailable", "保存したAPIキーを macOS キーチェーンから読み取れません。アクセス許可とキーチェーンの状態を確認してください。")
        if not item.value:
            raise KeychainError("credential_invalid", "保存したAPIキーの項目を確認できません。キーを保存し直してください。")
        if presence_only:
            return True
        if not data.value or not 1 <= length.value <= 2048:
            raise KeychainError("credential_invalid", "保存したAPIキーの長さを確認できません。キーを保存し直してください。")
        try:
            return ctypes.string_at(data, length.value).decode("ascii")
        except UnicodeError:
            raise KeychainError("credential_invalid", "保存したAPIキーを読み取れません。キーを保存し直してください。") from None
    finally:
        if data.value:
            security.SecKeychainItemFreeContent(None, data)
        if item.value:
            core.CFRelease(item)


def save(provider, key):
    account = _account(provider)
    if not isinstance(key, str) or not 1 <= len(key) <= 2048 or any(not 33 <= ord(char) <= 126 for char in key):
        raise KeychainError("invalid_key", "APIキーの長さと文字を確認してください。")
    security, core = _frameworks()
    status, _, _, item = _find(security, account)
    raw = key.encode("ascii")
    buffer = ctypes.create_string_buffer(raw, len(raw))
    created = ctypes.c_void_p()
    try:
        if status == 0 and item.value:
            result = security.SecKeychainItemModifyAttributesAndData(item, None, len(raw), buffer)
        elif status == _NOT_FOUND:
            result = security.SecKeychainAddGenericPassword(None, len(SERVICE), SERVICE, len(account), account,
                                                            len(raw), buffer, ctypes.byref(created))
        else:
            raise KeychainError("credential_store_unavailable", "macOS キーチェーンの保存先を確認できません。アクセス許可とキーチェーンの状態を確認してください。")
        if result != 0:
            raise KeychainError("credential_save_failed", "APIキーを macOS キーチェーンへ保存できませんでした。アクセス許可とキーチェーンの状態を確認してください。")
    finally:
        ctypes.memset(buffer, 0, len(raw))
        if item.value:
            core.CFRelease(item)
        if created.value:
            core.CFRelease(created)


def remove(provider):
    account = _account(provider)
    security, core = _frameworks()
    status, _, _, item = _find(security, account)
    try:
        if status == _NOT_FOUND:
            return
        if status != 0 or not item.value:
            raise KeychainError("credential_store_unavailable", "macOS キーチェーンの削除対象を確認できません。アクセス許可とキーチェーンの状態を確認してください。")
        if security.SecKeychainItemDelete(item) != 0:
            raise KeychainError("credential_delete_failed", "保存したAPIキーを macOS キーチェーンから削除できませんでした。")
    finally:
        if item.value:
            core.CFRelease(item)
