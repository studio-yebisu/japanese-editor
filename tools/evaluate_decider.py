"""Run fixed synthetic pairs against the actual shared local Decider."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from decider import request_for_pair
import decider_runtime


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cases", type=Path, default=ROOT / "evaluation/cases.json")
    parser.add_argument("--output", type=Path, default=ROOT / ".local/decider/evaluation-result.json")
    args = parser.parse_args()
    cases = json.loads(args.cases.read_text("utf-8"))["cases"]
    results = []
    begun = time.perf_counter()
    for case in cases:
        started = time.perf_counter()
        response = decider_runtime.decide(request_for_pair(case["before"], case["after"], case["profile"]))
        answer = response["result"]["answers"]["meaning_shift"]
        actual = answer["choice"]
        item = {"id": case["id"], "profile": case["profile"], "expected": case["expected_label"],
                "expected_label_origin": case["expected_label_origin"], "actual": actual,
                "matches_creator_expectation": actual == case["expected_label"], "model": response["model"],
                "revision": response["revision"], "device": response["device"], "latency_ms": response["latency_ms"],
                **{key: response[key] for key in ("precision", "precision_override", "reference_path") if key in response},
                "wall_seconds_including_startup": round(time.perf_counter()-started, 3),
                "input_tokens": response["result"].get("usage", {}).get("input_tokens")}
        results.append(item)
        print(json.dumps(item, ensure_ascii=False), flush=True)
    report = {"status": "actual_local_inference_completed", "finished_at": datetime.now(timezone.utc).isoformat(),
              "human_gold": False, "held_out": False, "cases": len(results),
              "matches_creator_expectation": sum(item["matches_creator_expectation"] for item in results),
              "wall_seconds": round(time.perf_counter()-begun, 3), "results": results,
              "note": "合成入力と未レビューの制作者期待に対する実行記録です。日本語校正全般の性能保証ではありません。製品の抜粋処理や応答検査を含む結合試験とは別です。"}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes((json.dumps(report, ensure_ascii=False, indent=2)+"\n").encode("utf-8"))
    print(json.dumps({"written": str(args.output), "cases": report["cases"],
                      "matches_creator_expectation": report["matches_creator_expectation"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
