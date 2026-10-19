# Yomiyasu StudioをCodex / Claude / MCPへ組み込む

WindowsとmacOSで共通のPythonエンジンを使います。主に、ホスト自身のモデルで修正案を作り、MCPで原文との対応とローカル判定を確認します。通常のホスト校正に、GeminiやSarashinaを追加で呼ぶ必要はありません。MCP・スキルと、容量の大きなFlashモデルは別々に導入します。Flashモデルの導入は任意です。

画面では修正を右の文章へ自動で反映します。MCPではホストが候補を見直し、最終稿に反映する案を `finalize` へ渡します。MCPの登録だけで、開いているファイルを自動で書き換えることはありません。

## Codex の既定の導入

Python 3.12以降とCodex CLIを準備し、アプリのフォルダーで実行します。

```powershell
# Windows
python -B install.py
```

```sh
# macOS
python3 -B install.py
```

Windows専用アプリの「このPCのCodexへ登録」からも同じ登録処理を使えます。stdio MCP用の実行ファイルは `YomiyasuStudioCLI.exe` です。GUI用の `YomiyasuStudio.exe` をMCPのコマンドへ指定しないでください。

登録処理は次を行います。

1. Codex設定を利用者領域へバックアップします。
2. 一時領域でCodexの `mcp add` に登録形式を確認し、このアプリの項目だけを既存設定へ加えます。他の設定とコメントを保持します。ソース版では現在のPythonとアプリの絶対パスを使います。
3. `~/.agents/skills/studio-yebisu-japanese-editor/` へ、スキルと参照するyomiyasu原典・通知・アプリ資料をコピーします。別の作業フォルダーからも参照できる構成です。

既存の登録と互換性を保つため、MCP名 `yebisu-japanese-editor` と個人スキル名 `studio-yebisu-japanese-editor` は維持します。ソース内の `skills/japanese-editor/` も同じ名前です。

同名の別サーバーや、利用者が変更した導入済みスキルは上書きしません。他のMCP設定やエージェントの自動編集フックをまとめて変更しません。登録後は新しいCodexセッションで利用します。ソースの場所やPythonを移した場合は再登録してください。

CodexのMCP設定は通常 `~/.codex/config.toml`（`CODEX_HOME`指定時はその設定先）、個人スキルは `~/.agents/skills/` です。本アプリの登録は起動待ち30秒・ツール待ち600秒を指定します。これはFlash初回読込や一件ずつの判定を待つためのホスト設定で、品質や完了時間の保証ではありません。[公式OpenAI MCP資料](https://developers.openai.com/codex/mcp)、[公式スキル資料](https://developers.openai.com/codex/build-skills)

## 別のホストや手動登録

次のコマンドは現在の実体に対応するMCP設定JSONを出力します。設定や認証情報を自動変更しません。

```sh
python -B app.py mcp-config
```

macOSは `python3`、Windowsバイナリは `YomiyasuStudioCLI.exe mcp-config` を使います。出力された `command` / `args` を、利用するMCPホストのstdio設定へ登録してください。アプリ内の場所を例示した架空の絶対パスへ置き換える必要はありません。

Claude Code用の `.claude-plugin/plugin.json` と `skills/japanese-editor/SKILL.md` も同梱します。プラグインの読み込みとMCPの登録は別です。ホストの現在のプラグイン・MCP設定方法に従い、両方を有効にします。Claude Codeからは、一時的なMCP設定で候補レビューと最終稿作成を実行済みです。常時登録や別OSでの実行は、[STATUS.md](STATUS.md) の確認範囲を参照してください。

MCPは必要時に `127.0.0.1:8896` の共有サービスを起動します。サービスの識別とデータ領域が一致する場合だけ共有し、他のアプリを停止したり別ポートへ切り替えたりしません。Flashは初めて判定するときに読み込み、無操作5分で解放します。MCP登録だけでモデルのダウンロードや重い常駐読み込みを行いません。

## ホストからの校正

たとえば「この文章を仕事のメール向けに、意味を変えず最小限で整えて。採用する案を見せて」と依頼します。用途IDは `article`、`blog`、`business`、`social`、`fiction`、`dialogue`。強さIDは `minimal`、`natural`、`structure` です。小説や台詞は一人称・口調・比喩・省略・反復を一律に直す対象にしません。

| ツール | 役割 |
| --- | --- |
| `japanese_editor_profiles` | 用途・強さと接続状態を取得 |
| `japanese_editor_analyze` | 規則を点検。既定で外部生成をしない |
| `japanese_editor_review_proposals` | ホストの案の位置・重複・保護領域を検査し、設定済みならFlash判定 |
| `japanese_editor_finalize` | 採用案から最終稿を組み立て、同じ本文をrun / verify |
| `japanese_editor_check_final` | 別途用意された最終稿の全文点検・本文照合 |
| `japanese_editor_prepare_decider` | 外部実装向けの要求JSONを準備。送信・推論はしない |

ホストが作る修正案の入力例:

```json
{
  "text": "保存して下さい。上限は3件です。",
  "profile": "business",
  "strength": "minimal",
  "edits": [{
    "before": "して下さい",
    "after": "してください",
    "reason": "補助動詞をひらがなにする",
    "rule": "用途と表記の扱い"
  }]
}
```

`before` は原文へ一度だけ現れる完全一致文字列です。重複する場合は前後も含め、範囲を重ねません。原文と各案を比べて採用案を決め、その `before` / `after` / `reason` / `rule` だけを `finalize` へ渡します。エンジンが返す `text` を最終稿とし、点検後にホストが独自に書き直しません。`finalize` はDeciderを再実行しません。

結果の位置はUTF-16の `[start,end)`、`source_hash` は元の本文のSHA-256です。原文が変わった結果を使い回しません。規則の指摘、案を作ったモデルの理由、Deciderの選択は別の情報です。`decision.status=executed` のときだけ判定済みと説明し、未設定・入力上限・実行失敗・時間切れを成功に言い換えません。`scope=excerpt` は前後の抜粋の比較で、全文や採用案の組み合わせ全体の保証ではありません。

`analyze` の `generate=true` は選んだ外部サービスへ文章を送る明示的な機能です。既定はfalseで、通常のホスト校正は `review_proposals` を使います。Gemini は公式 Antigravity CLI（`agy`）の Google ログイン、Sarashina は API キーを使います。Antigravity CLI の導入・ログインは、画面からの生成を使う場合だけ必要です。ホスト自身が案を作る通常の MCP 校正では不要です。[生成サービスと認証](PROVIDERS.md) を参照してください。

## 停止・登録の見直し

Flashだけを解放するには `python -B app.py decider-stop`、共有サービス全体を終了するには `python -B app.py stop-service` を使います。macOSは `python3` に読み替えます。判定中に停止を指示しても、処理の終了を待つ場合があります。画面を閉じても、推論が取り消される保証はありません。

MCP登録を外す場合は、CodexのMCP設定で `yebisu-japanese-editor` だけを無効化または削除します。不要になった個人スキルを取り除く前に、自分で加えた変更を確認してください。既存のCodex設定全体をバックアップで上書きする方法は、後から追加した他の設定を失う可能性があります。Sarashina の API キーの削除はアプリの専用操作または OS の資格情報管理で行い、設定JSONを削除しただけでキーが消えたとは扱いません。Gemini のログイン情報は公式 Antigravity CLI が OS キーリングで管理します。アプリは認証トークンを読んだりコピーしたりしません。旧版で保存した Gemini API キーを、新しい Google ログイン接続で使用・自動削除することはありません。
