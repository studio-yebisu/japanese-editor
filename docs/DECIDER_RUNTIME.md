# Decider Flashの実行方法と確認結果

v0.2.16では公式Q8とllama.cppを使うC++ワーカーを追加しました。ブラウザー・CLI・MCPは従来どおり一つの共有サービスを使います。新規導入は[INSTALL.md](INSTALL.md)のQ8手順を参照してください。固定したモデルの元チェックポイント、GGUFのSHA、llama.cppのコミットは[cpp/manifest.json](../cpp/manifest.json)にあります。

## C++・公式Q8版

公式`perplexity-ai/pplx-decider-flash-gguf-v1.2`の`decider-flash-v1.2-q8_0.gguf`（5,157,837,472 bytes）を使います。専用ワーカーがprotocol v3を実装し、文字列・トークン列・readout位置を従来版と照合しています。llama-serverや文章生成APIで判定を代用しません。文脈上限は既定2048トークン、空きRAMの起動ガードは8GiBです。長文の分割と判定する抜粋はアプリ側で決め、ワーカーは入力を切り捨てません。

NVIDIA CUDAを優先し、GPUに入る層だけを載せます。空きが少なければCPUを使います。WindowsではNVMLの専用VRAM空きも確認します。MacはMetalを使う実装で、共有メモリをRAMとVRAMの別容量として足しません。Mac実機は未検証です。

OOM時は所有するワーカーを停止してからGPU層数を半減し、最後はCPUで元の要求を再試行します。合計3試行までで、要求全体の待ち時間上限は延ばしません。2方式のモデルを同時には読み込みません。設定で実行方式を選び、応答に`runtime_backend`、`quantization`、`gguf_sha256`、`llama_commit`、実デバイス・GPU層数・CPU併用・OOM再試行回数を記録します。

2026-10-21、16件の同一合成入力で従来CPU BF16とQ8 CPUの判定が16/16一致し、確率の差は最大0.012364でした。CUDA3層＋CPUでも16/16一致し、従来BF16との差は最大0.012077でした。特殊トークン・日本語・emoji・CRLFを含む入力のprompt・token IDs・marker位置も16/16一致しました。これは日本語校正全体や公式FP32との一致を保証する結果ではありません。[入力と実測](../evaluation/cpp-parity-results.json)、[再現用コマンドと契約](../cpp/README.md)

以下は、比較・復帰用に維持している従来PyTorch版の手順です。Q8版の必要容量やMetalの条件とは異なります。

## 従来版（PyTorch）の実行条件

| 項目 | 現在の設定・実測環境 |
| --- | --- |
| モデル | `perplexity-ai/pplx-decider-flash-v1.2` |
| 固定 SHA | `6d771ad7b5ecff5e93b8fc7b90d4c5ccc341954d` |
| 方式 | 公式 `pplx_decider` 0.3.0、`fast=False`、CPU BF16 |
| 精度の記録 | `precision: bfloat16`, `precision_override: true` |
| 実行経路 | `native_fast_false_cpu_bfloat16_override` |
| 文脈上限 | `max_length=2048`（テンプレート・問い・原文・候補等を含むトークン数） |
| 専用環境 | torch 2.14.0+cpu、transformers 5.17.0、CPU threads 8 |
| 新規保管先 | Windows: `%LOCALAPPDATA%/StudioYebisu/JapaneseEditor/decider/`。macOS: `~/Library/Application Support/StudioYebisu/JapaneseEditor/decider/`。環境・モデル・設定・生ログは配布対象外 |

改名後も既存のモデルと設定を使うため、保管先の `StudioYebisu/JapaneseEditor/` は維持します。

公式 native は CPU で FP32 を選びます。このモードでは、隔離したワーカー内で backbone の `from_pretrained` に渡す dtype だけを、ロード中の限定された範囲で BF16 に変え、decision head も BF16 に揃えます。公式ソースや重みのバイト列、forward 処理、`torch.float32` 自体は変更しません。別モデルや量子化モデルへの置換ではありません。**公式 CPU FP32 との数値・判定の一致は未検証**です。対応しない環境で別精度へ自動的に切り替えることはありません。

## 新規環境で準備する

Python 3.12 以降と `uv` が必要です。アプリのディレクトリで次を実行します。このコマンドを実行すると、固定版23ファイル（約9.10 GB）と専用の依存パッケージを取得します。既存設定は上書きしません。

