"""Explicit, resumable acquisition of the fixed official Q8 model; no inference."""
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
from pathlib import Path
import shutil
import sys
import time
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from runtime_paths import PERSISTENT_ROOT

MANIFEST = json.loads((ROOT / "cpp/manifest.json").read_text("utf-8"))
TARGET = PERSISTENT_ROOT / "decider/gguf" / MANIFEST["gguf_revision"]
CHUNK = 64 * 1024 * 1024


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for piece in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(piece)
    return digest.hexdigest()


def main():
    TARGET.mkdir(parents=True, exist_ok=True)
    model = TARGET / MANIFEST["gguf_file"]
    size = MANIFEST["gguf_bytes"]
    base = f"https://huggingface.co/{MANIFEST['gguf_repo']}/resolve/{MANIFEST['gguf_revision']}/"
    if not (model.is_file() and model.stat().st_size == size and sha256(model) == MANIFEST["gguf_sha256"]):
        if model.exists():
            raise RuntimeError("Existing Q8 model failed verification. Preserve it and check the model directory before retrying.")
        if shutil.disk_usage(TARGET).free < size * 2 + 2 * 1024 ** 3:
            raise RuntimeError("Q8 acquisition requires about 12 GiB of additional free disk space.")
        chunks = TARGET / "ranges"
        chunks.mkdir(exist_ok=True)
        def download(start):
            end = min(size, start + CHUNK) - 1
            piece = chunks / f"{start}-{end}.chunk"
            if piece.is_file() and piece.stat().st_size == end - start + 1:
                return piece
            temporary = piece.with_suffix(".partial")
            for attempt in range(4):
                offset = start + (temporary.stat().st_size if temporary.exists() else 0)
                if offset > end:
                    break
                request = Request(base + MANIFEST["gguf_file"] + f"?range_start={offset}&range_end={end}", headers={"Range": f"bytes={offset}-{end}"})
                try:
                    with urlopen(request, timeout=45) as response:
                        if response.status != 206 or response.headers.get("Content-Range") != f"bytes {offset}-{end}/{size}":
                            raise RuntimeError("Official endpoint did not honor the exact range")
                        with temporary.open("ab") as stream:
                            received = offset
                            for data in iter(lambda: response.read(2 * 1024 * 1024), b""):
                                received += len(data)
                                if received > end + 1:
                                    raise RuntimeError("Oversized range response")
                                stream.write(data)
                    if temporary.stat().st_size == end - start + 1:
                        break
                except Exception:
                    if attempt == 3:
                        raise
                    time.sleep(1)
            if not temporary.exists() or temporary.stat().st_size != end - start + 1:
                raise RuntimeError("Incomplete Q8 model range")
            temporary.replace(piece)
            print(json.dumps({"downloaded_bytes": piece.stat().st_size, "range_start": start}), flush=True)
            return piece
        starts = range(0, size, CHUNK)
        with ThreadPoolExecutor(max_workers=12) as executor:
            for future in as_completed(executor.submit(download, start) for start in starts):
                future.result()
        temporary = model.with_suffix(".part")
        with temporary.open("wb") as output:
            for start in range(0, size, CHUNK):
                end = min(size, start + CHUNK) - 1
                with (chunks / f"{start}-{end}.chunk").open("rb") as stream:
                    shutil.copyfileobj(stream, output, 8 * 1024 * 1024)
        if temporary.stat().st_size != size or sha256(temporary) != MANIFEST["gguf_sha256"]:
            raise RuntimeError("Official Q8 model SHA-256 mismatch")
        temporary.replace(model)
    for name in ("LICENSE", "NOTICE", "README.md"):
        with urlopen(base + name, timeout=30) as response:
            content = response.read(128000)
        (TARGET / name).write_bytes(content)
    receipt = {"status": "sha256_verified", **MANIFEST}
    (TARGET / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n", "utf-8")
    print(json.dumps({"status": "sha256_verified", "path": str(model), "bytes": size, "sha256": MANIFEST["gguf_sha256"]}), flush=True)


if __name__ == "__main__":
    main()
