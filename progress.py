"""Bounded, ephemeral request progress; never retain manuscript or exception text."""
from __future__ import annotations

from collections import OrderedDict
from contextlib import contextmanager
from contextvars import ContextVar
import re
import threading
import time


MAX_ENTRIES = 128
TTL_SECONDS = 20 * 60
STAGES = frozenset({"rules", "generation", "decider", "checking", "completed"})
MESSAGES = {
    "rules": "日本語の規則を点検しています。",
    "generation": "AIが修正候補を生成しています。",
    "generation_retry_wait": "Gemini校正の自動再試行を準備しています。",
    "generation_retry": "Gemini校正を自動で再試行しています。",
    "decider": "Deciderを準備し、修正候補を判定しています。",
    "decider_running": "Deciderで修正候補を判定しています。",
    "decider_finished": "修正候補の判定を終了しました。",
    "decider_unfinished": "判定を終了しました（未判定またはエラーの候補があります）。",
    "decider_empty": "修正候補がないため、Decider判定は実行していません。",
    "checking": "修正後の全文を点検しています。",
    "checking_verify": "本文と点検記録を照合しています。",
    "completed": "処理が完了しました。",
    "failed": "処理を完了できませんでした。原文は残っています。",
}
_ID = re.compile(r"[A-Za-z0-9_-]{8,100}\Z", re.ASCII)
_CURRENT = ContextVar("yebisu_progress_request", default=None)


class ProgressError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def validate_id(request_id):
    if request_id is None:
        return None
    if not isinstance(request_id, str) or not _ID.fullmatch(request_id):
        raise ProgressError("進捗IDは8〜100文字の英数字・ハイフン・アンダースコアで指定してください。")
    return request_id


class Registry:
    def __init__(self, maximum=MAX_ENTRIES, ttl=TTL_SECONDS, clock=time.time, monotonic=time.monotonic):
        if not 1 <= maximum <= MAX_ENTRIES or not 0 < ttl <= TTL_SECONDS:
            raise ValueError("Invalid progress registry bounds")
        self.maximum, self.ttl = maximum, ttl
        self.clock, self.monotonic = clock, monotonic
        self._entries = OrderedDict()
        self._lock = threading.RLock()

    def _purge(self):
        now = self.monotonic()
        for key in [key for key, row in self._entries.items() if row["expires"] <= now]:
            del self._entries[key]

    def _touch(self, request_id, row):
        row["value"]["updated_at"] = self.clock()
        row["expires"] = self.monotonic() + self.ttl
        self._entries.move_to_end(request_id)

    def begin(self, request_id, stage="rules"):
        validate_id(request_id)
        if request_id is None or stage not in STAGES:
            raise ValueError("A valid request and stage are required")
        with self._lock:
            self._purge()
            previous = self._entries.get(request_id)
            if previous and previous["value"]["status"] == "running":
                raise ProgressError("同じ進捗IDの処理を実行中です。", 409)
            self._entries[request_id] = {"value": {"status": "running", "stage": stage,
                                        "message": MESSAGES[stage]}, "expires": 0}
            self._touch(request_id, self._entries[request_id])
            while len(self._entries) > self.maximum:
                self._entries.popitem(last=False)

    def update(self, request_id, stage, *, completed=None, total=None, message=None, attempt=None, max_attempts=None):
        if stage not in STAGES or (message is not None and message not in MESSAGES):
            raise ValueError("Unknown progress stage or fixed message")
        for value in (completed, total):
            if value is not None and (isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 1000000):
                raise ValueError("Invalid progress count")
        if completed is not None and total is not None and completed > total:
            raise ValueError("Completed count exceeds total")
        if attempt is not None or max_attempts is not None:
            if (stage != "generation" or type(attempt) is not int or type(max_attempts) is not int
                    or not 1 <= attempt <= max_attempts <= 3):
                raise ValueError("Invalid generation attempt")
        with self._lock:
            self._purge()
            row = self._entries.get(request_id)
            if not row or row["value"]["status"] != "running":
                return
            value = row["value"]
            if value["stage"] != stage:
                value.pop("completed", None)
                value.pop("total", None)
                value.pop("attempt", None)
                value.pop("max_attempts", None)
            value.update(stage=stage, message=MESSAGES[message or stage])
            if completed is not None:
                value["completed"] = completed
            if total is not None:
                value["total"] = total
            if attempt is not None:
                value.update(attempt=attempt, max_attempts=max_attempts)
            self._touch(request_id, row)

    def finish(self, request_id, success=True):
        with self._lock:
            self._purge()
            row = self._entries.get(request_id)
            if not row or row["value"]["status"] != "running":
                return
            value = row["value"]
            if success:
                value.update(status="completed", stage="completed")
                # A successful HTTP operation can still contain unrun judgments.
                if value["message"] not in {MESSAGES["decider_unfinished"], MESSAGES["decider_empty"]}:
                    value["message"] = MESSAGES["completed"]
            else:
                value.update(status="failed", message=MESSAGES["failed"])
            self._touch(request_id, row)

    def get(self, request_id):
        validate_id(request_id)
        with self._lock:
            self._purge()
            row = self._entries.get(request_id)
            if row is None:
                return None
            self._entries.move_to_end(request_id)
            return dict(row["value"])


REGISTRY = Registry()


@contextmanager
def operation(request_id, stage="rules"):
    """Only HTTP requests opt in; ordinary engine/MCP calls remain no-ops."""
    validate_id(request_id)
    if request_id is not None:
        REGISTRY.begin(request_id, stage)
    token = _CURRENT.set(request_id)
    try:
        yield
    except BaseException:
        if request_id is not None:
            REGISTRY.finish(request_id, False)
        raise
    else:
        if request_id is not None:
            REGISTRY.finish(request_id, True)
    finally:
        _CURRENT.reset(token)


def report(stage, *, completed=None, total=None, message=None, attempt=None, max_attempts=None):
    request_id = _CURRENT.get()
    if request_id is not None:
        REGISTRY.update(request_id, stage, completed=completed, total=total, message=message,
                        attempt=attempt, max_attempts=max_attempts)


def lookup(request_id):
    return REGISTRY.get(request_id)
