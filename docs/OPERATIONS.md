# Operations Guide

設計背景を含む詳細解説は `docs/HYBRID_ARCHITECTURE_JA.md` を参照してください。

## 初回セットアップ
1. `python3 -m venv .venv && source .venv/bin/activate`
2. `pip install .`
3. `cp config.example.json config.json`
4. `config.json` の `discord_webhook_url` を設定
5. `credentials.json` を `config.json` と同じディレクトリに配置
6. `amazon-notify --reauth` で `token.json` を作成
7. `amazon-notify --once` で動作確認
8. 必要に応じて `amazon-notify --test-discord` で Discord 疎通確認

## 通常運用
- 常駐監視: `amazon-notify`
- StreamingPull 常駐: `amazon-notify --streaming-pull --pubsub-subscription projects/PROJECT_ID/subscriptions/SUBSCRIPTION_ID`
- Gmail watch 登録: `amazon-notify --setup-watch --pubsub-topic projects/PROJECT_ID/topics/TOPIC_ID`
- 監視間隔変更: `amazon-notify --interval 120`
- 副作用なし確認: `amazon-notify --once --dry-run`
- 再認証: `amazon-notify --reauth`
- Discord テスト通知: `amazon-notify --test-discord`
- 設定検証: `amazon-notify --validate-config`
- ヘルスチェック(JSON): `amazon-notify --health-check`
  - 全チェック成功時は終了コード `0`、異常を含む場合は `1`
- ログ保存先変更: `amazon-notify --log-file /var/log/amazon-notify/notifier.log`
- 設定ファイル変更: `amazon-notify --config /opt/amazon-notify/config.json`
- モジュール実行: `python -m amazon_notify.cli`
- `amazon_subject_pattern` が不正な正規表現なら、起動時にエラーを表示して終了します。
- `state_file`、`events_file`、`runs_file`、`log_file` の相対パスは `config.json` のあるディレクトリ基準で解決されます。
- Pub/Sub を使う場合は追加で `pip install .[pubsub]` を実行します。
- `transient_alert_min_duration_seconds` と `transient_alert_cooldown_seconds` で一時障害アラート境界を調整できます。
- `structured_logging=true` にすると JSON 形式でログを出力します。

## v0.3.0 移行仕様
- checkpoint の正本は `events.jsonl`（`checkpoint_advanced`）です。
- `state.json` は互換スナップショット（派生物）として更新されます。
- 初回起動時に `events.jsonl` が空で `state.json.last_message_id` がある場合のみ、bootstrap 用 `checkpoint_advanced` を 1 回記録します。
- rollback 観点:
  - `state.json` は継続更新されるため、0.1 系の境界情報は保持されます。
  - ただし 0.2 系の監査情報（events/runs）は 0.1 系では参照されません。

## 認証エラー時の挙動
- `token.json` が存在しない場合は自動 OAuth を起動せず、警告のみ送信します。
- `token.json` の読み込みに失敗した場合は警告を送り、`amazon-notify --reauth` を促します。
- 期限切れトークンは自動更新を試みます。
- 自動更新が失敗した場合:
  - 一時障害ならその周期をスキップして次周期で再試行します。
  - 恒久障害なら警告を送り、`amazon-notify --reauth` を促します。
- 認証が復旧した場合は復旧通知を 1 回だけ送信します。

## 通知失敗時の挙動
- Gmail API からメッセージ一覧を取得できない場合、その周期はスキップします。
- メッセージ詳細取得に失敗した場合、そのメッセージ以降の処理を止めて次周期で再試行します。
- Discord 通知に失敗した場合は `state.json` を進めません。
- そのため、通知に失敗したメールは次周期で再試行されます。

## 障害時の見方（v0.3.0）
- 優先確認先:
  - `events.jsonl`: 失敗種別と checkpoint 進行
  - `runs.jsonl`: 実行単位の要約（before/after, counts, failure_kind）
  - `logs/amazon_mail_notifier.log`: 補助ログ
- `delivery_failed` が出たら:
  - Discord Webhook 疎通を確認
  - `checkpoint_after` が進んでいないことを確認（仕様どおり）
- `auth_failed` が出たら:
  - `amazon-notify --reauth`
  - `token.json` と `credentials.json` の配置を確認
- checkpoint が進まないとき:
  - `events.jsonl` の `message_detail_failed` / `delivery_failed` を確認
  - ordered frontier 仕様で中断している可能性を確認

## ログ
- 既定の保存先: `logs/amazon_mail_notifier.log`
- ローテーション: 2MB x 5 世代
- 標準出力にも同じログを出します。

## systemd 運用
`deployment/systemd/amazon-notify.service` は restart storm 抑止を有効にしています。
`StartLimitIntervalSec=300` と `StartLimitBurst=3` により、5分間に3回を超えて落ちると `start-limit-hit` で停止し、`OnFailure` が起動します。

