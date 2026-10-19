# 原典・利用条件・検証した範囲

確認日: 2026-10-17（日本時間）。原典調査と、この PC での実行を区別して記録する。固定版 Flash の CPU BF16 推論、ブラウザと Claude Code MCP からの利用を実行済み。実行条件と限定した短文試験は [DECIDER_RUNTIME.md](DECIDER_RUNTIME.md)、現行状態は [STATUS.md](STATUS.md) を参照。幅広い日本語品質の独立評価と Production 採用は行っていない。

## yomiyasu

| 項目 | 確認結果 |
| --- | --- |
| 原典 | [nanaism/yomiyasu](https://github.com/nanaism/yomiyasu) |
| 固定コミット | `68075bea91ae27958766effd6aaa56d42d6be125` |
| 版 | `2.0.0` |
| 役割 | 生成 AI 向けの日本語推敲方針と、表現・書式の lint、原文と案の全文差分、意味が変わる恐れのある形の検査 |
| 利用条件 | MIT。`Copyright (c) 2026 nanaism` と原典の許諾表示を保持する。Unicode 由来部分の `UNICODE-LICENSE.txt` も保持する |
| 配置 | `vendor/yomiyasu/`。スキル本文、全 references、全 scripts、LICENSE と Unicode notice を無改変で保存 |
| 来歴 | `vendor/yomiyasu/UPSTREAM.json` に固定コミット、原典の相対パス、ファイル URL、Git blob OID、SHA-256、byte 数を記録 |

固定された一次資料:

- [SKILL.md](https://github.com/nanaism/yomiyasu/blob/68075bea91ae27958766effd6aaa56d42d6be125/skills/yomiyasu/SKILL.md)
- [言い直しテスト](https://github.com/nanaism/yomiyasu/blob/68075bea91ae27958766effd6aaa56d42d6be125/skills/yomiyasu/references/rephrase-test.md)
- [小説・エッセイの扱い](https://github.com/nanaism/yomiyasu/blob/68075bea91ae27958766effd6aaa56d42d6be125/skills/yomiyasu/references/doc-types/essay.md)
- [付属ツールの仕様](https://github.com/nanaism/yomiyasu/blob/68075bea91ae27958766effd6aaa56d42d6be125/docs/tools.md)
- [限界と著者の検証範囲](https://github.com/nanaism/yomiyasu/blob/68075bea91ae27958766effd6aaa56d42d6be125/docs/limitations.md)
- [MIT License](https://github.com/nanaism/yomiyasu/blob/68075bea91ae27958766effd6aaa56d42d6be125/LICENSE)、[Unicode License](https://github.com/nanaism/yomiyasu/blob/68075bea91ae27958766effd6aaa56d42d6be125/UNICODE-LICENSE.txt)

原典が優先するのは、主張、比重、確度、文の働きの保持と、情報を勝手に足さないことである。候補数が減ったことや `rule_penalty_score` を、文章品質や意味保持の合格点として使わない。検査候補がなくても意味が変わる可能性があるため、全文差分を確認する。

小説では、読み取れる比喩、会話、語り手の主観、短文、ダッシュ、三点リーダー、多義性、文章のリズムを残す。upstream の CLI は小説とエッセイをともに `essay` として扱い、lint は両者を区別しない。アプリの文体・会話プロファイルは追加の設計であり、upstream の検出だけで機械的に書き換える根拠にしない。

### 実行契約

Python 標準ライブラリのみで動く。upstream は Python 3.8 以上の文法を用いる。単体 lint に必要な同梱モジュールは `scripts/markdown_visibility.py`。diff と check には同じ版の各 scripts と `references/lists/` も保持する。

プロジェクトの `apps/japanese-editor/` を作業ディレクトリとする例:

```powershell
python -B vendor/yomiyasu/scripts/yomiyasu_lint.py original.md --doc-type article --reader informed --json
python -B vendor/yomiyasu/scripts/yomiyasu_diff.py original.md candidate.md --stance 説明 --doc-type article --reader informed --json
python -B vendor/yomiyasu/scripts/yomiyasu_check.py run --source original.md --final candidate.md --doc-type article --reader informed --stance 説明 --fact-gaps --state-dir .local/yomiyasu --json
python -B vendor/yomiyasu/scripts/yomiyasu_check.py verify --final candidate.md --source original.md --run-id <実行ID> --state-dir .local/yomiyasu --json
```

単体 lint はファイル名を省くか `-` とすると標準入力を読む。原典スキルの一連の推敲を再現するときは、最終案の検査を check の CLI でまとめて実行し、提出直前の verify を行う。個々の API に内部関数を import する方式は原典の推奨手順ではない。

lint の JSON は次の形式。`findings` は同じ行に複数出る場合があり、列位置や置換文字列を必ず返すわけではない。行は1始まりで、`message` は見直し理由、`snippet` は対象の抜粋である。

```json
{
  "format": "yomiyasu-lint",
  "format_version": 2,
  "tool_version": "2.0.0",
  "candidate_count": 4,
  "counts": {"error": 0, "warn": 3, "info": 1},
  "no_rule_findings": false,
  "not_judged": ["meaning_preservation", "reader_comprehension"],
  "context": {"doc_type": "article", "reader": "informed", "skipped_by_doc_type": {}},
  "findings": [{"rule": "meta_filler", "line": 1, "severity": "warn", "message": "見直しの理由", "snippet": "対象の抜粋"}]
}
```

上の形式説明では、`metrics` と品質点数ではない `rule_penalty_score`、候補の続きは省略した。diff は `format: yomiyasu-diff`, `format_version: 2` で、`status`, `changed`, `meaning_risks`, `meaning_risk_count`, `fail_on_meaning_risk_count`, `full_diff.hunks` 等を返す。check の run/verify は `format: yomiyasu-check-result`, `format_version: 1` である。異なる形式の結果を同一 schema として扱わない。

候補があっても、`--strict` や `--fail-on` を付けない lint の終了コードは0。終了コード0や check の `executed` は検査の実行状態を示し、修正案の適切さを保証しない。`partial` と入力・実行エラーは通常完了と区別する。`--reader` はメタデータとして記録されるが、現行 checker の検出結果自体は変えない。

check は原稿全文を含む記録をローカルに保存するため、アプリの状態フォルダを明示し、公開データ・配布物から除外する。任意の `yomiyasu_hook.py` は同梱されているが、アプリの試作ではホスト設定へのフック導入や有効化を行っていない。

### この環境で実行したこと

Windows の `python`（3.13.7）で同梱 lint の `--help` と、合成した日本語2行を標準入力から渡す `--json --doc-type article --reader informed` を実行した。JSON を返して終了コード0だった。候補は4件（warn 3、info 1）で、`meta_filler`、`negative_parallelism`、`metaphor_verb` が含まれた。これは検査器が起動・検出・JSON 出力できた証拠であり、自然な日本語への修正品質を測った結果ではない。

著者の文書はWindows 実機、対話 UI、プラグイン配布形態で十分に検証していない範囲も明示している。今回の単体 lint 実行から、Codex / Claude のフック全体の Windows 互換性を推定しない。

## Perplexity Decider v1.2 / Flash v1.2

| 項目 | Decider | Decider Flash |
| --- | --- | --- |
| 原典 | [pplx-decider-v1.2](https://huggingface.co/perplexity-ai/pplx-decider-v1.2) | [pplx-decider-flash-v1.2](https://huggingface.co/perplexity-ai/pplx-decider-flash-v1.2) |
| 固定コミット | `225c913ddc8761a8264f04f64548e1f97e443eb3` | `6d771ad7b5ecff5e93b8fc7b90d4c5ccc341954d` |
| モデルカードの backbone | Qwen3.8-27B | Qwen3.5-4B |
| 重みの利用条件 | Apache-2.0、同梱 NOTICE も保持 | Apache-2.0、同梱 NOTICE も保持 |
| カードの BF16 メモリ目安 | 重み約52 GiB と作業用メモリ | 重み約9 GiB と作業用メモリ |

固定された一次資料:

- [Decider モデルカード](https://huggingface.co/perplexity-ai/pplx-decider-v1.2/blob/225c913ddc8761a8264f04f64548e1f97e443eb3/README.md)
- [Flash モデルカード](https://huggingface.co/perplexity-ai/pplx-decider-flash-v1.2/blob/6d771ad7b5ecff5e93b8fc7b90d4c5ccc341954d/README.md)
- [Flash native 推論の実装](https://huggingface.co/perplexity-ai/pplx-decider-flash-v1.2/blob/6d771ad7b5ecff5e93b8fc7b90d4c5ccc341954d/source/src/pplx_decider/model.py)
- [native package の依存](https://huggingface.co/perplexity-ai/pplx-decider-v1.2/blob/225c913ddc8761a8264f04f64548e1f97e443eb3/source/pyproject.toml)
- [native 推論の実装](https://huggingface.co/perplexity-ai/pplx-decider-v1.2/blob/225c913ddc8761a8264f04f64548e1f97e443eb3/source/src/pplx_decider/model.py)
- [native request / answer の実装](https://huggingface.co/perplexity-ai/pplx-decider-v1.2/blob/225c913ddc8761a8264f04f64548e1f97e443eb3/source/src/pplx_decider/protocol.py)
- [Decider LICENSE](https://huggingface.co/perplexity-ai/pplx-decider-v1.2/blob/225c913ddc8761a8264f04f64548e1f97e443eb3/LICENSE)、[Decider NOTICE](https://huggingface.co/perplexity-ai/pplx-decider-v1.2/blob/225c913ddc8761a8264f04f64548e1f97e443eb3/NOTICE)
- [Flash LICENSE](https://huggingface.co/perplexity-ai/pplx-decider-flash-v1.2/blob/6d771ad7b5ecff5e93b8fc7b90d4c5ccc341954d/LICENSE)、[Flash NOTICE](https://huggingface.co/perplexity-ai/pplx-decider-flash-v1.2/blob/6d771ad7b5ecff5e93b8fc7b90d4c5ccc341954d/NOTICE)
- [公式 Decisions API guide](https://docs.perplexity.ai/docs/decisions/quickstart)、[API reference](https://docs.perplexity.ai/api-reference/decisions-post)、[料金](https://docs.perplexity.ai/docs/getting-started/pricing)

Decider は、アプリが渡した状態、問い、判断基準に対して、選択・真偽確率・順序付き評価を返す。日本語を書き直す生成モデルではなく、修正文、任意の位置の抽出、判断理由の自然文を生成しない。修正候補と理由を用意する生成モデルは別に必要である。

このアプリに適用する場合は、原文・修正候補・規則・用途を渡し、原文維持の選択肢とレビュー保留を用意する。Decider の確率や confidence は、現実の正しさや意味保持を実測した百分率ではない。Flash の多問一括判定は問い同士の影響があるため、公式案内の1問ずつを基準に評価する。Flash から大型版への再判定も、必ず品質が改善するとは限らない。

### 実行・ホスティングの制約

- native は専用の `pplx_decider` package（確認した版は0.3.0、Python 3.12+）を使う。非 causal な attention と専用 decision head があり、一般の chat 生成 API、Ollama、llama.cpp、GGUF 等にそのまま入るとは確認できていない。
- native は exact commit を指定して取得・ロードできる。今回 Flash の固定版23ファイル、約9.10 GB を照合し、ignored の専用環境（torch 2.14.0+cpu / transformers 5.17.0）で CPU 推論を実行した。`fast=False` と明示的な CPU BF16 読込を使う。公式 native の CPU 既定 FP32 との判定・数値の一致は未検証。27B と CUDA は実行していない。公式の大規模 GPU の速度値を、この Windows PC の速度として示さない。
- 公式の hosted API は `POST https://api.perplexity.ai/v1/decisions`。API key、外部送信、課金が伴う。認証情報はバックエンドまたは保護したローカル設定で扱い、ブラウザー配布物へ埋め込まない。今回 API key の取得や paid request は実施していない。
- native と hosted で文脈上限、質問数、score 段階数、usage の形式が異なる。現行 native 実装はテキストと一問の choice に限定し、タイムアウト、要求上限超過、資源不足、応答不正を成功と区別する。hosted の429 / 5xx / 取消は実装・実行していない。
- hosted のモデル指定名が返答へ echo されても、実際の checkpoint SHA を示すわけではない。要求モデル、日時、規則版、問いの構成、結果を記録する。
- 公開モデルの Apache-2.0 と、hosted API のサービス条件・料金・データの扱いは別に確認する。

### 日本語品質について確認できていないこと

調べた公式モデルカード、native の検証例、Decisions API guide には、日本語文章校正、yomiyasu との組合せ、日本語の意味保持を測った benchmark の根拠がなかった。一般の Language カテゴリーの成績を、日本語文章校正の性能として扱わない。

現行アプリでは Flash を実行し、実行・失敗・未判定を区別している。合成12組は制作者の未レビュー期待と11組、追加対照6組は6組が一致した。曖昧な指示対象の例は期待 `needs_review` に対して実際は `shifted`。期待ラベルは人が確定した正解でも独立テストセットでもなく、この照合数を日本語校正の正解率としない。[runtime-results.json](../evaluation/runtime-results.json) に短文の結果を記録した。幅広い実用文・ブログ・小説・会話について、変更すべき箇所と原文維持すべき箇所を独立して人手評価する必要がある。

## この資料から導いた設計

yomiyasu の方針と規則で候補を整理し、別の生成モデルで案を書き、Decider で比較する構成を試作した。ブラウザと Claude Code MCP で実行したが、完成アプリとしての広い校正品質や長文速度を検証したものではない。自動検査・モデル判断・利用者の採否を別々に記録し、原文と未確定の情報を保持する。各 Flash 判定は一件の変更だけが対象で、長文では抜粋を使い、複数案を組み合わせた最終稿の意味保持を保証しない。