```powershell
python -B tools/setup_decider.py --device cpu --cpu-dtype bfloat16 --max-length 2048
```

Windowsの配布ZIPでは、展開したフォルダーで次を使います。Sarashinaの校正にはこのローカル判定環境が必須です。別途Pythonとuvを用意してください。生成をオフにした規則点検では取得不要です。

```powershell
python -B _internal/editor/tools/setup_decider.py --device cpu --cpu-dtype bfloat16 --max-length 2048
```

上表の保管先へ `.venv`、固定 SHA のモデルフォルダ、`config.json` を作ります。macOSでは `python3` に読み替えます。原典の LICENSE / NOTICE と取得物のハッシュを保持し、起動時にも照合します。通常のアプリ用 Python に torch を入れる必要はありません。`YEBISU_EDITOR_DATA_DIR` で保管先を変更できます。既存環境のモデルパスは設定の絶対パスを引き続き使えます。

### 取得前のディスク点検

Windows・Macとも、setupコマンドは取得を始める前にディスクの空きを確認します。同じコマンドに `--check-only` を付けると、取得・インストール・書込みなしで数値だけを確認できます。Windows配布版の例:

```powershell
python -B _internal/editor/tools/setup_decider.py --device cpu --cpu-dtype bfloat16 --max-length 2048 --check-only
```

新規導入では、モデル・専用環境・uvキャッシュ・一時領域が同じディスクにある場合、**追加の空き30 GiB以上**を導入時の目安として要求します。内訳は、約9.10GBの固定モデルを収める9 GiB、分割取得データと組立中ファイルの共存に備える9 GiB、専用依存4 GiB・uvキャッシュ6 GiB・一時領域2 GiBです。取得済みの標準配置が固定manifestと全ファイルのサイズに一致すれば、モデル再利用候補として追加分を12 GiBへ減らします。重みのハッシュは取得処理で改めて照合し、再取得が必要なら取得前に容量を再確認します。

これは余裕を含む導入予算で、正確なインストール容量や成功の保証ではありません。依存パッケージ・既存キャッシュ・一時ファイルの増加により、さらに容量が必要になる場合があります。保存先、`UV_CACHE_DIR`（未指定ならuvの標準キャッシュ先）、OSの一時領域が別ディスクの場合は、それぞれの必要分を別に確認します。空き容量を取得できない場合も、取得を開始せず理由を表示します。

JSONの `disk.status` は `sufficient` / `insufficient` / `unknown`、`disk.required_bytes` は追加容量の合計です。`disk.volumes` にディスクごとの `available_bytes`・`required_bytes`・用途を示します。同じ一枚のディスクの場合だけ、`disk.available_bytes` にその空きを入れます。`inventory_scope` が示すとおり、点検は新規の標準配置が対象です。既存設定の別の絶対パスを調べるものではなく、`model_files_present: false` や依存一覧が空でも、既存環境の故障とは扱いません。

初回ロード前に、次の空き容量を確認します。BF16 では Windows のコミット可能メモリも同じ量だけ必要です。ページファイルを含むコミット上限と、物理 RAM の空きは別々に確認します。

| CPU の設定 | 必要な空き RAM | 必要なコミット可能メモリ |
| --- | --- | --- |
| BF16、`max_length <= 2048` | 11 GiB 以上 | 11 GiB 以上 |
| BF16、より大きい文脈（例: 8192） | 12 GiB 以上 | 12 GiB 以上 |
| 公式既定の FP32 | 24 GiB 以上 | BF16 用の追加ガード対象外 |

ワーカーは `HF_DEACTIVATE_ASYNC_LOAD=1` を指定し、ロード時の非同期先読みを止めます。上表は起動ガードで、全入力での動作保証ではありません。macOSでは `vm_stat` から空きRAMを調べ、Windows固有のコミット確認は行いません。`max_length` の既定は2048です。8192を選ぶ場合は `--max-length 8192` を指定します。既存設定を変更する場合は保管先の `config.json` を確認してください。

起動ガードで資源不足になった場合は、モデルを読み込まず元のエラーを返します。ワーカーが自然終了するタイミングと停止処理が重なっても、起動したプロセスの終了と専用通信の終了を両方確認できれば、資源不足の案内を保持します。終了を確認できない場合は、子プロセスが残る可能性を考慮して再読み込みを止めます。

