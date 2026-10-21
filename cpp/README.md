# PPDD C++ワーカー

Perplexityの公式Q8 GGUFとlibllamaを使い、文章の修正で意味が変わるかを判定します。モデル・ソース・プロトコルの固定値は [manifest.json](manifest.json) にあります。モデル重みはこのリポジトリに含めません。

## 判定の契約

- 公式Flash v1.2のprotocol v3を実装します。テキストの`choice`質問1件だけが対象で、画像・複数質問・文章生成は対象外です。入力の切り捨てや追加の内部分割はしません。
- 公式チャットテンプレート、状態と選択肢の順序、予約トークンのエスケープを維持します。全文を単一microbatchで処理し、full attentionはnoncausal、linear attentionはllama.cppのcausal処理を使います。
- 要求ごとに推論メモリを消去し、`fim_suffix`位置だけのlogitsを読みます。有効な選択肢のtoken IDだけに、GGUFのFLOAT64温度を一度適用します。
- 入力はUTF-8 JSONL、stdoutはJSONL応答だけです。ログはstderrへ出し、親プロセスがモデルID・元チェックポイント・GGUF SHA・llama.cppコミット・量子化・実デバイスを照合します。
- 標準のllama-serverの`/v1/systemone`はこのprotocol v3の代わりに使いません。

## GPUとメモリ

`auto`はNVIDIA CUDAを優先し、Apple SiliconではMetalを選びます。GPUの空き容量から1GiBと作業用512MiBを引き、残りに入る層だけをGPUへ載せます。Windows CUDAではNVMLで専用VRAMも照会し、小さい空き容量を使います。MetalではRAMとGPUを別々の容量として足し合わせません。空きが少ない場合はCPUを使い、実行結果にその状態を記録します。

CUDA/Metalのメモリ不足を検知した場合、Python親が所有するワーカーの終了を確認してから、GPU層数を半減し、最後はCPUで元の要求を再試行します。起動を含めて最大3試行で、元の待ち時間上限を延ばしません。低下した層数は、同じ設定の次の要求にも保持します。タイムアウトやモデル照合エラーはOOMとして再試行しません。

## ビルド

CMake 3.20以降、Git、C++17コンパイラーが必要です。Windows CUDAにはCUDA Toolkit 12.xと対応MSVC、MacにはApple Silicon用Xcode Command Line Toolsを用意します。

```sh
python3 -B tools/build_cpp_decider.py --backend metal
# CPUのみ
python3 -B tools/build_cpp_decider.py --backend cpu
```

WindowsのCUDAビルド例：

```powershell
python -B tools/build_cpp_decider.py --backend cuda --cuda-root "C:/Program Files/NVIDIA GPU Computing Toolkit/CUDA/v12.8"
```

CUDAの既定ビルド対象はSM75/80/86/89/90です。ローカルのRTX 4090専用なら`--cuda-architectures 89`でビルド時間を減らせます。配布用は既定の複数世代を使います。ビルド結果の`BUILD.json`にファイルのSHAと対象を残します。Mac実機のビルド・推論は未検証です。

## 確認の再現

アプリの設定からモデルのメモリを解放し、導入手順に従って2方式を準備してから実行します。設定ファイルは利用者ごとの保存領域にあります。

```sh
python3 -B tools/compare_decider_backends.py --reference-config /path/to/pytorch-config.json --candidate-config /path/to/llama_cpp-config.json --output comparison.json
```

[合成入力](../evaluation/cpp-parity-inputs.json)と[実測結果](../evaluation/cpp-parity-results.json)を公開します。従来CPU BF16との一致を、公式FP32との一致や日本語校正の精度保証として扱いません。

## 通知

独自実装はMITです。Perplexityのprotocol v3を参考にした部分とモデルの条件はApache-2.0です。[Perplexity LICENSE](PERPLEXITY-LICENSE.txt)・[NOTICE](PERPLEXITY-NOTICE.txt)を保持します。llama.cppとnlohmann JSONはMIT、SHA256実装はpublic domainです。ビルドしたワーカーと一緒に各通知を保持します。Windows CUDAのcudart/cuBLAS DLLにはNVIDIA CUDA EULAが適用され、アプリのMITへ置き換えません。
