# Windows アプリと配布 ZIP

主な組み込み方はWindows / macOS 共通の MCP とスキルです。Windows の専用ウィンドウは同じエンジンを操作する追加の入口です。生成モデルや原文の扱いはブラウザ版と共通で、修正案を作る操作では選択した生成先へ文章を送ります。ルール検査はローカルで動きます。

## Windows で使う

Windows x64 の配布 ZIP を展開し、`YomiyasuStudio.exe` を起動します。インストーラーや管理者権限は必要ありません。GUI と同じフォルダの `YomiyasuStudioCLI.exe` と `_internal/` を一緒に置いてください。exe 一つだけを移すと動きません。モデル、認証キー、ユーザーの原稿は ZIP に含めません。

専用ウィンドウのタイトルは「Yomiyasu Studio」。pywebview の EdgeChromium を使うため、Microsoft の WebView2 Runtime が必要です。未導入なら起動時に案内を表示します。[Microsoft 公式ページ](https://developer.microsoft.com/microsoft-edge/webview2/)から利用者が導入します。アプリは Runtime やモデルを自動取得しません。既存の Microsoft Edge 本体と WebView2 Runtime は別の構成要素です。

アプリは `127.0.0.1:8896` だけを使います。互換性があり、保存先も同じ本アプリが起動済みなら、そのサーバーを共有します。他のアプリや古い版が使っている場合は、相手を終了したり別ポートへ移ったりせず、利用者へ案内します。自分で起動したサーバーとモデルだけを終了時に停止し、共有先は停止しません。処理中なら新しい要求を止め、進行中の生成・判定の完了を待ってから終了します。

共有先のプロセスが予期せず終了した場合は、専用ウィンドウが同じポートでサービスを復旧します。開いている原稿や校正結果は保持します。`stop-service` で明示的に停止した場合は自動復旧せず、再開にはアプリを起動し直します。

ウィンドウの寸法、WebView2 の設定、接続設定はユーザー領域 `%LOCALAPPDATA%\StudioYebisu\JapaneseEditor\` に保存します。配布フォルダへ個人設定を書きません。改名後も旧版の設定を引き継ぐため、保存先は従来のパスを維持します。開いている原稿は自動保存せず、終了時に確認します。原稿を残す場合は、出力のコピーかテキスト保存を使ってください。

MSIX などによる保存先の仮想化がある環境では、起動元によってユーザー領域の実体が異なる場合があります。その環境で GUI と Codex を共有する場合は、両方へ同じ絶対パスの `YEBISU_EDITOR_DATA_DIR` を指定してください。保存先が違うサービスへ原稿を送ることはありません。

Flash を使う場合は [DECIDER_RUNTIME.md](DECIDER_RUNTIME.md) の専用環境が別途必要です。約9.10 GB のモデルは配布 ZIP に含めません。Gemini の生成には公式 **Antigravity CLI 1.4.0 / 1.4.1 / 1.4.4（`agy`）** を別途導入し、Google でログインします。Node.js / npm は不要です。CLI 本体と認証情報は ZIP に含めません。既存の OS キーリング認証を公式 CLI 自身が使い、アプリはトークンを移しません。Sarashina は API キー、Claude / Codex は既存の認証を使います。[生成先の導入と条件](PROVIDERS.md) を参照してください。

CLI はターミナルや MCP ホストから使います。

```powershell
.\YomiyasuStudioCLI.exe profiles
.\YomiyasuStudioCLI.exe gemini-login
.\YomiyasuStudioCLI.exe mcp-config
.\YomiyasuStudioCLI.exe mcp
```

`mcp-config` が返す絶対パスの CLI を登録します。GUI 用 exe を stdio MCP の command にしないでください。保存先を変えた場合は、設定を出力し直します。

## ソースから Windows 版を作る

Windows x64、Python 3.12 以降、`uv` を使います。制作時は Windows 11 / Python 3.13.7、pywebview 6.2.1、PyInstaller 6.22.3 でビルドしました。アプリのディレクトリで実行します。

```powershell
python -B tools/build_windows.py
python -B tools/build_windows.py --smoke-cli --smoke-gui
python -B tools/build_release.py
```

`.local/build/.venv` に固定したビルド依存だけを導入します。ComfyUI や通常の Python 環境は変更しません。出力は `.local/build/dist/YomiyasuStudio/`、配布 ZIP と SHA-256 は `.local/build/releases/` です。ビルド中は、依存パッケージと固定した小さなライセンス原本を取得します。モデルの取得や推論は行いません。

`--smoke-recovery` は8896が空いている場合に限り、検証用に起動した共有サービスの消失、GUIによる復旧、原稿とDOMの保持、明示停止後の復旧抑制を確かめます。既存サービスは終了しません。

`--smoke-cli` は合成文で MCP と全文点検・本文照合、`--smoke-gui` は実際の専用ウィンドウで規則の検査を行い、自分のウィンドウを閉じます。どちらも生成モデルや Flash を呼びません。結果は `.local/build/proofs/` に保存します。

Windows 用の unsigned onedir ビルドです。署名鍵、証明書、インストーラー、管理者昇格は作りません。Windows が発行元未確認の案内を出す場合があります。実行する配布元と `SHA256.json` を確認してください。依存の版と入力ファイルを固定する手順であり、異なる PC 間でのバイナリ完全一致を確認したという意味ではありません。

`build_release.py` は明示したソースファイルとディレクトリだけを収集し、`.local`、モデル、環境、ログ、キャッシュ、秘密鍵を除きます。バイナリ ZIP の中に定義済みの禁止パスやモデル形式があれば作成を拒否します。ソースだけなら `--source-only` を使います。各 ZIP にはファイルの SHA-256 を記録した `RELEASE-MANIFEST.json` を含めます。

## macOS との関係

MCP / スキル / HTTP の共通 Python エンジンを使います。macOS 向けの実機動作、`.app` の作成、署名、公証は未検証です。Windows から macOS 用のバイナリを作れたとは扱いません。

追加の専用ウィンドウをソースから試す場合は、macOS 上で専用環境を作ります。pywebview は macOS の WebKit と PyObjC を使い、WebView2 を必要としません。

```sh
python3 -m venv .local/desktop-venv
.local/desktop-venv/bin/python -m pip install pywebview==6.2.1
.local/desktop-venv/bin/python -B desktop.py
```

この手順は macOS 実機で未実行です。Windows で測った CPU BF16 の Flash 速度・必要メモリを macOS の保証値として使いません。macOS の配布 `.app` を用意する場合は macOS ランナーで別にビルドし、起動、保存、MCP、終了時のプロセス保全を検証する必要があります。署名・公証は配布方針と証明書の準備後に扱います。

macOS のローカル Flash は`vm_stat` のページサイズと重複しない free / inactive / speculative ページから空き RAM の目安を取り、起動ガードを適用します。Windows / Linux と同じ固定コミット上限を取得できるとは扱わず、結果に `commit_check: not_available_on_macos` を残します。この計算はモックで検証した段階です。実際のモデル読み込みと CPU BF16 演算の対応は macOS 上で別途確認が必要です。

## 0.2.2 で確認した範囲

0.2.2 の凍結 CLI で MCP・全文点検・本文照合・CRLF の保持を確認しました。常設先の CLI から Gemini の実生成 → ローカル Flash の意味変化判定 → 採用文の run / verify とハッシュ照合まで実行しました。新しい Codex セッションでも登録先の v0.2.2 を呼び出せています。画面更新後の確認範囲と実測は [STATUS.md](STATUS.md) を参照してください。

## 0.2.1 で確認した範囲

以下は 0.2.1 の実行証跡です。0.2.2 の Antigravity CLI 接続の確認範囲は [STATUS.md](STATUS.md) を参照してください。

2026-10-17、Windows 11 x64 上で GUI / CLI の実行ファイルを作成しました。共通資源を含むフォルダーは約31 MBで、Flash の重みを含みません。凍結 CLI から MCP の初期化・ツール一覧・合成文の候補レビュー・最終稿作成を実行し、yomiyasu の run / verify、本文ハッシュ一致、CRLF の保持を確認しました。

実際の EdgeChromium ウィンドウで「StudioYebisu 日本語エディター」のタイトル、6用途、合成文の規則検査、正常な終了を確認しました。既存の共有サーバーは停止していません。この専用ウィンドウのスモークでは、生成 API と Flash は呼び出していません。0.2.1 では、検証用に起動した共有CLIサービスだけを終了させ、GUI が同じ8896番で復旧し、合成文と開いていたDOMを保持したまま規則検査できることを確認しました。続けて明示的なサービス停止を行い、自動復旧しないことも確認しました。

文章品質の人手評価や macOS 実機の検証は、この結果に含めません。

## 同梱部品と原典

独自コードは MIT。yomiyasu の固定版と Unicode の通知を保持します。バイナリには Python と、実際に含める各部品の通知を `_internal/editor/third_party_licenses/` に保存します。WebView2 Runtime 自体の再配布は行いません。

| 部品 | 条件と確認元 |
| --- | --- |
| pywebview 6.2.1 | BSD-3-Clause。[公式ソース](https://github.com/r0x0r/pywebview)、[Windows の依存](https://pywebview.flowrl.com/guide/installation.html)、[凍結の案内](https://pywebview.flowrl.com/guide/freezing.html) |
| pythonnet / clr-loader | MIT。インストールしたパッケージの LICENSE を保持 |
| PyInstaller 6.22.3 | GPL と実行ファイル配布の例外。[公式ライセンス説明](https://pyinstaller.org/en/stable/license.html)。生成したアプリの依存条件は別に守る |
| Microsoft WebView2 SDK 1.0.3856.49 | pywebview 同梱 DLL の版を確認。[固定版 NuGet](https://www.nuget.org/packages/Microsoft.Web.WebView2/1.0.3856.49)の LICENSE / NOTICE を保持 |
| proxy-tools 0.1.0 | PyPI metadata は MIT、ソース先頭と原典 LICENSE は BSD。両方の通知を保持。[固定した原典 LICENSE](https://github.com/jtushman/proxy_tools/blob/ccd35a569b95bec271a59890780c7821756c548a/LICENSE.txt) |

Microsoft の [WebView2 配布資料](https://learn.microsoft.com/en-us/microsoft-edge/webview2/concepts/distribution)は、Runtime の検出、利用者ごと・PC ごとの導入、Evergreen と Fixed Version の違いを説明しています。この配布 ZIP は利用者の Evergreen Runtime を使い、250 MB を超える Fixed Version Runtime を抱き合わせません。