1. `deployment/systemd/amazon-notify.service` を `/etc/systemd/system/` に配置します。
2. `User`, `WorkingDirectory`, `ExecStart` を環境に合わせて修正します。
3. 事前に対象ディレクトリで `pip install .` を実行し、`.venv/bin/amazon-notify` が存在する状態にします。
4. 連続クラッシュ時に Discord 通知したい場合は、以下も配置します。
   - `deployment/systemd/amazon-notify-alert@.service` -> `/etc/systemd/system/amazon-notify-alert@.service`
   - `deployment/systemd/notify-on-failure.sh` -> `/opt/amazon-notify/deployment/systemd/notify-on-failure.sh`
   - `deployment/systemd/amazon-notify-alert.env.example` をコピーして `/opt/amazon-notify/deployment/systemd/amazon-notify-alert.env` を作成し、`DISCORD_ALERT_WEBHOOK_URL` を設定
5. `notify-on-failure.sh` に実行権限を付与します。

```bash
sudo chmod +x /opt/amazon-notify/deployment/systemd/notify-on-failure.sh
```

6. 反映します。

```bash
sudo systemctl daemon-reload
sudo systemctl enable amazon-notify.service
sudo systemctl restart amazon-notify.service
```

7. 状態を確認します。

```bash
sudo systemctl status amazon-notify.service
sudo journalctl -u amazon-notify.service -f
```

### systemd セットアップ自動化
手順をまとめて実行したい場合は、以下を利用できます。

```bash
sudo deployment/systemd/install-systemd.sh --mode hybrid
```

- `--mode standard` で polling only 構成をインストールします。
- `--no-enable-now` で unit 配置のみ行います。
- `--no-install-deps` で venv/pip の更新をスキップします。

## systemd ハイブリッド（推奨）
メイン系（リアルタイム）とサブ系（フェールオーバー）を分離します。

1. メイン系:
   - `deployment/systemd/amazon-notify-pubsub.service` を `/etc/systemd/system/` に配置
   - `config.json` の `pubsub_subscription` を設定
   - メイン系はアプリ内で自己復旧を優先し、必要時のみ systemd 再起動にフォールバックします
   - heartbeat (`runtime/pubsub-heartbeat.txt`) は `updated_at` と `worker_last_seen_at` を保持し、サイレント停止を検知します
2. サブ系:
   - `deployment/systemd/amazon-notify-fallback.service` と `deployment/systemd/amazon-notify-fallback.timer` を `/etc/systemd/system/` に配置
   - fallback service は `--fallback-watchdog` でメイン系を判定します
   - 判定ロジック:
      - `systemd is-active` が `active` でない -> フェールオーバー実行
      - heartbeat が欠損/古い -> フェールオーバー実行
      - worker heartbeat が古い -> フェールオーバー実行
      - 両方正常 -> サブ系は `[SKIP]` で終了
3. 初回に Gmail watch を登録します。

```bash
/opt/amazon-notify/.venv/bin/amazon-notify \
  --config /opt/amazon-notify/config.json \
  --setup-watch \
  --pubsub-topic projects/PROJECT_ID/topics/TOPIC_ID
```

4. 反映して起動します。

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now amazon-notify-pubsub.service
sudo systemctl enable --now amazon-notify-fallback.timer
```

5. 状態を確認します。

```bash
sudo systemctl status amazon-notify-pubsub.service
sudo systemctl status amazon-notify-fallback.timer
sudo journalctl -u amazon-notify-pubsub.service -f
```

6. サブ系の判定ログを確認したい場合:

```bash
sudo journalctl -u amazon-notify-fallback.service -f
```

## 運用メモ
- `max_messages` は 1 監視周期あたりの最大流入数より大きくしてください。
- StreamingPull の自己復旧パラメータ:
  - `pubsub_trigger_failure_max_consecutive`
  - `pubsub_trigger_failure_base_delay_seconds`
  - `pubsub_trigger_failure_max_delay_seconds`
  - `pubsub_stream_reconnect_base_delay_seconds`
  - `pubsub_stream_reconnect_max_delay_seconds`
  - `pubsub_stream_reconnect_max_attempts` (`0` は無制限)
- `state.json` を削除すると未処理境界が失われ、直近メールを再走査します。
- `state_file` にネストしたパスを指定した場合も、保存先ディレクトリは自動作成されます。
- `config.json`、`credentials.json`、`token.json`、`state.json`、`events.jsonl`、`runs.jsonl`、`logs/` は Git に含めないでください。

## 配布前クリーンアップ
```bash
make clean
make coverage
make dist
```

`make dist` で作る zip には、実行に必要なコードとドキュメント、パッケージ定義だけを含めます。出力先は `dist/amazon-notify.zip` です。テストや GitHub Actions 設定は含めません。
