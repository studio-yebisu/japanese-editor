# Yomiyasu Studioの導入

Windowsの配布版と、macOSのソース実行では準備が異なります。認証と生成サービスの設定は [PROVIDERS.md](PROVIDERS.md)、Codex・Claudeへの組込みは [PLUGINS.md](PLUGINS.md)、確認済みの範囲は [STATUS.md](STATUS.md) にあります。

## Windows

Windows用ZIPを展開し、`YomiyasuStudio.exe` を起動します。モデルや認証情報は配布物に含めません。標準のSarashina校正にはPerplexity Deciderのローカル判定が必要です。別途Pythonと `uv` を用意し、展開したフォルダーで次を実行します。固定モデル約9.10GBと専用依存を取得します。

```powershell
python -B _internal/editor/tools/setup_decider.py --device cpu --cpu-dtype bfloat16 --max-length 2048
```

既存設定は上書きしません。初回はモデルと取得中の一時領域、専用依存等を合わせた追加の空きディスク30GiBを目安とし、取得前に容量を確認します。CPU BF16・文脈2048には空きRAM11GiB以上、Windowsではコミット余裕も11GiB以上が必要です。総RAMと空きRAMは別の条件です。詳しい予算と停止方法は [DECIDER_RUNTIME.md](DECIDER_RUNTIME.md) を参照してください。

### NVIDIA GPUを使う場合

校正中の処理が終わってから、設定の「判定モデルのメモリを解放」を押し、次を実行します。モデルは再利用し、固定したPyTorch・torchvisionを[PyTorch公式のCUDA wheel](https://download.pytorch.org/whl/cu126)から導入します。アプリが自動で依存を変更することはありません。

```powershell
python -B _internal/editor/tools/setup_decider.py --device cuda:0
```

アプリで「接続状態を更新」を押し、「PPDDの実行先」を「NVIDIA GPU（CUDA）」にして「実行先を保存」を押します。空きVRAM 11GiB・空きRAM 12GiB以上を確認してから保存します。NVIDIAドライバーも必要です。CUDAを利用できない場合やメモリ不足の場合は元の設定を保持します。推論直前にも条件を確認し、CPUへ自動では切り替えません。MacはCPUを選んでください。

## macOS・Apple Silicon（実験段階）

Mac向け `.app`・署名・公証はまだ用意していません。Python 3.12以降を用意し、ソースのアプリフォルダーで次を実行します。生成サービスとMCPの設定は上記の各文書に従います。

```sh
python3 -B app.py serve
```

[http://127.0.0.1:8896](http://127.0.0.1:8896) を開きます。Sarashina校正にはDeciderの意味判定が必要です。生成をオフにした規則の点検と、Gemini・Claudeの任意の生成では、Deciderを取得せずに使えます。

総メモリ24GBのApple Silicon Mac向けに、ローカルDeciderの `mac24` プリセットを用意しています。macOS 14以降、arm64 Python 3.12〜3.14（3.14.1を除く）、`uv` が対象です。CPU BF16・文脈2048・CPU threads 8で実行し、MPSは使いません。**Mac実機の推論は未検証**です。

```sh
# 事前点検だけ。取得・インストール・状態ファイル作成・認証情報の読取りはしません。
python3 -B tools/setup_decider.py --preset mac24 --check-only

# モデル約9.10GBと専用依存を取得し、新規の場合だけ設定します。
python3 -B tools/setup_decider.py --preset mac24
```

`ready_to_setup` は、導入前の条件だけを確認した結果です。総RAM 24 GiB以上と、現在の空きRAMの目安11 GiB以上を別々に確認します。演算対応・推論成功・速度は保証しません。Intel MacやRosettaのx86_64 Pythonはこのプリセットの対象外です。既存設定は保持し、プリセットの値へ変更しません。詳しい条件と未検証範囲は [DECIDER_RUNTIME.md](DECIDER_RUNTIME.md) を参照してください。
