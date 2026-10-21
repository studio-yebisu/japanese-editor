"""Sequential real inference comparison. Never generate or send text externally."""
import argparse
import json
from pathlib import Path
import sys

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import decider_runtime


def run(path, cases):
    owner = decider_runtime.DeciderRuntime(path)
    results = []
    try:
        for case in cases:
            output = owner.decide(case["request"], timeout=480)
            results.append({"id": case["id"], "answer": output["result"]["answers"]["meaning_shift"],
                            "input_tokens": output["result"]["usage"]["input_tokens"],
                            "identity": {key: output[key] for key in ("model", "revision", "backend", "device", "precision",
                                "precision_override", "gguf_sha256", "llama_commit", "gpu_layers", "cpu_assisted", "oom_retries") if key in output}})
            print(json.dumps({"case": case["id"], "completed": len(results), "total": len(cases)}), flush=True)
    finally:
        owner.stop()
    return results


def main():
    parser = argparse.ArgumentParser(description="同じ入力で従来版とQ8版の判定・確率を比較します。")
    parser.add_argument("--reference-config", required=True, type=Path)
    parser.add_argument("--candidate-config", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if decider_runtime.status().get("loaded"):
        raise SystemExit("Release the app's loaded model before comparing; two model owners are not allowed.")
    cases = json.loads((ROOT / "evaluation/cpp-parity-inputs.json").read_text("utf-8"))
    reference = run(args.reference_config, cases)
    candidate = run(args.candidate_config, cases)
    rows = [{"id": a["id"], "choices_agree": a["answer"]["choice"] == b["answer"]["choice"],
             "tokens_agree": a["input_tokens"] == b["input_tokens"],
             "max_probability_delta": max(abs(a["answer"]["probabilities"][key] - b["answer"]["probabilities"][key])
                                          for key in a["answer"]["probabilities"])} for a, b in zip(reference, candidate)]
    report = {"cases": len(rows), "choice_agreement": sum(row["choices_agree"] for row in rows),
              "max_probability_delta": max(row["max_probability_delta"] for row in rows),
              "rows": rows, "reference": reference, "candidate": candidate,
              "scope": "Synthetic examples; agreement is not a Japanese proofreading quality guarantee."}
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", "utf-8")
    print(json.dumps({key: report[key] for key in ("cases", "choice_agreement", "max_probability_delta")}))


if __name__ == "__main__":
    main()
