"""Authenticate a private, exclusively bound loopback RPC connection.

This is not transport encryption. The server proof binds its actual listening
port, and the client never reconnects between proof and manuscript submission.
Together with an exclusive server bind this prevents an untrusted listener from
relaying a proof obtained from a legitimate server on another port. A malicious
process running as the same OS user can use that user's private key and is not
protected against. Administrators and network interception outside the local OS
socket model are also outside this boundary.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass, field
import hashlib
import hmac
import http.client
import json
import os
from pathlib import Path
import secrets
import stat
import time
from urllib.parse import urlsplit

from runtime_paths import PERSISTENT_ROOT

PROTOCOL = "yomiyasu-rpc-hmac-v1"
CHALLENGE_PATH = "/api/decider/challenge"
SECRET_PATH = PERSISTENT_ROOT / "rpc" / "secret-v1"
MAX_CHALLENGE_BYTES = 4096
MAX_RESPONSE_BYTES = 2_000_000
SESSION_SECONDS = 30
REQUEST_HEADER = "X-Yomiyasu-Request-MAC"
RESPONSE_HEADER = "X-Yomiyasu-Response-MAC"
_ENTROPY = b"Yomiyasu Studio private local RPC secret v1"


class AuthenticationError(RuntimeError):
    """Safe public error: never include body text, local paths or key material."""


def _dpapi(value, *, protect):
    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]

    def blob(data):
        buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
        return Blob(len(data), buffer), buffer

    source, source_buffer = blob(value)
    entropy, entropy_buffer = blob(_ENTROPY)
    output = Blob()
    api = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    function = api.CryptProtectData if protect else api.CryptUnprotectData
    function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.POINTER(Blob),
                         ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    function.restype = wintypes.BOOL
    # CRYPTPROTECT_UI_FORBIDDEN, with current-user (not LOCAL_MACHINE) scope.
    if not function(ctypes.byref(source), None, ctypes.byref(entropy), None, None, 1, ctypes.byref(output)):
        raise AuthenticationError("共有サービスの認証情報を保護または読み取りできません。")
    try:
        return ctypes.string_at(output.pbData, output.cbData)
    finally:
        kernel.LocalFree(output.pbData)


def _read_secret(path):
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    try:
        with os.fdopen(os.open(path, flags), "rb") as stream:
            details = os.fstat(stream.fileno())
            if not stat.S_ISREG(details.st_mode):
                raise AuthenticationError("共有サービスの認証情報が通常のファイルではありません。")
            if os.name != "nt" and (details.st_uid != os.getuid() or stat.S_IMODE(details.st_mode) & 0o077):
                raise AuthenticationError("共有サービスの認証情報を本人だけが読める権限にしてください。")
            stored = stream.read(4097)
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise AuthenticationError("共有サービスの認証情報を読み取れません。") from exc
    if len(stored) > 4096:
        raise AuthenticationError("共有サービスの認証情報の形式が不正です。")
    secret = _dpapi(stored, protect=False) if os.name == "nt" else stored
    if len(secret) != 32:
        raise AuthenticationError("共有サービスの認証情報の形式が不正です。")
    return secret


def load_secret(path=None):
    """Load/create one per-user key, outside settings and the distribution.

    A private temporary file is published with a no-overwrite hard link, so
    concurrent creators cannot replace a key or observe a partially written key.
    Windows contents are additionally protected by current-user DPAPI.
    """
    path = Path(SECRET_PATH if path is None else path)
    try:
        return _read_secret(path)
    except FileNotFoundError:
        pass
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        secret = secrets.token_bytes(32)
        stored = _dpapi(secret, protect=True) if os.name == "nt" else secret
        temporary = path.with_name("." + path.name + "." + secrets.token_hex(16))
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(stored)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, path)
            except FileExistsError:
                pass
        finally:
            temporary.unlink(missing_ok=True)
        return _read_secret(path)
    except (OSError, FileNotFoundError) as exc:
        raise AuthenticationError("共有サービスの認証情報を安全に作成できません。") from exc


def _nonce(value):
    return isinstance(value, str) and len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def _mac(secret, label, *parts):
    # JSON array framing avoids concatenation ambiguity. Labels separate keys'
    # uses without exporting the per-user secret to an unauthenticated peer.
    frame = json.dumps([PROTOCOL, label, *parts], ensure_ascii=True, separators=(",", ":")).encode("ascii")
    return hmac.new(secret, frame, hashlib.sha256).hexdigest()


@dataclass
class Session:
    secret: bytes = field(repr=False)
    client_nonce: str
    server_nonce: str
    port: int
    created: float = field(default_factory=time.monotonic)
    used: bool = False


@dataclass
class RequestContext:
    session: Session = field(repr=False)
    request_mac: str


def issue_challenge(client_nonce, bound_port, *, secret=None):
    """bound_port MUST be from accepted_socket.getsockname(), never Host/input."""
    if not _nonce(client_nonce) or isinstance(bound_port, bool) or not isinstance(bound_port, int) or not 0 < bound_port <= 65535:
        raise AuthenticationError("共有サービスの認証要求が不正です。")
    session = Session(load_secret() if secret is None else secret, client_nonce, secrets.token_hex(32), bound_port)
    challenge = {"protocol": PROTOCOL, "client_nonce": client_nonce, "server_nonce": session.server_nonce,
                 "port": bound_port, "proof": _mac(session.secret, "server", client_nonce, session.server_nonce, bound_port)}
    return challenge, session


def verify_challenge(challenge, client_nonce, expected_port, *, secret=None):
    if not isinstance(challenge, dict) or challenge.get("protocol") != PROTOCOL or challenge.get("client_nonce") != client_nonce \
            or not _nonce(challenge.get("server_nonce")) or type(challenge.get("port")) is not int or challenge.get("port") != expected_port:
        raise AuthenticationError("共有サービスを認証できません。アプリと共有サービスを同じバージョンに更新してください。")
    session = Session(load_secret() if secret is None else secret, client_nonce, challenge["server_nonce"], expected_port)
    proof = challenge.get("proof")
    expected = _mac(session.secret, "server", client_nonce, session.server_nonce, expected_port)
    if not _nonce(proof) or not hmac.compare_digest(proof, expected):
        raise AuthenticationError("共有サービスを認証できません。アプリと共有サービスを同じバージョンに更新してください。")
    return session


def request_headers(session, method, path, body):
    digest = hashlib.sha256(body).hexdigest()
    value = _mac(session.secret, "request", session.client_nonce, session.server_nonce, session.port, method, path, digest)
    return {REQUEST_HEADER: value}


def verify_request(session, method, path, body, headers):
    if not isinstance(session, Session) or session.used or time.monotonic() - session.created > SESSION_SECONDS:
        raise AuthenticationError("共有サービスの認証が必要です。")
    session.used = True
    value = headers.get(REQUEST_HEADER)
    expected = request_headers(session, method, path, body)[REQUEST_HEADER]
    if not _nonce(value) or not hmac.compare_digest(value, expected):
        raise AuthenticationError("共有サービスへの要求を認証できません。")
    return RequestContext(session, value)


def response_headers(context, status, body):
    session = context.session
    value = _mac(session.secret, "response", session.client_nonce, session.server_nonce, session.port,
                 context.request_mac, status, hashlib.sha256(body).hexdigest())
    return {RESPONSE_HEADER: value}


def verify_response(context, status, body, headers):
    value = headers.get(RESPONSE_HEADER)
    expected = response_headers(context, status, body)[RESPONSE_HEADER]
    if not _nonce(value) or not hmac.compare_digest(value, expected):
        raise AuthenticationError("共有サービスの応答を認証できません。")


def authenticated_request(url, path, body=None, *, timeout=3, secret=None):
    """Return (status, raw JSON body), only after authenticated same-socket RPC.

    http.client bypasses environment proxy settings and never follows redirects.
    The server must exclusively own its loopback port and keep the challenge
    connection alive. A closed connection causes an error, never a reconnect.
    """
    try:
        parsed = urlsplit(url) if isinstance(url, str) else None
        port = (80 if parsed.port is None else parsed.port) if parsed is not None else 0
        valid = parsed is not None and parsed.scheme == "http" and parsed.hostname in ("127.0.0.1", "localhost") \
            and parsed.username is None and parsed.password is None and parsed.path in ("", "/") \
            and not parsed.query and not parsed.fragment and not any(ord(character) <= 32 or ord(character) == 127 for character in url)
    except ValueError:
        valid = False
    if not valid:
        raise AuthenticationError("共有サービスには認証情報を含まないループバックURLを指定してください。")
    if not 0 < port <= 65535 or not isinstance(path, str) or not path.startswith("/api/") or "?" in path or "#" in path \
            or any(ord(character) <= 32 or ord(character) == 127 for character in path):
        raise AuthenticationError("共有サービスへの接続先が不正です。")
    secret = load_secret() if secret is None else secret
    nonce = secrets.token_hex(32)
    # Numeric 127.0.0.1 avoids DNS/hosts-file alias changes for 'localhost'.
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        connection.request("GET", CHALLENGE_PATH + "?nonce=" + nonce, headers={"Connection": "keep-alive"})
        response = connection.getresponse()
        challenge_body = response.read(MAX_CHALLENGE_BYTES + 1)
        if response.status != 200 or len(challenge_body) > MAX_CHALLENGE_BYTES:
            raise AuthenticationError("共有サービスを認証できません。アプリと共有サービスを同じバージョンに更新してください。")
        try:
            challenge = json.loads(challenge_body)
        except (ValueError, UnicodeError) as exc:
            raise AuthenticationError("共有サービスの認証応答が不正です。") from exc
        session = verify_challenge(challenge, nonce, port, secret=secret)
        if connection.sock is None or response.will_close:
            raise AuthenticationError("共有サービスの認証済み接続が閉じられました。アプリを更新して再起動してください。")
        if connection.sock.getpeername()[:2] != ("127.0.0.1", port):
            raise AuthenticationError("共有サービスの認証済み接続先が一致しません。")
        # Without auto_open=0 HTTPConnection silently reconnects after close.
        connection.auto_open = 0
        method = "GET" if body is None else "POST"
        raw = b"" if body is None else body
        headers = request_headers(session, method, path, raw)
        context = RequestContext(session, headers[REQUEST_HEADER])
        headers.update({"Content-Type": "application/json", "Connection": "close"})
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        raw = response.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise AuthenticationError("共有サービスの応答が大きすぎます。")
        verify_response(context, response.status, raw, response.headers)
        return response.status, raw
    finally:
        connection.close()
