# Portability Parameters (環境依存値一覧)

English version: [PORTABILITY_PARAMS.en.md](./PORTABILITY_PARAMS.en.md)

この文書は、`amazon-notify` を他人環境へ移すときに必要な
**環境依存パラメータ**を網羅したチェックリストです。

## 1. 何が環境依存か

| 区分 | パラメータ | 例 | 設定場所 | 備考 |
|---|---|---|---|---|
| ファイルシステム | `BASE_DIR` | `/opt/amazon-notify` | `install-systemd.sh --base-dir` | コード配置ディレクトリ |
| Linuxユーザー | `SYSTEM_USER` | `ubuntu` / `yuki` | `install-systemd.sh --system-user` | systemd `User=` に反映 |
| 設定ファイル | `CONFIG_PATH` | `/opt/amazon-notify/config.json` | `install-systemd.sh --config-path` | `ExecStart --config` に反映 |
| runtime directory 基準 | dedupe/index 派生ファイル | `.discord_dedupe_state.json`, `*.index.json` | `CONFIG_PATH` の配置ディレクトリ基準 | `--config` を変えると参照先も変わる |
| heartbeat | `HEARTBEAT_PATH` | `/opt/amazon-notify/runtime/pubsub-heartbeat.txt` | `install-systemd.sh --heartbeat-path` | pubsub/fallback 両方に反映 |
| GCP | `PROJECT_ID` | `my-gcp-project` | `config.json`, CLI 引数 | topic/subscription の親 |
| Pub/Sub | `TOPIC_ID` | `amazon-notify-topic` | `--setup-watch --pubsub-topic` | `projects/<PROJECT_ID>/topics/<TOPIC_ID>` |
| Pub/Sub | `SUBSCRIPTION_ID` | `amazon-notify-sub` | `config.json.pubsub_subscription` | `projects/<PROJECT_ID>/subscriptions/<SUBSCRIPTION_ID>` |
| 通知先 | `DISCORD_WEBHOOK_URL` | `https://discord.com/api/webhooks/...` | `config.json.discord_webhook_url` | 運用値。秘匿対象 |
| OAuth | `credentials.json` | `<base-dir>/credentials.json` | ローカルファイル | Gmail API OAuth クライアント |
| OAuth | `token.json` | `<base-dir>/token.json` | `amazon-notify --reauth` で生成 | Gmail API 実行トークン |
| ADC | Application Default Credentials | `~/.config/gcloud/application_default_credentials.json` | `gcloud auth application-default login` | Pub/Sub クライアントで必須 |
| 障害通知 | `DISCORD_ALERT_WEBHOOK_URL` | `https://discord.com/api/webhooks/...` | `<base-dir>/deployment/systemd/amazon-notify-alert.env` | OnFailure 用（任意） |

## 2. 最低限の変更対象

他人環境に移すとき、最低でも次を置換してください。

1. `PROJECT_ID`
2. `TOPIC_ID` / `SUBSCRIPTION_ID`
3. `BASE_DIR`
4. `SYSTEM_USER`
5. `DISCORD_WEBHOOK_URL`

## 3. 反映ポイント

### `config.json`

```json
{
  "discord_webhook_url": "https://discord.com/api/webhooks/REPLACE_ME",
  "pubsub_subscription": "projects/PROJECT_ID/subscriptions/SUBSCRIPTION_ID",
  "pubsub_main_service_name": "amazon-notify-pubsub.service",
  "pubsub_heartbeat_file": "runtime/pubsub-heartbeat.txt"
}
```

### systemd 導入例

`install-systemd.sh` に環境依存値を明示して実行します。

```bash
sudo bash deployment/systemd/install-systemd.sh \
  --mode hybrid \
  --base-dir /path/to/amazon-notify \
  --system-user your_user \
  --config-path /path/to/amazon-notify/config.json \
  --heartbeat-path /path/to/amazon-notify/runtime/pubsub-heartbeat.txt
```

## 4. GCP 側チェック

```bash
gcloud config set project PROJECT_ID

gcloud pubsub topics list --format='value(name)' | grep "projects/PROJECT_ID/topics/TOPIC_ID"
gcloud pubsub subscriptions list --format='value(name)' | grep "projects/PROJECT_ID/subscriptions/SUBSCRIPTION_ID"
```

Gmail watch 用 IAM:

```bash
gcloud pubsub topics add-iam-policy-binding TOPIC_ID \
  --member="serviceAccount:gmail-api-push@system.gserviceaccount.com" \
  --role="roles/pubsub.publisher"
```

## 5. 実行前バリデーション

```bash
python3 -m json.tool ./config.json >/dev/null
amazon-notify --config ./config.json --validate-config
amazon-notify --config ./config.json --health-check
```

## 6. よくあるミス

- `config.json` を `{...}{...}` の2オブジェクト連結で保存する
- `gcloud auth login` のみで ADC を設定したつもりになる
- systemd unit の `User=YOUR_USER` / `/opt/amazon-notify` を未置換のまま起動する
- topic/subscription の project が `config.json` と一致していない
- `--config` を変えたのに、前の runtime directory 側の `.discord_dedupe_state.json` / `*.index.json` を見て原因調査してしまう
