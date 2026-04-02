[![CI](https://github.com/yukimurata0421/amazon-notify/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/yukimurata0421/amazon-notify/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.11%20%7C%203.12-blue)

# Amazon Notify v2

Gmail API を利用して Amazon.co.jp からの配送関連メールを検出し、Discord Webhook に通知する自己ホスト向けツールです。`state.json` による重複防止、認証異常時の警告、一時的な通信障害からの復旧通知に対応しています。

これは個人運用を前提にしたサンプル兼実用ツールです。Amazon、Google、Discord の公式製品ではありません。

Amazon の注文・発送系メールを見逃しにくくするために、Gmail を起点に軽量な個人通知基盤として設計しています。Amazon メールの抽出、重複防止用の state 管理、認証異常と復旧通知、systemd 常駐運用までを最小構成でまとめています。

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
- `amazon_notify/`: 本体パッケージ
- `pyproject.toml`: パッケージ定義と CLI エントリポイント
- `config.example.json`: 設定例
- `tests/unit`: ユニットテスト
- `tests/e2e`: E2E シナリオテスト
- `.github/workflows/ci.yml`: GitHub Actions CI
- `deployment/systemd/amazon-notify.service`: systemd サンプル
- `docs/OPERATIONS.md`: 運用メモ

## クイックスタート
1. 仮想環境を作成して有効化します。

```bash
python3 -m venv .venv
source .venv/bin/activate
```

2. パッケージをインストールします。

```bash
pip install .
```

3. 設定ファイルを作成します。

```bash
cp config.example.json config.json
```

4. `config.json` の `discord_webhook_url` などを環境に合わせて編集します。`config.json`、`credentials.json`、`token.json`、`logs/` は既定でこのファイルと同じディレクトリを基準に扱います。
5. `credentials.json` を同じディレクトリに配置します。
6. 初回認証または再認証を行います。

```bash
amazon-notify --reauth
```

7. 単発実行で疎通確認します。

```bash
amazon-notify --once
```

## 実行
インストール後は `amazon-notify` コマンドで実行できます。モジュールとして起動したい場合は `python -m amazon_notify.cli` でも同じです。別の場所に設定ファイルを置く場合は `--config` を使います。

常駐監視:

```bash
amazon-notify
```

監視間隔を上書き:

```bash
amazon-notify --interval 120
```

ログ保存先を上書き:

```bash
amazon-notify --log-file /var/log/amazon-notify/notifier.log
```

設定ファイルを明示:

```bash
amazon-notify --config /opt/amazon-notify-v2/config.json
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
`amazon_subject_pattern` は Python 正規表現として評価されます。不正な正規表現が設定されている場合は、起動時にエラーを表示して終了します。
`state_file` と `log_file` の相対パスは `config.json` のあるディレクトリ基準で解決されます。`state_file` はネストしたパスでも指定でき、保存先ディレクトリが未作成でも自動作成します。

## 通知例

```text
📦 Amazon 配達関連メールを検出しました

件名: 商品を発送しました
From: shipment-tracking@amazon.co.jp
プレビュー: ご注文の商品を発送しました。お届け予定日をご確認ください。
https://mail.google.com/mail/u/0/#inbox/18c0123456789abc
```

## 制約
- 境界管理は `last_message_id` と `max_messages` ベースです。短時間に大量のメールが流れる高トラフィック環境には向いていません。
- 個人運用向けの軽量ツールであり、Gmail History API を使った厳密な差分追跡は行っていません。

## 開発環境
開発用の依存込みで入れる場合は次を実行します。

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .[dev]
```

Makefile を使う場合は `make setup`、開発用途なら `make setup-dev` を使えます。

## 障害時の挙動
- `token.json` がない場合、自動で OAuth を起動せず警告だけ送ります。
- `token.json` が壊れている、または更新できない場合は警告を送り、`amazon-notify --reauth` による再認証を促します。
- DNS、タイムアウト、証明書不整合などの一時障害ではその周期をスキップし、次周期で復旧を試みます。
- Discord 通知に失敗したメールは `state.json` を進めず、次周期で再試行します。

## テスト
開発依存をインストールしてから実行します。

```bash
pip install -e .[dev]
pytest -q
```

## Makefile
- `make setup`: 実行依存のセットアップ
- `make setup-dev`: 開発依存のセットアップ
- `make test`: テスト
- `make lint`: 構文チェック
- `make clean`: `__pycache__`、`.pyc`、`.pytest_cache` の削除
- `make dist`: 実行に必要なファイルだけを含む配布 zip (`dist/amazon-notify-v2.zip`) の作成

## CI
GitHub Actions では以下を実行します。

- Python 3.11 / 3.12
- `pip install -e .[dev]`
- `python -m compileall -q amazon_notify`
- `amazon-notify --help`
- `pytest -q`

## systemd
`deployment/systemd/amazon-notify.service` を環境に合わせて修正して利用してください。詳細は `docs/OPERATIONS.md` を参照してください。

## セキュリティ
次のローカルファイルはコミットしないでください。

- `credentials.json`
- `token.json`
- `config.json`
- `state.json`
- `logs/`

`.gitignore` で除外しています。
