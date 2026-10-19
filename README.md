# Yomiyasu Studio

Yomiyasu Studioは日本語の文章を整え、どこを変えたか左右で見比べるツールです。解説・ブログ・仕事の文章・小説・台詞など、作るものに合わせて用途を切り替えられます。アプリで使うほか、CodexやClaude Codeへの組み込みにも対応します。標準は **Sarashinaで修正案を作り、Yomiyasuの方針・規則とローカルPerplexity Decider Flashの意味判定を組み合わせる構成** です。制作は StudioYebisu です。

## ダウンロード

[最新版と更新内容を見る](https://github.com/studio-yebisu/yomiyasu-studio/releases/latest)

| 使う環境 | v0.2.9のダウンロード | 提供状況 |
| --- | --- | --- |
| Windows x64 | [Windowsアプリ ZIP](https://github.com/studio-yebisu/yomiyasu-studio/releases/download/v0.2.9/YomiyasuStudio-windows-x64-0.2.9.zip) | 専用ウィンドウ版。実推論の確認範囲は下記参照 |
| macOS | [ソース ZIP](https://github.com/studio-yebisu/yomiyasu-studio/releases/download/v0.2.9/YomiyasuStudio-source-0.2.9.zip) | Pythonで起動する実験版。Mac実機は未検証 |

Sarashina APIとローカルDeciderを使う標準構成の実推論をWindowsで確認しています。v0.2.9を含む版ごとの検証内容は [STATUS.md](docs/STATUS.md) を参照してください。

v0.2.9では初回の生成先をSarashinaに変更し、Sarashina生成にローカルDeciderの意味判定を必須にしました。既存のGemini・Claude等の保存設定は保持します。ローカルDeciderの共有通信には認証を使います。**旧版からはアプリと共有サービスの両方を更新し、サービスを起動し直してください。** MCPも新版へ登録し直して、新しい会話で使います。旧サービスの認証方式へ自動で戻すことはありません。[更新と接続の案内](docs/PLUGINS.md#共有サービスの更新)

![小説の校正例。実画面の一部。左の原文に赤い取り消し線、右の校正後に緑の変更箇所を表示。](https://raw.githubusercontent.com/studio-yebisu/yomiyasu-studio/main/docs/assets/editor.png)

## Windowsで始める

1. 上の「Windowsアプリ ZIP」をダウンロードします。
2. ZIPを右クリックし、「**すべて展開**」を選びます。ZIPの中から直接起動しないでください。
3. 展開先の `YomiyasuStudio` フォルダーを開き、`YomiyasuStudio.exe` を起動します。

`YomiyasuStudioCLI.exe` と `_internal` フォルダーは同じ場所に置いたままにします。exeだけを移すと動きません。**アプリの起動にPythonは不要**です。

WebView2が見つからないと表示された場合は、[Microsoft公式のWebView2 Runtime](https://developer.microsoft.com/en-us/microsoft-edge/webview2/#download)から「Evergreen Bootstrapper」を導入し、アプリを起動し直します。

画面でAIに修正案を作ってもらう場合は、下の「標準構成の準備」へ進んでください。まず規則の指摘だけ見たい場合は、上部の2つのチェックを外して「校正する」を押せます。

## Macで始める（実験版）

Mac用の `.app` はまだ用意していません。ブラウザーで使うソース版を用意していますが、Mac実機での起動・認証・保存・モデル推論は未検証です。

1. [Python公式のmacOSダウンロード](https://www.python.org/downloads/macos/)から **Python 3.13.xのmacOS installer** を導入します。Apple Siliconではarm64で動くPythonを使ってください。
2. 上の「ソース ZIP」をダウンロードし、ダブルクリックして展開します。
3. 「ターミナル」を開きます。ダウンロード先が通常の「ダウンロード」フォルダーなら、次を1行ずつ実行します。

```sh
cd "$HOME/Downloads/YomiyasuStudio-source-0.2.9"
python3 --version
python3 -B app.py serve
```

`python3 --version` で3.13.xと表示されることを確認します。別の場所へ展開した場合は、`cd ` と入力してから、`app.py` があるフォルダーをターミナルへドラッグし、Enterを押すと移動できます。

サーバーが起動したら、ブラウザーで **[http://127.0.0.1:8896](http://127.0.0.1:8896)** を開きます。使っている間はターミナルを開いたままにし、終了するときはターミナルで Control + C を押します。

この起動だけなら、追加のPythonパッケージや大きなモデルの取得は不要です。画面から修正案を作る場合は、次の準備を行います。

## 標準構成の準備

1. [Sarashina公式サービス](https://sarashina.ai/)へ登録し、自分のAPIキーを発行します。現在は無料ベータです。日本国内向けの提供条件、回数制限、入出力の利用条件があります。[利用条件の案内](docs/PROVIDERS.md#sarashina-の利用条件)
2. アプリ右上の「設定」で「Sarashina」を選び、モデルを `sarashina3-mini` にしてAPIキーを保存します。キーはWindowsの資格情報マネージャー、Macのキーチェーンで管理します。チャットやGitHubへキーを貼らないでください。
3. [Windows・MacのDecider導入手順](docs/INSTALL.md)に従い、固定したFlashモデル約9.10GBと専用環境を準備します。これは利用者のPCで動き、PerplexityのAPIキーは不要です。CPU BF16・短い文脈の構成では空きRAM約11GiBが必要です。Mac実機は未検証です。
4. 「接続状態を更新」を押し、生成と意味判定の設定を確認します。Sarashinaの校正では生成と意味判定をセットで使います。

生成前にDeciderの設定を確認し、未設定なら原文をSarashinaへ送信しません。生成後に意味判定が失敗した場合は、原文と修正候補を残し、コピー・保存を止めます。原文を変更したり、古い判定を新しい文章へ流用したりしません。

初回の生成先だけをSarashinaにします。更新前から使っている生成先を切り替える場合は、設定で自分で選びます。

## Geminiを使う場合（オプション）

Gemini 3.8 Flash（High）も選べます。Google公式の **Antigravity CLI 1.4.0 / 1.4.1（`agy`）** を使い、Googleアカウントでログインします。Gemini用のAPIキーは不要です。`gemini` コマンドを使う従来のGemini CLIとは別の製品です。

[Google公式の導入手順](https://antigravity.google/docs/getting-started?tab=cli)に沿ってCLIを導入します。公式のインストールコマンドは次のとおりです。

WindowsのPowerShell:

```powershell
irm https://antigravity.google/cli/install.ps1 | iex
agy --version
```

Macのターミナル:

```sh
curl -fsSL https://antigravity.google/cli/install.sh | bash
agy --version
```

このアプリが対応するCLIの版は1.4.0と1.4.1です。 `agy --version` で確認してください。別の版は未対応として扱います。導入後、アプリを起動し直します。Macでは別のターミナルで導入し、アプリ用サーバーを起動し直してください。

1. アプリ右上の「設定」を開き、「サービス」でGeminiを選びます。
2. 「**Googleアカウントでログイン**」を押し、公式CLIとブラウザーの案内に従います。
3. ブラウザーだけでなく、CLI側にもログイン完了が出るまで待ちます。
4. アプリの「接続状態を更新」と「設定を保存」を押します。

Google AI Pro / Ultraを利用する場合は、契約した同じアカウントでログインします。使えるモデルや上限はアカウントと提供状況によります。アプリが全員の利用権や無料利用を保証するものではありません。

Codex自身に修正案を作ってもらう使い方なら、このGemini設定は不要です。Sarashina APIや既存のClaude Codeを選ぶ方法は、[生成サービスの案内](docs/PROVIDERS.md)にあります。SarashinaはAPIキーが必要です。

## 文章を校正する

1. 左側へ文章を貼り、用途と手直しの強さを選びます。残したい口調や表現は、設定の「文体メモ・守りたい表現」に書けます。
2. 上部のチェックで処理を選び、「**校正する**」を押します。
3. 修正は右側へ自動で反映されます。左の赤い取り消し線と右の緑の差分で変更箇所を見比べ、「変更の詳細」で理由を確認します。
4. 結果をコピーするか、別のテキストファイルへ保存します。

| チェック | 行うこと |
| --- | --- |
| AI校正 | 選んだ生成サービスへ原文・文体メモ・Yomiyasuの推敲方針を送り、修正案を作ります。別の生成先を選ぶと表示名も変わります |
| Deciderの意味判定 | 別途準備したローカルDeciderで、原文と各修正案の意味の変化を確認します |

Sarashina校正では意味判定を外せません。GeminiやClaudeを選ぶ場合は、従来どおり意味判定を選べます。両方を外すと、原文を変えずに規則の指摘だけを表示します。修正案がない場合は、意味判定も実行しません。

処理中は進捗バーと経過時間を表示します。意味判定の進捗は判定を終えた件数で表示します。Geminiが本文を返さない場合など、対象となる一時的な失敗は、同じ原文・モデルで最大2回自動再試行します。処理中に押し直す必要はありません。完了しなくても原文は残ります。

入力した原文や元のファイルを自動で上書きしません。保存前にYomiyasuの全文点検と保存本文の照合を行います。意味の変化が疑われる箇所・判断保留・未判定は別に表示します。判定済みでも、意味保持や文章品質の保証にはなりません。長文の意味判定は変更付近の抜粋が対象です。

本文はどの生成先でも12,000字までです。小説や台詞では、比喩・省略・反復・方言・語尾などを残す方針で校正します。

## Codexに組み込む

Codex CLIが使える環境で、MCPと校正スキルをまとめて登録します。画面の「設定」→「このPCのCodexへ登録」からも実行できます。

Windows配布版では、exeがあるフォルダーでPowerShellを開きます。

```powershell
.\YomiyasuStudioCLI.exe install-codex
```

Macのソース版では、上で展開した `app.py` のあるフォルダーで実行します。

```sh
python3 -B install.py
```

既存のCodex設定をこのPC内へバックアップしてから、アプリの設定とスキルを加えます。同名の別設定や利用者が変更したスキルは上書きしません。この登録で約9GBのモデルを取得することはありません。 登録後は新しいCodexの会話を開き、たとえば次のように依頼します。

> studio-yebisu-japanese-editorを使って、次の文章をブログ向けに整えて。意味と口調を残し、変更点と理由も見せて。

既存の登録と設定を引き継ぐため、MCP名 `yebisu-japanese-editor`、個人スキル名 `studio-yebisu-japanese-editor`、設定・モデルの保存先と資格情報名は従来どおりです。製品の表示名とは別の互換用IDとして維持します。

通常はCodex自身が修正案を作るため、追加のGemini設定や生成用APIキーは不要です。準備済みなら、ローカルDeciderも候補の意味判定に使います。[Claude Code・手動MCP登録など](docs/PLUGINS.md)

## ローカルの意味判定を追加する（任意）

Decider Flashは用意した原文と修正案を比較する判断モデルです。使う場合だけ、約 **9.10GBの固定モデル**と専用環境を別途取得します。Pythonと、環境の準備に使う `uv` が必要です。[Python公式](https://www.python.org/downloads/)と [uv公式の導入手順](https://docs.astral.sh/uv/getting-started/installation/)を参照し、Python 3.13とuvを用意してください。

### Windows

`YomiyasuStudio.exe` があるフォルダーでPowerShellを開き、次を実行します。

```powershell
python --version
uv --version
python -B _internal/editor/tools/setup_decider.py --device cpu --cpu-dtype bfloat16 --max-length 2048
```

CPU BF16・文脈2048では、**空きRAMとコミット可能メモリが各11GiB以上**必要です。コミット可能メモリにはWindowsのページファイルも関係します。詳しい条件と保管先は [Deciderの実行条件](docs/DECIDER_RUNTIME.md)にあります。

### Mac・総メモリ24GBのApple Silicon（実験設定）

macOS 14以降、arm64 Python 3.12〜3.14（3.14.1を除く）が対象です。ここでは3.13を推奨します。Intel MacやRosettaのx86_64 Pythonは、このプリセットの対象外です。

ソースを展開した `app.py` のあるフォルダーで実行します。最初のコマンドは点検だけで、取得・インストール・設定変更を行いません。

```sh
uv --version
python3 -B tools/setup_decider.py --preset mac24 --check-only
```

`ready_to_setup` は導入前の条件を満たしたという表示で、推論できたという意味ではありません。条件を確認してモデルを取得する場合だけ、次を実行します。

```sh
python3 -B tools/setup_decider.py --preset mac24
```

総RAM 24GiB以上と、現在の空きRAMの目安11GiB以上を別々に確認します。CPU BF16・文脈2048・8 threadsを使い、MPSは使いません。**Mac実機の導入・推論・速度は未検証**で、すべての24GB搭載Macで動く保証はありません。既存設定は上書きしません。

準備後はアプリの「接続状態を更新」を押し、意味判定をオンにします。判定が不要になったら、設定の「判定モデルのメモリを解放」でメモリを空けられます。無操作が5分続いた場合も自動で解放します。

## 困ったとき

| 状況 | 確認すること |
| --- | --- |
| Windowsアプリが起動しない | ZIPをすべて展開し、exe・CLI・`_internal` を一緒に置きます。WebView2 Runtimeが必要と出た場合はMicrosoft公式から導入します |
| `app.py` が見つからない | ターミナルの場所が違います。`app.py` がある展開先フォルダーへ `cd` で移動します |
| `python` / `python3` が見つからない、版が古い | Pythonを導入してターミナルを開き直します。`--version` で確認し、Macで複数版がある場合は各コマンドの `python3` を `python3.13` に読み替えます |
| GeminiのCLIが見つからない・版が未対応 | 必要なのは `agy` 1.4.0または1.4.1です。`agy --version` を確認し、導入後はアプリも起動し直します |
| Google認証後も使えない | ブラウザーとCLI両方の完了を確認します。ブラウザーが開かなければ、CLIの案内にある認証URLを自分で開いて手順を続けます。その後「接続状態を更新」を押します |
| `127.0.0.1:8896` が開かない・使用中と出る | Macはサーバー用ターミナルを確認します。既に本アプリが起動していればその画面を使います。別アプリや旧版が使用中の場合は表示された案内に従います |
| Deciderの資源不足・未接続 | 空きRAMと導入先を確認します。Sarashina校正は意味判定の準備後に使います。生成をオフにした規則点検は先に使えます。総RAMの容量だけでは起動を判断できません |
| 不正なUnicode文字と表示される | 原文・文体メモ・修正案などの該当箇所を入力し直します。入力を自動で別の文字に置き換えることはありません |
| 共有サービスを認証できない | アプリと共有サービスの両方をv0.2.7以降へ更新し、旧サービスを終了して起動し直します。認証できない共有先へ原文・候補は送りません |

実行確認の範囲は [STATUS.md](docs/STATUS.md)、詳細な導入は [INSTALL.md](docs/INSTALL.md)、Windows固有の案内は [WINDOWS.md](docs/WINDOWS.md)を参照してください。

## 原典・ライセンス

Yomiyasu StudioはStudioYebisuが制作する独立したアプリです。nanaism/yomiyasuの公式製品ではなく、原典著者による承認・品質保証を示しません。

独自コードと文書は StudioYebisuの [MIT License](LICENSE) です。[nanaism/yomiyasu 2.0.0](https://github.com/nanaism/yomiyasu)の推敲方針・参照資料・検査スクリプトを使い、原典のMITとUnicode License V3の通知を無改変で保持しています。生成をオンにすると、その方針の全文と用途別資料を生成モデルへ渡します。

Perplexity Decider Flashのモデルと公式コードはApache-2.0で、元のLICENSE / NOTICEを保持して別途取得します。このアプリのために新しいモデルを学習したものではありません。Gemini・Sarashina・Claudeは各提供元の利用条件に従います。[原典と出典](docs/SOURCES.md) / [第三者の通知](THIRD_PARTY_NOTICES.md) / [開発への参加](CONTRIBUTING.md) / [問題の報告](SECURITY.md)
