# 生成サービス

選んだ生成先へ原文・文体メモ・Yomiyasuの推敲方針・出力形式を送り、修正案を受け取ります。画面では変更を右の文章へ自動で反映し、左に原文を残します。変更理由や意味判定を確認してからコピー・保存できます。規則のチェックとローカル Decider の判定は、生成とは別の処理です。

初回の既定は **Sarashina / `sarashina3-mini`** です。利用者自身のAPIキーを設定し、Yomiyasuの方針で修正案を作り、ローカルDeciderで意味を比較します。Sarashinaで生成する場合は意味判定が必須です。既存の保存設定は保持し、GeminiやClaudeも選べます。障害時に別のサービスへ自動で切り替えません。

| 選択肢 | 接続と認証 | 原文のアプリ内上限 |
| --- | --- | --- |
| Sarashina（初回既定） | Sarashina API / `sarashina3-mini`、`sarashina3-nano`。APIキーを設定し、ローカルDeciderの意味判定を併用 | 全体12,000字、1回1,200字以内へ自動分割 |
| Gemini 3.8 Flash（High） | Antigravity CLI 1.4.0 / 1.4.1 / 1.4.4、`gemini-3.8-flash-high`。CLI の Google ログイン | 12,000字 |
| Claude（既存 CLI） | 既存 Claude Code / `claude-opus-5-5`。既存 CLI の認証を利用 | 12,000字 |

「字」はPythonのUnicode文字数です。アプリの上限と、公式の128kトークンという文脈長は別です。Sarashina向けの分割は段落・改行・文末をコードで探し、AIを使わずに行います。引用やコード、URL、絵文字等の途中を避け、安全な境界がない場合は送信前に止めます。全体の原文を切り詰めたり改行を作り直したりしません。アプリの出力上限は各回4,000トークンで、途中の応答は採用しません。出力上限・新たな引用符の不整合・600字を超える修正範囲や短い引用への極端な書き足しが発生した部分は、安全な境界で2段階まで再分割します。原稿全体で最大24リクエスト・生成600秒の予算とし、送信間隔の待機も含めます。認証・429・モデル名の不一致・不正なJSON・通信障害は自動で再送しません。分割ごとの文脈になるため、離れた段落どうしの関係を十分に扱えない場合があります。

## Antigravity CLI を導入する

画面上部の「校正AI」で切り替えます。接続が設定済みなら保存操作は不要です。未設定なら設定を開き、保存せず閉じると元のAIへ戻ります。切り替えでは原文やキーを送信しません。原文は保持し、保存が成功した後に前の校正結果を取り消します。