## Apple Silicon・総メモリ24GB向けの実験設定

`mac24` は、Apple Silicon の Mac で **CPU BF16・文脈2048・CPU threads 8** を選ぶ実験用プリセットです。MPS・Metal は使用しません。総メモリ24GBのMacを開発対象としていますが、Mac実機での導入・演算・速度・長文処理は未検証です。すべての24GB搭載Macで動くという保証ではありません。

macOS 14以降、arm64版Python 3.12〜3.14（3.14.1を除く）、`uv` が必要です。まず、次の点検だけを実行できます。Python 3.12を推奨します。Intel Mac と Rosetta 上のx86_64 Pythonはこのプリセットの対象外です。

```sh
python3 -B tools/setup_decider.py --preset mac24 --check-only
```

このコマンドはモデルの取得、依存のインストール、設定・状態ファイルの作成、認証トークンの読取りを行いません。OS・Python・`uv`、`sysctl` の総RAM、`vm_stat` の現在の空きRAMの目安、専用環境にある依存のメタデータをJSONで表示します。torchのimport、BF16演算、モデルのハッシュ照合や推論も実行しません。`ready_to_setup` はセットアップの事前条件を満たしたという意味で、推論できたという結果ではありません。未充足なら `not_ready` と終了コード2を返します。

プリセットは **総RAM 24 GiB以上** を確認し、それとは別に、既存と同じ **空きRAM 11 GiB以上** を要求します。総容量だけでは実行できるかを判断しません。空きRAMは free / inactive / speculative の合計による目安で、メモリプレッシャーや他アプリの使用状況が変われば不足することがあります。Windowsのコミット確認やCUDAのVRAM容量とは別の指標です。

点検後、モデル約9.10GBと専用依存を取得する場合は次を使います。既存の `config.json` がある場合はそのまま残し、プリセットの値を反映しません。表示された保管先の設定を確認してください。

```sh
python3 -B tools/setup_decider.py --preset mac24
```

`--device cuda:0`、`--cpu-dtype float32`、`--max-length 8192` など、プリセットと矛盾する値は取得前にエラーにします。従来の汎用CPU/CUDAオプションは引き続き使えます。実際のロード時にも同じ空きRAMガードと小さいCPU BF16演算の確認を行い、未対応なら失敗として返します。別精度やMPSへの自動切替えはしません。CPU BF16と公式CPU FP32の判定一致は未検証です。

