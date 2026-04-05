# Hybrid Quickstart (Pub/Sub + Fallback) for Practitioners

このドキュメントは、`amazon-notify` を **StreamingPull + fallback-watchdog** で実運用するための
最短手順です。実際に詰まりやすいポイントを前提に、コピペで進められる形にしています。

対象環境:
- Linux (Debian/Ubuntu/Raspberry Pi OS)
- systemd 運用
- Gmail API + Pub/Sub を同一 GCP プロジェクトで利用

前提:
- プロジェクトルート: `/opt/amazon-notify` （必要に応じて置換）
- `credentials.json` は配置済み

環境依存パラメータの完全一覧は `docs/PORTABILITY_PARAMS_JA.md` を参照してください。

---

## 1. 事前チェック

```bash
cd /opt/amazon-notify
python3 --version
```

`config.json` は **1つの JSON オブジェクト**である必要があります（`{...}{...}` の連結は不可）。

```bash
python3 -m json.tool ./config.json >/dev/null && echo CONFIG_JSON_OK
```

---

## 2. Cloud SDK (`gcloud`) を導入

```bash
sudo apt-get update
sudo apt-get install -y apt-transport-https ca-certificates gnupg curl

curl -fsSL https://packages.cloud.google.com/apt/doc/apt-key.gpg \
| sudo gpg --dearmor -o /usr/share/keyrings/cloud.google.gpg

echo "deb [signed-by=/usr/share/keyrings/cloud.google.gpg] https://packages.cloud.google.com/apt cloud-sdk main" \
| sudo tee /etc/apt/sources.list.d/google-cloud-sdk.list >/dev/null

sudo apt-get update
sudo apt-get install -y google-cloud-cli

gcloud --version
```

---

## 3. 認証（ここが重要）

`gcloud auth login` と `gcloud auth application-default login` は別です。
Pub/Sub クライアントには **ADC**（Application Default Credentials）が必要です。

```bash
gcloud auth login
gcloud auth application-default login
```

プロジェクト設定:

```bash
gcloud config set project PROJECT_ID
gcloud auth application-default set-quota-project PROJECT_ID
```

ADC 確認:

```bash
gcloud auth application-default print-access-token >/dev/null && echo ADC_OK
```

---

## 4. Pub/Sub 準備（Topic / Subscription / IAM）

```bash
gcloud services enable pubsub.googleapis.com gmail.googleapis.com

gcloud pubsub topics create amazon-notify-topic
gcloud pubsub subscriptions create amazon-notify-sub --topic amazon-notify-topic

gcloud pubsub topics add-iam-policy-binding amazon-notify-topic \
  --member="serviceAccount:gmail-api-push@system.gserviceaccount.com" \
  --role="roles/pubsub.publisher"
```

存在確認:

```bash
gcloud pubsub topics list --format='value(name)' | grep '/topics/amazon-notify-topic'
gcloud pubsub subscriptions list --format='value(name)' | grep '/subscriptions/amazon-notify-sub'
```

---

## 5. `config.json` を Pub/Sub 対応にする

既存キーを残したまま、以下を同じオブジェクト内に追加:

```json
{
  "pubsub_subscription": "projects/PROJECT_ID/subscriptions/amazon-notify-sub",
  "pubsub_main_service_name": "amazon-notify-pubsub.service",
  "pubsub_heartbeat_file": "runtime/pubsub-heartbeat.txt",
  "pubsub_heartbeat_interval_seconds": 30,
  "pubsub_heartbeat_max_age_seconds": 120,
  "pubsub_trigger_failure_max_consecutive": 5,
  "pubsub_trigger_failure_base_delay_seconds": 1.0,
  "pubsub_trigger_failure_max_delay_seconds": 60.0,
  "pubsub_stream_reconnect_base_delay_seconds": 1.0,
  "pubsub_stream_reconnect_max_delay_seconds": 60.0,
  "pubsub_stream_reconnect_max_attempts": 0
}
```

検証:

```bash
source .venv/bin/activate
amazon-notify --config ./config.json --validate-config
```

---

## 6. Gmail watch 登録

```bash
amazon-notify --config ./config.json --setup-watch \
  --pubsub-topic projects/PROJECT_ID/topics/amazon-notify-topic
```

成功時は `historyId` と `expiration` が返ります。
`expiration` は watch 期限（通常約7日）です。

---

## 7. 手動で StreamingPull 動作確認

```bash
amazon-notify --config ./config.json --streaming-pull
```

想定ログ:
- `STREAMING_PULL_MODE_START`
- `RUN_ONCE_*`（初回キャッチアップ）

---

## 8. systemd ハイブリッド導入

```bash
sudo bash deployment/systemd/install-systemd.sh \
  --mode hybrid \
  --base-dir /opt/amazon-notify \
  --system-user your_user \
  --config-path /opt/amazon-notify/config.json \
  --heartbeat-path /opt/amazon-notify/runtime/pubsub-heartbeat.txt
```

このスクリプトは `YOUR_USER` と `/opt/amazon-notify` を引数値で自動置換して unit を生成します。
既存導入済み環境を再適用する場合も、同じコマンドを再実行すれば更新できます。

確認:

```bash
sudo systemctl status amazon-notify-pubsub.service --no-pager -l
sudo systemctl status amazon-notify-fallback.timer --no-pager -l
```

---

## 9. 受け入れテスト（必須）

1. Amazon 判定されるテストメールを1通送る
2. Discord 通知が届くことを確認
3. フェールオーバー確認（任意）:

```bash
sudo systemctl stop amazon-notify-pubsub.service
sudo systemctl start amazon-notify-fallback.service
sudo journalctl -u amazon-notify-fallback.service -n 100 --no-pager
sudo systemctl start amazon-notify-pubsub.service
```

---

## 10. よくある失敗と対処

### `GMAIL_WATCH_SETUP_FAILED ... Resource not found (resource=amazon-notify-topic)`
- 原因: Topic が存在しない、または project 不一致
- 対処: `gcloud config set project ...` の後、topic 作成・再実行

### `DefaultCredentialsError: Your default credentials were not found`
- 原因: ADC 未設定
- 対処: `gcloud auth application-default login` を実行

### `status=217/USER` (systemd)
- 原因: `User=YOUR_USER` が未置換
- 対処: unit の `User` を実ユーザーへ変更

### `config.json` の JSON エラー（Extra data など）
- 原因: JSON オブジェクトを2つ連結
- 対処: 1つの `{ ... }` に統合

---

## 11. 運用メモ

- watch は期限付きです。期限前に `--setup-watch` を再実行してください。
- 監視ログ:

```bash
sudo journalctl -u amazon-notify-pubsub.service -f
sudo journalctl -u amazon-notify-fallback.service -f
```

- ヘルスチェック:

```bash
amazon-notify --config ./config.json --health-check
```
