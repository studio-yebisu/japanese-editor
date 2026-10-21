# Yomiyasu Studioの導入

Windowsの配布版と、macOSのソース実行では準備が異なります。生成AIの認証は[PROVIDERS.md](PROVIDERS.md)、Codex・Claudeへの組込みは[PLUGINS.md](PLUGINS.md)、確認した範囲は[STATUS.md](STATUS.md)を参照してください。

## Windows

1. AVX2対応CPUのWindows x64が対象です。Windows用ZIPをすべて展開し、`YomiyasuStudio.exe`を起動します。CLIと`_internal`も同じフォルダーに置きます。
2. [Python 3.12以降](https://www.python.org/downloads/)を用意し、展開先でPowerShellを開きます。
3. PPDDの公式Q8モデルを準備します。モデルは約5.16GBです。C++ワーカーとCUDA用ライブラリは配布版に同梱し、PyTorch・uv・CUDA Toolkitは利用者の導入には不要です。

```powershell
python --version
python -B _internal/editor/tools/setup_cpp_decider.py
```

4. アプリの設定で「接続状態を更新」を押します。既存設定がある場合は「PPDDの実行方式」を「C++・公式Q8」にして保存します。初めての導入ではQ8版を使います。
5. SarashinaのAPIキーを設定するか、Geminiの公式CLIへログインして校正します。SarashinaではローカルPPDDの判定が必須です。

### NVIDIA GPUを使う場合

NVIDIAドライバーを用意し、実行先を「GPU優先・メモリに合わせて自動」にします。GPUの空きVRAMを確認して、入る層だけをGPUへ載せ、残りをCPUで処理します。空きが少ないときはCPUで処理します。OOM時は所有するワーカーを終了してGPU層数を減らし、最後にCPUで同じ要求を再試行します。起動を含めて最大3試行です。設定の表示と、修正候補の詳細で実際の実行先を確認できます。

標準のGPU予約は1GiBと作業用512MiBです。空きVRAMがこの合計以下ならCPUを選びます。他のアプリを自動で終了する処理はありません。配布版のCUDAビルド対象はSM75/80/86/89/90です。古いGPUはCPUを選んでください。

### 必要な空き容量

| 項目 | C++・公式Q8版の目安 |
| --- | --- |
| RAM | 空き8GiB以上。総搭載量とは別の条件 |
| Windowsのコミット余裕 | 文脈2048では3GiB以上、4096では5GiB以上 |
| VRAM | GPUは任意。空き容量に応じてCPUを併用 |
| ディスク | 初回取得前の空き12GiB以上。約5.16GBのモデルと取得中の分割ファイルが共存 |
| 入力 | 文書12,000字まで。生成は段落等で分割し、判定要求は文脈2048トークンまで |

これらは起動ガードと導入予算です。全入力での推論成功や速度を保証する値ではありません。モデルのサイズとSHAを照合し、初回推論の前にも全文のSHAを確認します。準備だけでは推論を実行しません。

## macOS・Apple Silicon（実験段階）

Mac用の署名・公証済み`.app`はまだありません。Apple Silicon用のPython 3.12以降、Git、CMake 3.20以降、C++17コンパイラーを用意します。Xcode Command Line Toolsは`xcode-select --install`で導入できます。CMakeは[公式配布](https://cmake.org/download/)や既存のパッケージ管理環境を使ってください。

Metalのコンパイラーも必要です。`xcrun -sdk macosx metal --version`で見つからない場合は、Xcodeを導入・選択したうえでMetal Toolchainを追加します。対応するXcodeでは`xcodebuild -downloadComponent MetalToolchain`を使えます。[Apple公式の追加コンポーネント案内](https://developer.apple.com/documentation/Xcode/downloading-and-installing-additional-xcode-components)

ソースZIPを展開し、`app.py`があるフォルダーで次を実行します。

```sh
python3 --version
cmake --version
git --version
python3 -B tools/setup_cpp_decider.py --build
python3 -B app.py serve
```

最初の準備で固定版llama.cppを取得してMetalワーカーをビルドし、公式Q8を取得します。ブラウザーで[http://127.0.0.1:8896](http://127.0.0.1:8896)を開いて設定します。総メモリ24GB程度のApple Silicon Macを対象にしていますが、空きRAMは別途8GiB以上必要です。GPUとRAMは共有のメモリなので別容量として足し合わせません。

**Mac実機でのビルド・推論・速度は未検証**です。Metalを使うコードとビルド経路を用意した段階です。メモリ不足時のCPUへの切り替えもMac実機では未検証です。

## 従来版からの切り替え・復帰

準備コマンドは既存の`config.json`を上書きしません。Q8の準備結果を`llama_cpp-config.json`に保存します。アプリの「PPDDの実行方式」から保存すると、旧方式を`pytorch-config.json`へ残して切り替えます。準備済みの旧方式へも同じ画面から戻せます。校正中の切り替えは拒否し、2方式のモデルを同時には読み込みません。

従来版のモデル約9.10GB、専用PyTorch・uv、CPU BF16・CUDAの条件は[DECIDER_RUNTIME.md](DECIDER_RUNTIME.md)に残しています。C++ Q8と従来BF16は異なる精度であり、すべての入力で同じ判定になるとは限りません。[同一入力の比較結果](../evaluation/cpp-parity-results.json)も参照してください。
