# ハイブリッド構成ガイド（Pub/Sub + Fallback Polling）

English version: [HYBRID_ARCHITECTURE.en.md](./HYBRID_ARCHITECTURE.en.md)

このドキュメントは、`amazon-notify` を高可用に運用するための設計意図と実装方針をまとめた詳細記事です。

対象:

- メイン系を Pub/Sub StreamingPull でリアルタイム化したい
- ただしサイレント障害や一時停止の取りこぼしを避けたい
- systemd と Discord 通知を使って運用ノイズを抑えたい

## 1. 目的

ハイブリッド構成の目的は、次の 3 点です。

1. 低遅延: メール到達後できるだけ早く Discord 通知する  
2. 耐障害性: メイン系が停止・沈黙しても最終的に通知を回収する  
3. 運用性: 失敗時の通知を「必要なときだけ」出し、アラート疲れを防ぐ

## 2. 構成概要

- メイン系（リアルタイム）  
  `amazon-notify --streaming-pull` を systemd service で常駐

- サブ系（フェールオーバー）  
  `amazon-notify --once --fallback-watchdog` を systemd timer で定期実行

- 共有状態  
  `events.jsonl`（正本 checkpoint）と `state.json`（互換スナップショット）

## 3. 障害検知レイヤー

### レイヤーA: systemd プロセス監視

メイン系 service は以下の思想で運用します。

- `Restart=always`
- `RestartSec=10`
- `OnFailure=amazon-notify-alert@%n.service`（必要時）

これにより、クラッシュは systemd が即検知し、再起動と通知を機械的に実行します。

### レイヤーB: サブ系 watchdog（相互監視）

サブ系は定期実行時に以下を判定します。

1. `systemctl is-active <main service>` が `active` か
2. heartbeat ファイル（例: `runtime/pubsub-heartbeat.txt`）が新鮮か

判定結果:

- 正常: サブ系はスキップ（実処理しない）
- 異常: サブ系がその回だけポーリング実行して取りこぼし回収

### レイヤーC: アプリ層例外ハンドリング

- Gmail API / Discord Webhook の一時障害は指数バックオフで再試行
- 恒久障害や認証障害はアラート通知
- `runs.jsonl` / `events.jsonl` に失敗種別を記録
- StreamingPull 断線時はプロセス内で再接続バックオフし、systemd 再起動は最終手段にする
- trigger 連続失敗はしきい値で打ち切り、健全性を明示的に fail へ遷移する

## 4. サイレント障害対策（heartbeat）

StreamingPull で問題になるのは「プロセスは生きているが実質停止」の状態です。

この対策として、メイン系は定期的に heartbeat ファイルの更新時刻を更新します。
サブ系は heartbeat 年齢と worker heartbeat 年齢を見て「古い = 停止相当」と判定します。

設定例:

- heartbeat 更新間隔: `30` 秒
- 異常判定閾値: `300` 秒

## 5. なぜ二重処理が問題になりにくいか

本プロジェクトは ordered frontier と checkpoint commit を前提にしているため、
重複処理が発生しても境界整合性を保ちやすい設計です。

補足:
- Pub/Sub は durable workflow queue としては扱わず、trigger 経路として扱います。
- StreamingPull 側の latest event aggregation は「取りこぼし許容」ではなく、Gmail 側 catch-up を前提にローカル backlog を簡略化するための設計です。
- frontier consistency の判定は引き続き Gmail 側状態 + `events.jsonl` で行います。

- 成功時のみ frontier を前進
- 途中失敗時は checkpoint を進めない
- サブ系が回収しても frontier 整合性を崩しにくい

## 6. 主要 CLI

- メイン系:
  `amazon-notify --streaming-pull --pubsub-subscription ...`
- watch 登録:
  `amazon-notify --setup-watch --pubsub-topic ...`
- サブ系:
  `amazon-notify --once --fallback-watchdog`

補助オプション:

- `--heartbeat-file`
- `--heartbeat-interval-seconds`
- `--heartbeat-max-age-seconds`
- `--main-service-name`

## 7. systemd ユニット例

このリポジトリのテンプレート:

- `deployment/systemd/amazon-notify-pubsub.service`
- `deployment/systemd/amazon-notify-fallback.service`
- `deployment/systemd/amazon-notify-fallback.timer`
- `deployment/systemd/amazon-notify-alert@.service`

## 8. 導入手順（要点）

1. `config.json` を作成し `discord_webhook_url` / `pubsub_subscription` を設定
2. `credentials.json` 配置、`amazon-notify --reauth`
3. `--setup-watch` を 1 回実行
4. systemd に unit 配置して `daemon-reload`
5. `amazon-notify-pubsub.service` と `amazon-notify-fallback.timer` を有効化

## 9. 監視チェックリスト

- `journalctl -u amazon-notify-pubsub.service -f`
- `journalctl -u amazon-notify-fallback.service -f`
- `amazon-notify --health-check`
- `events.jsonl` に `checkpoint_advanced` が継続して出ているか
- `runs.jsonl` の `failure_kind` が偏っていないか

## 10. トラブルシュート

- Pub/Sub trigger 停止が疑われる場合:
  - heartbeat 更新時刻を確認
  - fallback service ログで `SKIP` か `FAILOVER` か確認

- 通知が来ない:
  - `--test-discord` で webhook 疎通確認
  - `config.json` のパターン設定確認
  - `events.jsonl` の `delivery_failed` / `auth_failed` を確認

- 認証系エラー:
  - `amazon-notify --reauth`
  - `credentials.json` / `token.json` の配置再確認

## 11. 運用上の判断

- 最小構成で始めるなら: まずポーリング常駐のみ
- 可用性重視へ上げるなら: StreamingPull + fallback timer の二段構え
- アラートノイズを減らすなら:
  - incident 抑制
  - restart storm 検知
  - fallback 通知の重複抑制

---

運用時は `docs/OPERATIONS.md` と併読してください。こちらは「設計意図」、`OPERATIONS.md` は「具体手順」に寄せています。