固定依存の [torch 2.14.0](https://pypi.org/project/torch/2.14.0/#files) と [torchvision 0.29.0](https://pypi.org/project/torchvision/0.29.0/#files) には `macosx_14_0_arm64` wheelがあります。これは依存を用意できる根拠で、Decider全体のMac動作確認ではありません。公式 [固定Flashの説明](https://huggingface.co/perplexity-ai/pplx-decider-flash-v1.2/blob/6d771ad7b5ecff5e93b8fc7b90d4c5ccc341954d/README.md) と異なるCPU BF16精度の記録は上の条件表のとおり保持します。

## 起動・共有・停止

```powershell
python -B app.py serve
```

[http://127.0.0.1:8896](http://127.0.0.1:8896) を開きます。MCPはサービスを自動起動でき、通常はこの起動操作が不要です。最初の候補判定でワーカーを読み込み、同じモデルを共有します。`config.json` の `service_url` は `http://127.0.0.1:8896` です。接続できないときは `service_unavailable` 等を返し、第二のモデルを独立してロードしません。無操作が5分続くとモデルをメモリから解放します。

v0.2.7以降の共有RPCは原文・候補を送信する前にHMACで接続先を認証し、応答のMACも確認します。アプリと共有サービスの両方を更新してください。旧サービスを認証できなければ本文は送らず、旧方式や別のサービスへ切り替えません。[共有サービスの更新](PLUGINS.md#共有サービスの更新)

認証は実際の待受ポートと同じTCP接続に結び付け、ポートも排他的に確保します。共有鍵は設定保存領域の `rpc/secret-v1` に保存し、Windowsでは現在のユーザーのDPAPI、POSIXでは本人所有・0600で保護します。鍵は配布物に含めません。これは暗号化ではなく、同じOSユーザー権限の悪意のあるプロセスや管理者、OSの通信経路の操作は防げません。[認証の範囲と限界](../SECURITY.md#共有サービスの認証と限界)

HTTPのヘッダーと本文の受信待機は合わせて30秒まで、同時に処理する接続は32までです。受信後のモデル処理をこの期限で打ち切るものではありません。

```powershell
python -B app.py decider-status
python -B app.py decider-stop
```

`decider-status` は未読込・読込中・判定中・準備完了等を区別します。`decider-stop` は処理中の判定が終わってから、アプリが起動した専用ワーカーとその子プロセスを終了してモデルを降ろします。サーバーは残り、次の判定で再読込します。停止を確認できなければ、二重ロードを避けるため再起動を止めます。アプリサーバーを Ctrl+C で終了すると、そのワーカーも終了します。他のアプリを終了してメモリを空ける操作や ComfyUI の変更は行いません。

Windowsのexe版では、専用Pythonや停止コマンドが配布物のDLLを誤って読み込まないよう、外部プロセスを起動する間だけDLL検索先を調整し、直後に元へ戻します。

## 要求の時間上限

v0.2.8以降では、明示した `timeout` をロック取得・初回起動・推論の待機全体で使います。ロックを取得できず期限が切れた場合は、別の要求が使っているワーカーに触れず終了します。起動待ちも残り時間と `startup_timeout_seconds` の短い方までとし、期限切れの要求について新しい推論を開始しません。

OSの同期処理や、起動済みプロセスの安全な終了を待つ時間が加わる場合があります。応答が指定秒数ぴったりに返る保証ではありません。停止を確認できない場合は、二重起動を防ぐため再読込を保留します。

## 判定の範囲と失敗時の表示

候補ごとに原文へ一件だけ変更を適用し、`meaning_shift` を一問だけ渡します。結果は `preserved`、`shifted`、`needs_review`。モデルが返す修正理由の文章はありません。画面の理由は候補を作った生成モデルの説明です。起動・推論のエラーは固定文案へ変換し、生の例外文字列や絶対パスは返しません。判定の確率を実測した正解率として表示しません。

短い文章は `scope: document`、長い文章は変更付近の抜粋を `scope: excerpt` として比較します。2048設定では通常600文字以内の原文抜粋を使いますが、文字数とトークン数は一致しません。抜粋の外の前提や、複数候補を組み合わせた最終稿の意味は、この判定の対象外です。全文の yomiyasu 点検・本文照合は出力前に別途行います。

要求がトークン上限を超えた場合は切り詰めず、回復可能な `request_too_long` を返します。入力形式エラーも回復可能として、読み込み済みモデルを保持します。資源不足・タイムアウト・応答不正等は失敗または未判定として残し、成功扱いや別モデルへの自動置換はしません。

## 今回の実測

| 対象 | 結果 |
| --- | --- |
| 初回モデル読み込み | 27.1309秒 |
| 最初の要求 | 2.3391秒（読み込みを除く） |
| 合成12組 + 対照6組 | 一件1.7067〜3.2159秒、入力258〜348トークン |
| CLI の抜粋判定1件 | 合成文3,273文字中592文字を比較、9.184秒、`scope: excerpt` |
| 短文試験中の Private メモリ最大値 | 約10.03 GiB（最大文脈の測定ではない） |
| 制作者期待との照合 | 合成12組中11組、追加対照6組中6組 |

期待ラベルは未レビューの制作者案で、人が確定した正解でも、独立した未使用のテストセットでもありません。曖昧な指示対象では期待 `needs_review` に対して `shifted` が返りました。この照合数は校正品質の正解率を表しません。結果は [runtime-results.json](../evaluation/runtime-results.json) にあります。

native のトークン上限超過を実際に起こし、回復可能なエラーの後も同じワーカーで有効な要求を実行できることを確認しました。

ブラウザでは実際の Opus 5.5 生成2候補に Flash 判定を付け、一件だけ採用して全文点検・照合後の保存開始まで確認しました。Claude Code の一時 MCP 設定からもレビュー → Flash 判定 → 最終稿生成を実行しました。Codexへ登録したMCPからも、READMEの2候補を実判定して最終稿を点検・照合しました。保存先ファイルの読み返し、macOS実機、27B、CUDA、hosted API、公式CPU FP32との比較、幅広い実文・長文の人手評価は未実施です。