このアプリが対象とする版は **1.4.0 / 1.4.1 / 1.4.4** です。この3版だけを許可し、将来の版を自動で互換と扱いません。Google 公式のネイティブ CLI を別途導入します。Node.js や npm は不要です。既に `agy` を使っている場合は、まず版を確認してください。[公式ダウンロード](https://antigravity.google/download/)、[公式クイックスタート](https://antigravity.google/docs/getting-started?tab=cli)

1.4.4では、[CLIのカスタムエージェント](https://antigravity.google/docs/cli/commands/agents/)に校正の役割とYomiyasuの全文資料を渡します。一時フォルダー内の指示で、利用者の既存設定は書き換えません。ツール操作の記録がある結果は保留し、JSON全体をアプリのスキーマで検証してから修正案として扱います。Markdownや説明文からJSONを取り出したり、壊れた応答を補修したりしません。

Windows（PowerShell）:

```powershell
irm https://antigravity.google/cli/install.ps1 | iex
agy --version
```

macOS（ターミナル）:

```sh
curl -fsSL https://antigravity.google/cli/install.sh | bash
agy --version
```

これらは利用者が実行する公式インストール手順です。アプリは CLI を自動取得・更新しません。導入で PATH が変わった場合は、アプリを起動し直してください。現在のアダプターは **1.4.0 / 1.4.1 / 1.4.4** を対象とし、それ以外の版は未対応として表示します。生成開始時に実行ファイルと版を確認し、同じ呼び出しの再試行でもその実行対象を使います。macOS 実機でのアプリ連携は未検証です。

## Google でログインする

1. 通常のターミナルで `agy` を起動するか、アプリの「Googleアカウントでログイン」を押します。
2. 初回は公式 CLI とブラウザーの案内に従い、利用する Google アカウントを選びます。Google AI Pro / Ultra を使う場合は、契約した同じアカウントでログインします。
3. CLI の `/model` と `/usage` で、選択アカウントのモデルと利用枠を確認します。
4. アプリで「接続状態を更新」を押し、Gemini を選んで修正案を作ります。

既存の Antigravity ログインが有効なら、CLI が OS の安全なキーリングから認証し、再ログインなしで使えます。Windows は資格情報マネージャー、macOS はキーチェーンなどを公式 CLI 自身が扱います。アプリはトークンを読んだりコピーしたりせず、旧 Gemini CLI の OAuth ファイルも流用しません。[公式認証手順](https://antigravity.google/docs/install?tab=cli)、[公式移行案内](https://antigravity.google/docs/cli/gcli-migration/)

ターミナルからアプリのログイン起動を使う場合は、ソース版で `python -B app.py gemini-login`（macOS は `python3`）、Windows 配布版で `.\YomiyasuStudioCLI.exe gemini-login` を実行します。この操作は公式 `agy` の対話画面を開きます。アプリへ Google のパスワードを入力する必要はありません。

接続状態の表示では対応 CLI の検出と、実際の認証・モデル利用の結果を分けます。CLI を検出しただけでは、そのアカウントの認証や利用権を確認したことにはなりません。認証・利用枠・モデルへのアクセスは生成時に確認します。

## Google の利用枠・入力の扱い

モデルと上限は Google AI プランや提供状況によります。AI Pro / Ultra の加入で上限が拡大しますが、全モデルの利用や特定の課金先をアプリが保証するものではありません。既存プランの利用枠を消費し、残量・混雑・提供地域などの条件が適用されます。[公式プラン](https://antigravity.google/docs/plans/)

アプリの生成用設定は一時フォルダーへ隔離し、ツールを拒否する設定と **`useG1Credits: false`** を指定します。利用枠超過時に AI クレジットを追加消費する設定へ切り替えたり、API キー・別モデルへ代替送信したりしません。普段使う `agy` の設定を書き換えません。[設定項目](https://antigravity.google/docs/cli/reference)、[公式 headless 実行](https://antigravity.google/docs/cli/headless/)

入力・応答・利用情報には Google のサービス条件とプライバシー条件が適用されます。利用情報の保存や製品改善への利用、利用者の設定については公式条件を確認してください。本アプリの配布条件とは別に、提供元の利用条件が適用されます。[Antigravity 利用条件](https://antigravity.google/terms/)

## Sarashina の API キー

[公式APIキー画面](https://chat.sarashina.ai/dashboard/api-keys)へGoogleアカウントで登録・ログインし、「API Keys」で新しいキーを作成します。コピーしてアプリの設定へ保存します。設定済みなら取得し直す必要はありません。PerplexityのAPIキーは使いません。保存キーを優先し、未保存なら `SARASHINA_API_KEY` を使います。[公式の取得手順](https://docs.sarashina.ai/introduction#1-api-キーの取得)

- Windows は資格情報マネージャーの Generic Credential、対象名 `StudioYebisu/JapaneseEditor/generator/sarashina` を使います。
- macOS はシステムの Security.framework の Generic Password API、サービス名 `StudioYebisu/JapaneseEditor/generator`、アカウント名 `sarashina` を使います。実機での保存・許可ダイアログ・読み出しは未検証です。
- `providers.json` に保存するのは `version`、`provider`、`model` だけです。キーを設定JSON・生成結果・公開設定へ含めません。
- 保存キーへのアクセス拒否や破損はエラーとし、環境変数へ切り替えません。キーの削除はアプリ固有の項目に限り、環境変数は削除しません。
- macOS のシステムフレームワークが使えない環境や、その他のOSでは環境変数を使えます。平文ファイルへの代替保存は行いません。

改名後も既存のキーを使えるよう、資格情報名と `providers.json` の保存先は従来どおりです。

`public_config().secret_store` は `available`、`type`、`notice` を返します。`type` は `windows_credential_manager`、`macos_keychain`、`environment_only` のいずれかです。使用する SecKeychain API は Apple が非推奨としています。動作しない環境ではエラーを表示し、環境変数を案内します。[Apple の宣言](https://github.com/apple-oss-distributions/Security/blob/main/OSX/libsecurity_keychain/lib/SecKeychain.h)、[更新・解放・削除の宣言](https://github.com/apple-oss-distributions/Security/blob/main/OSX/libsecurity_keychain/lib/SecKeychainItem.h)

## Sarashina の利用条件

2026年10月13日から無料のパブリックベータとして案内されています。無料期間の終了日は未定です。公開サービスの利用規約は日本在住者・日本に本店を置く法人を対象とし、第11条で入力・出力の学習やサービス開発等への利用を定めています。有償プランのオプトアウトは将来のデータが対象で、過去分へ遡りません。企業向け Cloud PF Type A の条件を、公開APIへ置き換えて案内しません。[SB Intuitions の発表](https://www.sbintuitions.co.jp/news/press/20261013_01/)、[Sarashina 利用規約](https://sarashina.ai/terms)

固定のHTTPS `https://api.sarashina.ai/v1/chat/completions`、Bearerヘッダー、`response_format.json_schema`、`max_completion_tokens: 8000` を使います。Sarashinaは分割したまとまりごとに送信し、同じプロバイダーとモデル、Yomiyasuの方針を使います。アプリ内の送信開始間隔を10秒以上空け、待機中も何番目を処理しているか示します。ほかのアプリが使った分の利用枠までは把握できません。

各回は送信1,000,000バイト・受信2,000,000バイト・接続待ち180秒までです。途中のまとまりが失敗したら、その操作全体の結果を保留し、一部だけを本文へ反映しません。リダイレクト・環境変数由来プロキシ・自動再試行・別サービスへの代替送信は行いません。429は時間をおいて手動で再試行します。公式上限はmini 6 RPM、nano 60 RPMです。[モデルと接続](https://docs.sarashina.ai/)、[Chat Completions](https://docs.sarashina.ai/api-reference/chat-completions)、[レート制限](https://docs.sarashina.ai/rate-limits)

処理中に原文を編集すると、その操作の結果表示を中止します。受理済みの生成や判定、分割処理は続く場合があります。再度「校正する」を押した場合は、先頭から送り直します。途中からの再開や、成功した部分だけの再利用は行いません。

## 生成結果の確認

要求したプロバイダー・モデルと、応答から確認できたモデル名を分けて記録します。Antigravity は CLI の stream 初期イベントが報告したモデル名を照合し、`identity_basis: cli_stream_init_model` を残します。要求と違うモデル、途中で切れた出力、JSON形式の不一致は採用しません。モデル名の記録は CLI / API の報告で、重みや内部チェックポイントの実証ではありません。原文の位置、重複、コード・URLなどの保護領域、意味の比較はエンジン側で確認します。

Antigravity の通信や再試行は公式 CLI 自体の実装にも依存します。アプリは別サービスや API キー経由へ代替送信しません。成功した最終イベントの `structured_output`、または公式仕様の `response` 全体にあるJSONを、同じスキーマで検証します。本文からのJSONの抜き出しや壊れた形式の補修は行いません。[公式headless仕様](https://antigravity.google/docs/cli/headless/)

CLI の診断全文には原文や認証情報が含まれる可能性があるため、公開応答へそのまま含めません。

## 確認済みの範囲

2026年10月19日にSarashinaの公式資料を無料専用treg経由で再確認しました。Antigravity CLI 接続の実装と、実機でのログイン・生成の確認状況は [STATUS.md](STATUS.md) と [verification.json](../verification.json) を参照してください。以前の Gemini API アダプターのモック検査を、新しい CLI 接続の実生成の証拠へ置き換えません。

Windowsでは保存済みキーを使い、実際のSarashina APIから修正案を生成し、ローカルFlash判定と最終稿のrun / verifyを実行しました。個別の実測は [STATUS.md](STATUS.md) に記録します。生成モデル名はAPIの報告で、内部のチェックポイントを証明するものではありません。Gemini の生成は公式 `agy` を使います。ブラウザーのトークンを取り出す処理はありません。
