# Incident: SSL Handshake Alert (2026-05-04)

## 概要
- 種別: Gmail API 一時通信障害
- 症状: Discord へ `sslv3 alert handshake failure` 警告通知
- 影響: 一部実行サイクルで Gmail API 呼び出し失敗の可能性（常時停止はなし）
- 復旧: 一時障害として再試行する判定を追加し、サービス再起動で反映

## 時系列 (UTC)
- 2026-05-04 13:56 頃: 運用側で Discord 警告を確認（`[SSL: SSLV3_ALERT_HANDSHAKE_FAILURE]`）。
- 2026-05-04 13:58: 稼働確認。`amazon-notify` は実行中、heartbeat 更新継続、`last_trigger_ok=true`。
- 2026-05-04 13:59-14:00: 原因切り分け。`ssl handshake failure` が transient 判定漏れの可能性を確認。
- 2026-05-04 14:00: コード修正。
  - `amazon_notify/gmail_client.py`: `ssl.SSLError` を transient として扱う。
  - `amazon_notify/gmail_client.py`: `handshake failure` 系キーワードを transient 判定に追加。
  - `tests/unit/test_notifier_core.py`: `SSLV3_ALERT_HANDSHAKE_FAILURE` ケースを追加。
- 2026-05-04 14:01: `amazon-notify-pubsub.service` 反映対応。
  - `systemctl restart` は対話認証要求で失敗。
  - 既存 PID へ `TERM` 送信し、`Restart=always` により systemd 自動再起動で反映。
- 2026-05-04 14:01:12: サービス再起動完了（新 PID 2619413）。
- 2026-05-04 14:01:13 以降: `RUN_ONCE_*`/`STREAMING_PULL_MODE_START` が正常出力、稼働継続。

## 恒久対応
- 方針: SSL handshake 系の失敗を「一時障害」としてリトライ経路に統一。
- 期待効果: 短時間の TLS 揺らぎで即時アラート化せず、自動復旧率を向上。
- 残課題: `.venv` に `pytest` が未導入のため、現地では回帰テスト未実行（構文チェックのみ）。
