---
name: studio-yebisu-editor-setup
description: Yomiyasu StudioのMCPとスキルをローカルのCodexへ登録する。APIの利用先や任意のローカル判定を設定するときにも使う。
---

Yomiyasu Studioの登録にはこのプラグインのルートにある`install.py`を使う。Windowsでは`python install.py`、Macでは`python3 install.py`。Python 3.12以降が必要。`--print-config`は設定を表示するだけ。登録時はこのアプリのMCPだけを追加し、既存設定をこのPC内へバックアップする。

登録後、新しいホストの会話で`japanese_editor_profiles`を呼び出す。ホスト自身が修正案を作る通常の校正では、外部生成サービスのAPIキーは不要。原文を`review_proposals`へ、採用案だけを`finalize`へ渡す。

単独画面の生成サービスはGemini 3.8 Flash、短文用Sarashina、既存Claude Codeから選ぶ。GeminiはGoogle公式Antigravity CLI（agy）のGoogleログインを使い、APIキーは不要。SarashinaのAPIキーだけを画面からOSの資格情報ストアへ保存し、チャットに貼らせない。送信先と利用条件は[生成サービスの案内](../../docs/PROVIDERS.md)で確認する。ローカルFlashの約9GB取得と専用環境は任意で、プラグイン登録時にはダウンロードしない。

WindowsとMacの実機検証範囲は[README](../../README.md)に従い、未確認のOS・API接続を確認済みと扱わない。
