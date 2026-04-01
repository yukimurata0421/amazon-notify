# Amazon Notify v2

Gmail API を利用して Amazon.co.jp からの配送関連メールを検出し、Discord Webhook に通知する自己ホスト向けツールです。`state.json` による重複防止、認証異常時の警告、一時的な通信障害からの復旧通知に対応しています。

これは個人運用を前提にしたサンプル兼実用ツールです。Amazon、Google、Discord の公式製品ではありません。

## 主な機能
- Gmail 受信トレイをポーリングして新着メールを確認
- 差出人アドレスと件名パターンで Amazon 配送関連メールを抽出
- `state.json` で処理済み境界を保持し、重複通知を防止
- `token.json` 不在、破損、更新失敗時に警告を通知
- 一時的な通信障害の検知と復旧通知
- ローテーションファイルログ（既定: `logs/amazon_mail_notifier.log`）
- `pytest` と GitHub Actions CI を同梱

## 前提条件
- Python 3.11 以上
- Google Cloud で Gmail API を有効化していること
- OAuth クライアントの `credentials.json` を取得済みであること
- Discord Webhook URL を用意していること

## ディレクトリ構成
- `amazon_mail_notifier.py`: 本体
- `config.example.json`: 設定例
- `tests/unit`: ユニットテスト
- `tests/e2e`: E2E シナリオテスト
- `.github/workflows/ci.yml`: GitHub Actions CI
- `deployment/systemd/amazon_mail_notifier.service`: systemd サンプル
- `docs/OPERATIONS.md`: 運用メモ

## セットアップ
1. 仮想環境を作成して有効化します。

```bash
python3 -m venv .venv
source .venv/bin/activate
```

2. 実行依存をインストールします。

```bash
pip install -r requirements.txt
```

3. 設定ファイルを作成します。

```bash
cp config.example.json config.json
```

4. `config.json` の `discord_webhook_url` などを環境に合わせて編集します。
5. `credentials.json` を配置します。
6. 初回認証または再認証を行います。

```bash
python amazon_mail_notifier.py --reauth
```

7. 単発実行で疎通確認します。

```bash
python amazon_mail_notifier.py --once
```

## 実行
常駐監視:

```bash
python amazon_mail_notifier.py
```

監視間隔を上書き:

```bash
python amazon_mail_notifier.py --interval 120
```

ログ保存先を上書き:

```bash
python amazon_mail_notifier.py --log-file /var/log/amazon-notify/notifier.log
```

## 設定
`config.example.json` には次の項目があります。

- `discord_webhook_url`: Discord Webhook URL
- `amazon_from_pattern`: 差出人アドレスに対する正規表現
- `amazon_subject_pattern`: 件名に対する正規表現
- `max_messages`: 1 回のポーリングで確認する最大件数
- `poll_interval_seconds`: 常駐時の監視間隔
- `state_file`: 状態ファイルの保存先
- `log_file`: ログファイルの保存先

`max_messages` は 1 監視周期の間に受信しうるメール件数より十分大きくしてください。短時間に大量のメールが流れる運用では、この値が小さいと古い未処理メールを拾いきれません。

## 障害時の挙動
- `token.json` がない場合、自動で OAuth を起動せず警告だけ送ります。
- `token.json` が壊れている、または更新できない場合は警告を送り、`--reauth` による再認証を促します。
- DNS、タイムアウト、証明書不整合などの一時障害ではその周期をスキップし、次周期で復旧を試みます。
- Discord 通知に失敗したメールは `state.json` を進めず、次周期で再試行します。

## テスト
開発依存をインストールして実行します。

```bash
pip install -r requirements-dev.txt
pytest -q
```

## Makefile
- `make setup`: 実行依存のセットアップ
- `make setup-dev`: 開発依存のセットアップ
- `make test`: テスト
- `make lint`: 構文チェック
- `make clean`: `__pycache__`、`.pyc`、`.pytest_cache` の削除
- `make dist`: 配布 zip (`dist/amazon-notify-v2.zip`) の作成

## CI
GitHub Actions では以下を実行します。

- Python 3.11 / 3.12
- `pip install -r requirements-dev.txt`
- `python -m py_compile amazon_mail_notifier.py`
- `pytest -q`

## systemd
`deployment/systemd/amazon_mail_notifier.service` を環境に合わせて修正して利用してください。詳細は `docs/OPERATIONS.md` を参照してください。

## セキュリティ
次のローカルファイルはコミットしないでください。

- `credentials.json`
- `token.json`
- `config.json`
- `state.json`
- `logs/`

`.gitignore` で除外しています。
