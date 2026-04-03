[![CI](https://github.com/yukimurata0421/amazon-notify/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/yukimurata0421/amazon-notify/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.11%20%7C%203.12-blue)
[![Coverage](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/yukimurata0421/amazon-notify/main/.github/badges/coverage.json)](https://github.com/yukimurata0421/amazon-notify/blob/main/.github/badges/coverage.json)
[![Lint](https://img.shields.io/badge/lint-ruff-brightgreen?style=flat-square)](https://github.com/yukimurata0421/amazon-notify/actions/workflows/ci.yml)
[![Types](https://img.shields.io/badge/types-mypy-brightgreen?style=flat-square)](https://github.com/yukimurata0421/amazon-notify/actions/workflows/ci.yml)

# Amazon Notify

Gmail API を利用して Amazon.co.jp からの配送関連メールを検出し、Discord Webhook に通知する自己ホスト向けツールです。`state.json` の checkpoint 管理、認証異常時の警告、一時的な通信障害からの復旧通知に対応しています。

これは個人運用を前提にしたサンプル兼実用ツールです。Amazon、Google、Discord の公式製品ではありません。

## 解こうとした問題
このツールを作った出発点は、Amazon の配達完了確認が不便だったことです。  
置き配が前提になった今でも、配達されたかどうかを知るには Amazon アプリを何度も開いて確認する必要がありました。

しかし、配送完了そのものはすでにメールとして外部化されています。  
つまり、確認のボトルネックは「情報がないこと」ではなく、「その情報を人間が取りに行かされていること」です。

ならば、Amazon アプリを人が監視するのではなく、配送完了メールをシステムが検知すればよい。  
`amazon-notify` はその発想から作った、Gmail 起点・Discord 通知型の自己ホスト配送通知システムです。

つまり、解こうとしたのは「配送完了を知ること」ではなく、「既に存在する配送完了イベントを、人間が手動で確認しに行く非効率」でした。

## 主な機能
- Gmail 受信トレイをポーリングして新着メールを確認
- 差出人アドレスと件名パターンで Amazon 配送関連メールを抽出
- `state.json` を checkpoint として扱い、通知成功時だけ前進
- `events.jsonl` / `runs.jsonl` に実行結果を構造化保存
- `token.json` 不在、破損、更新失敗時に警告を通知
- 一時的な通信障害の検知と復旧通知
- ローテーションファイルログ（既定: `logs/amazon_mail_notifier.log`）
- `--dry-run`、`--test-discord`、`--validate-config`、`--health-check` の運用コマンド
- `pytest`、`ruff`、`mypy` と GitHub Actions CI を同梱

## v0.2.0 移行と正本
- 正本（source of truth）は `events.jsonl` の `checkpoint_advanced` イベントです。
- `state.json` は互換スナップショット（派生物）として維持します。
- `runs.jsonl` は監査・可観測性用の派生ログです。
- 初回移行時:
  - `events.jsonl` が空で `state.json` に `last_message_id` がある場合のみ、1回だけ bootstrap イベントを自動記録します。
  - 以後は pipeline commit による `checkpoint_advanced` を正本として参照します。

## 前提条件
- Python 3.11 以上
- Google Cloud で Gmail API を有効化していること
- OAuth クライアントの `credentials.json` を取得済みであること
- Discord Webhook URL を用意していること

## ディレクトリ構成
- `amazon_notify/`: 本体パッケージ
- `pyproject.toml`: パッケージ定義と CLI エントリポイント
- `CHANGELOG.md`: リリース履歴
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

4. `config.json` の `discord_webhook_url` などを環境に合わせて編集します。`config.json`、`credentials.json`、`token.json`、`logs/`、`events.jsonl`、`runs.jsonl` は既定でこのファイルと同じディレクトリを基準に扱います。
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

副作用なしで単発確認（Discord送信・state更新なし）:

```bash
amazon-notify --once --dry-run
```

ログ保存先を上書き:

```bash
amazon-notify --log-file /var/log/amazon-notify/notifier.log
```

設定ファイルを明示:

```bash
amazon-notify --config /opt/amazon-notify/config.json
```

設定検証:

```bash
amazon-notify --validate-config
```

ヘルスチェック(JSON出力):

```bash
amazon-notify --health-check
```

`--health-check` は常に JSON を標準出力し、全チェック成功時は終了コード `0`、1つでも異常がある場合は終了コード `1` を返します。

Discord疎通確認:

```bash
amazon-notify --test-discord
```

## 設定
`config.example.json` には次の項目があります。

- `discord_webhook_url`: Discord Webhook URL
- `amazon_from_pattern`: 差出人アドレスに対する正規表現
- `amazon_subject_pattern`: 件名に対する正規表現
- `max_messages`: 1 回のポーリングで確認する最大件数
- `poll_interval_seconds`: 常駐時の監視間隔
- `state_file`: 状態ファイルの保存先
- `events_file`: 失敗/commitイベント(JSONL)の保存先
- `runs_file`: 実行結果(JSONL)の保存先
- `log_file`: ログファイルの保存先

`max_messages` は 1 監視周期の間に受信しうるメール件数より十分大きくしてください。短時間に大量のメールが流れる運用では、この値が小さいと古い未処理メールを拾いきれません。
`amazon_from_pattern` は JSON 文字列として書くため、バックスラッシュは `\\` でエスケープします（例: `amazon\\.co\\.jp`）。
`amazon_subject_pattern` は Python 正規表現として評価されます。不正な正規表現が設定されている場合は、起動時にエラーを表示して終了します。
`state_file`、`events_file`、`runs_file`、`log_file` の相対パスは `config.json` のあるディレクトリ基準で解決されます。保存先ディレクトリが未作成でも自動作成します。
`poll_interval_seconds` は意味検証で下限（10秒）をチェックします。

## Ordered Frontier ポリシー
- 処理順は oldest-first（古いメッセージから）です。
- 途中で `message detail` 取得失敗または `delivery` 失敗が起きた場合、その時点で処理を停止します。
- checkpoint は成功した最後のメッセージまでしか進めません。

この方針は throughput より frontier の整合性を優先するための仕様です。後続メッセージを先に進めて穴を作らないことを最優先にしています。

## JSONL 記録例
`events.jsonl`:

```json
{"schema_version":1,"event":"checkpoint_advanced","run_id":"run-1","at":"2026-04-04 10:00:01","checkpoint":"18c0...01","source":"pipeline_commit"}
{"schema_version":1,"event":"delivery_failed","run_id":"run-2","at":"2026-04-04 10:05:12","error":"Amazon メールの Discord 通知に失敗しました。","message_id":"18c0...02"}
```

`runs.jsonl`:

```json
{"schema_version":1,"run_id":"run-2","started_at":"2026-04-04 10:05:10","ended_at":"2026-04-04 10:05:12","checkpoint_before":"18c0...01","checkpoint_after":"18c0...01","processed_count":1,"matched_count":1,"notified_count":0,"non_target_count":0,"failure_kind":"delivery_failed","should_retry":true,"should_alert":true,"auth_status":"READY"}
```

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
- Discord 通知に失敗したメールは `state.json` の checkpoint を進めず、次周期で再試行します。
- 失敗種別と commit 履歴は `events.jsonl` / `runs.jsonl` で追跡できます。

## テスト
開発依存をインストールしてから実行します。

```bash
pip install -e .[dev]
pytest -q
```

カバレッジ付き:

```bash
pytest -q --cov=amazon_notify --cov-report=term-missing --cov-report=xml --cov-fail-under=90
```

静的チェック:

```bash
ruff check amazon_notify tests
mypy amazon_notify
```

## Makefile
- `make setup`: 実行依存のセットアップ
- `make setup-dev`: 開発依存のセットアップ
- `make test`: テスト
- `make coverage`: カバレッジ付きテスト
- `make lint`: 構文チェック
- `make ruff`: Ruff チェック
- `make typecheck`: mypy 型チェック
- `make dry-run`: `--once --dry-run` で単発確認
- `make test-discord`: Discord テスト通知
- `make validate-config`: 設定検証
- `make health-check`: ヘルスチェック JSON 出力
- `make clean`: `__pycache__`、`.pyc`、`.pytest_cache` の削除
- `make dist`: 実行に必要なファイルだけを含む配布 zip (`dist/amazon-notify.zip`) の作成

## CI
GitHub Actions では以下を実行します。

- Python 3.11 / 3.12
- `pip install -e .[dev]`
- `python -m compileall -q amazon_notify`
- `ruff check amazon_notify tests`
- `mypy amazon_notify`
- `amazon-notify --help`
- `pytest -q --cov=amazon_notify --cov-report=term-missing --cov-fail-under=90`

## 変更履歴
リリース単位の変更点は `CHANGELOG.md` を参照してください。

## systemd
`deployment/systemd/amazon-notify.service` を環境に合わせて修正して利用してください。詳細は `docs/OPERATIONS.md` を参照してください。

## セキュリティ
次のローカルファイルはコミットしないでください。

- `credentials.json`
- `token.json`
- `config.json`
- `state.json`
- `events.jsonl`
- `runs.jsonl`
- `logs/`

`.gitignore` で除外しています。
