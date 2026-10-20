# 第三者のコード・モデル・通知

Yomiyasu Studioの独自コードと独自ドキュメントは [MIT License](LICENSE)、Copyright (c) 2026 StudioYebisu です。以下の外部部品の著作権・ライセンスを、このMITへ置き換えません。

Yomiyasu StudioはStudioYebisuが制作する独立したアプリです。nanaism/yomiyasuの公式製品ではなく、原典著者による承認・品質保証を示しません。

## 同梱する yomiyasu

- 著者: **nanaism**、Copyright (c) 2026 nanaism。
- 原典: [nanaism/yomiyasu](https://github.com/nanaism/yomiyasu)。同梱版2.1.0、コミット `8be3ef6e0e668f14cae30c3da6b0c20b95b2179f`。
- ライセンス: [同梱MIT](vendor/yomiyasu/LICENSE) と [Unicode License V3 の通知](vendor/yomiyasu/UNICODE-LICENSE.txt)。Unicode通知は Copyright © 1991–2026 Unicode, Inc.。
- `vendor/yomiyasu/` のスキル・参照資料・検査スクリプトは原典のファイルを無改変で保持します。来歴、各ファイルの原典URL、ハッシュは [UPSTREAM.json](vendor/yomiyasu/UPSTREAM.json) に記録します。
- アプリの用途・画面・生成プロバイダー・MCPとの接続はStudioYebisu側の追加です。原典著者による承認・品質保証を示しません。

## 任意で取得する Decider

ローカルの Flash は [perplexity-ai/pplx-decider-flash-v1.2](https://huggingface.co/perplexity-ai/pplx-decider-flash-v1.2) の固定コミット `6d771ad7b5ecff5e93b8fc7b90d4c5ccc341954d` を利用します。モデルと公式コードの条件は [Apache-2.0 LICENSE](https://huggingface.co/perplexity-ai/pplx-decider-flash-v1.2/blob/6d771ad7b5ecff5e93b8fc7b90d4c5ccc341954d/LICENSE) と [NOTICE](https://huggingface.co/perplexity-ai/pplx-decider-flash-v1.2/blob/6d771ad7b5ecff5e93b8fc7b90d4c5ccc341954d/NOTICE) です。

公式コード・重みは通常のアプリ配布へ含めず、明示的な別セットアップで取得し、取得元のLICENSE / NOTICEを保持します。依存するPyTorch、Transformers等は、その専用環境へ導入した版の条件が適用されます。CPU BF16での読み込み精度の変更は、結果の `precision_override` に記録します。公式CPU FP32との一致は実証していません。大型27B版やPerplexity hosted APIの利用権・料金は、この配布のMITに含みません。

## 任意の Windows バイナリ

ソース版の MCP・規則のチェック・外部サービスの呼び出し部分は Python 標準ライブラリで動きます。Antigravity CLI や Claude Code 本体は、利用者が別途導入します。Windows専用ウィンドウのバイナリには、実際に同梱した部品の通知を `_internal/editor/third_party_licenses/` に保持します。正確な版とファイルは、その配布物の通知・マニフェストを参照してください。

| 部品 | 条件・通知 |
| --- | --- |
| Python | PSF LicenseおよびPython配布物の第三者通知。`PYTHON-LICENSE.txt` を保持 |
| pywebview 6.2.1 | BSD-3-Clause。原典LICENSEを保持 |
| pythonnet / clr-loader | MIT。各パッケージのLICENSEを保持 |
| PyInstaller 6.22.3 | GPLと実行ファイル配布の例外。同梱部品の条件は個別に保持 |
| Microsoft WebView2 SDK 1.0.3856.49 | 同梱DLLに対応するSDKのLICENSE / NOTICEを保持 |
| proxy-tools 0.1.0 | パッケージメタデータのMIT表記と、原典のBSD通知を両方保持 |

Microsoft WebView2 Runtime本体を抱き合わせて再配布しません。Windows版は利用者のEvergreen Runtimeを使います。[Windowsビルドと出典](docs/WINDOWS.md)、[PyInstallerの公式説明](https://pyinstaller.org/en/stable/license.html)

macOSのSecurity.framework / CoreFoundationはOS提供のシステムライブラリを呼び出し、ライブラリ本体をこの配布へ複製しません。macOS用の専用`.app`バイナリは未作成です。

## 外部の生成サービス

Gemini / Sarashina / Claude は利用者が明示的に選ぶ外部サービスです。Gemini は Google 公式の **Antigravity CLI 1.4.0 / 1.4.1（`agy`）** を通じて利用します。CLI は別途導入するネイティブ製品で、その本体や OS キーリングの認証情報を配布 ZIP に含めません。アプリはトークンの読み取り・コピーや、旧 Gemini CLI の OAuth 再利用を行いません。

Antigravity CLI と Google のモデル・サービスには、提供元の [利用条件](https://antigravity.google/terms/) が適用されます。MIT は StudioYebisu の独自ソースに適用し、外部製品には提供元の条件が適用されます。[公式 CLI の概要](https://antigravity.google/docs/overview?tab=cli)、[導入・認証](https://antigravity.google/docs/install?tab=cli)

Sarashina は API キー、Claude は既存 CLI の認証を使います。データ利用・利用枠・料金は各サービスの条件に従います。[PROVIDERS.md](docs/PROVIDERS.md) に一次資料と実行確認の範囲を記載します。

`evaluation/` の日本語合成例はStudioYebisu側のオリジナルです。制作者の期待ラベルを、人が確定した正解データや原典提供のベンチマークとして扱いません。
