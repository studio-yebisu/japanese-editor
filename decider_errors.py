"""Fixed public Decider diagnostics; never publish arbitrary worker strings."""
from __future__ import annotations

import math

MESSAGES = {
    "runtime_error": "Deciderの処理を完了できませんでした。",
    "not_configured": "ローカルDeciderの設定がありません。",
    "disabled": "ローカルDeciderは無効です。",
    "invalid_config": "Deciderの設定を確認してください。",
    "busy": "PPDDが処理中です。完了してから設定を変更してください。",
    "settings_changed": "PPDDの設定が別の操作で更新されました。接続状態を更新して再実行してください。",
    "settings_save_failed": "PPDDの設定を保存できませんでした。元の設定は残っています。",
    "unsupported_model": "固定したDecider Flash v1.2のモデルと版を指定してください。",
    "missing_runtime": "専用環境またはモデルファイルが見つかりません。",
    "missing_artifact": "必要なモデルファイルが見つかりません。モデルの準備をやり直してください。",
    "artifact_mismatch": "モデルまたは公式ソースの照合に失敗しました。モデルの準備をやり直してください。",
    "insufficient_memory": "空きメモリが不足しているため、モデルを読み込んでいません。",
    "resource_check_failed": "空きメモリを確認できません。環境の状態を確認してください。",
    "device_unavailable": "CUDAを利用できません。NVIDIAドライバーとGPU用PyTorchの導入を確認してください。",
    "precision_unsupported": "指定した精度の演算を確認できません。別精度へは切り替えていません。",
    "precision_override_error": "公式ローダーの精度指定が想定と異なるため、読み込みを停止しました。",
    "identity_mismatch": "指定したDeciderのモデル・版・実行方式と応答が一致しません。",
    "launch_error": "専用PythonでDeciderを起動できませんでした。",
    "startup_error": "Deciderの起動に失敗しました。専用環境とモデルの準備を確認してください。",
    "worker_exited": "ローカルDeciderのプロセスが終了しました。",
    "worker_io_error": "Deciderとの通信が切れました。推論結果は得られていません。",
    "worker_stop_failed": "判定プロセスの停止を確認できません。重複起動を防ぐため再読み込みを保留しています。",
    "protocol_error": "ローカルDeciderの応答形式を確認できません。",
    "service_protocol_error": "共有Deciderサービスの応答形式を確認できません。",
    "service_unavailable": "共有Deciderサービスに接続できません。",
    "service_authentication_failed": "共有Deciderサービスを認証できません。アプリと共有サービスを同じバージョンに更新してください。",
    "timeout": "Deciderの待ち時間が上限に達しました。共有サービスの状態を確認して再実行してください。",
    "request_too_long": "入力がモデルのトークン上限を超えています。文章を短くして再実行してください。",
    "invalid_request": "Deciderへの要求形式を確認してください。",
    "invalid_answer": "Deciderの判定結果を読み取れませんでした。",
    "cuda_out_of_memory": "推論用のVRAMが不足しました。文章を短くして再実行してください。",
    "inference_error": "Deciderの推論を完了できませんでした。",
}
EXCEPTION_TYPES = frozenset({"OSError", "PermissionError", "FileNotFoundError", "ImportError", "ModuleNotFoundError",
    "RuntimeError", "ValueError", "TypeError", "KeyError", "MemoryError", "JSONDecodeError", "UnicodeDecodeError",
    "LengthError", "RequestError", "OutOfMemoryError", "WorkerError"})
RESOURCE_FIELDS = frozenset({"ram_available_bytes", "ram_required_bytes", "commit_available_bytes", "commit_required_bytes",
                            "cuda_available_bytes", "cuda_total_bytes", "cuda_required_bytes"})
OPERATIONS = frozenset({"process_stop", "taskkill", "wait_after_taskkill", "startup_exit_without_protocol_eof"})
RECOVERABLE_CODES = frozenset({"timeout", "request_too_long", "invalid_request", "cuda_out_of_memory"})


def public_error(error):
    """Allowlist every public field, including code and detail values."""
    error = error if isinstance(error, dict) else {}
    code = error.get("code")
    code = code if isinstance(code, str) and code in MESSAGES else "runtime_error"
    raw = error.get("details")
    raw = raw if isinstance(raw, dict) else {}
    details = {}
    for key in RESOURCE_FIELDS:
        value = raw.get(key)
        if type(value) in (int, float) and 0 <= value <= 2**64-1 and (type(value) is int or math.isfinite(value)):
            details[key] = value
    value = raw.get("max_length")
    if type(value) is int and 128 <= value <= 32768:
        details["max_length"] = value
    value = raw.get("exception_type")
    if isinstance(value, str) and value in EXCEPTION_TYPES:
        details["exception_type"] = value
    value = raw.get("operation")
    if isinstance(value, str) and value in OPERATIONS:
        details["operation"] = value
    value = raw.get("return_code")
    if type(value) is int and -(2**31) <= value < 2**31:
        details["return_code"] = value
    if raw.get("ram_check") == "vm_stat_free_inactive_speculative_estimate":
        details["ram_check"] = "vm_stat_free_inactive_speculative_estimate"
    if raw.get("commit_check") == "not_available_on_macos":
        details["commit_check"] = "not_available_on_macos"
    return {"code": code, "message": MESSAGES[code], "details": details,
            "recoverable": code in RECOVERABLE_CODES and error.get("recoverable") is True}
