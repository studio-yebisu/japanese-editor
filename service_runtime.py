"""Start the shared loopback service on demand for a local plugin session."""
import hashlib
import json
import os
import subprocess
import tempfile
import time
import uuid
from urllib.error import URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from runtime_paths import PERSISTENT_ROOT, cli_command, hidden_process

URL = "http://127.0.0.1:8896"
STOP_PATH = PERSISTENT_ROOT / "service-stop.json"


def record_service_stop():
    """Let a desktop client distinguish an explicit stop from a lost host."""
    PERSISTENT_ROOT.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=PERSISTENT_ROOT, prefix=".service-stop-", delete=False) as stream:
            temporary = stream.name
            stream.write(json.dumps({"nonce": uuid.uuid4().hex}).encode("utf-8"))
        os.replace(temporary, STOP_PATH)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def data_id():
    return hashlib.sha256(str(PERSISTENT_ROOT).encode("utf-8")).hexdigest()[:16]


def _probe():
    try:
        with build_opener(ProxyHandler({}), NoRedirect()).open(URL + "/api/health", timeout=1) as response:
            value = json.loads(response.read(4096))
        return isinstance(value, dict) and not value.get("closing", False) and value.get("application") == "studio-yebisu-japanese-editor" and value.get("protocol_version") == 1 and value.get("data_id") == data_id()
    except (URLError, OSError, ValueError):
        return False


def ensure_service():
    if _probe():
        return True
    PERSISTENT_ROOT.mkdir(parents=True, exist_ok=True)
    with (PERSISTENT_ROOT / "service.log").open("ab") as output:
        process = subprocess.Popen(cli_command("serve"), stdin=subprocess.DEVNULL, stdout=output, stderr=output,
                                   env={**os.environ, "YEBISU_EDITOR_DATA_DIR": str(PERSISTENT_ROOT)},
                                   start_new_session=os.name != "nt", **hidden_process())
    for _ in range(30):
        if _probe():
            return True
        if process.poll() is not None:
            return False
        time.sleep(0.1)
    return False


def stop_service():
    if not _probe():
        raise ValueError("このアプリの共有サービスが見つかりません。終了するサービスはありません。")
    request = Request(URL + "/api/service/stop", data=b"{}", headers={"Content-Type": "application/json"})
    try:
        with build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=5) as response:
            return json.loads(response.read(4096))
    except (URLError, OSError, ValueError):
        raise ValueError("共有サービスの終了を確認できません。接続状態を確認してください。") from None
