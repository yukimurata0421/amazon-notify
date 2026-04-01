# Operations Guide

## 初回セットアップ
1. `cp config.example.json config.json`
2. `config.json` の `discord_webhook_url` を設定
3. `credentials.json` を配置
4. `python amazon_mail_notifier.py --reauth` で `token.json` を作成
5. `python amazon_mail_notifier.py --once` で動作確認

## 通常運用
- 常駐監視: `python amazon_mail_notifier.py`
- 監視間隔変更: `python amazon_mail_notifier.py --interval 120`
- 再認証: `python amazon_mail_notifier.py --reauth`
- ログ保存先変更: `python amazon_mail_notifier.py --log-file /var/log/amazon-notify/notifier.log`

## 認証エラー時の挙動
- `token.json` が存在しない場合は自動 OAuth を起動せず、警告のみ送信します。
- `token.json` の読み込みに失敗した場合は警告を送り、`--reauth` を促します。
- 期限切れトークンは自動更新を試みます。
- 自動更新が失敗した場合:
  - 一時障害ならその周期をスキップして次周期で再試行します。
  - 恒久障害なら警告を送り、`--reauth` を促します。
- 認証が復旧した場合は復旧通知を 1 回だけ送信します。

## 通知失敗時の挙動
- Gmail API からメッセージ一覧を取得できない場合、その周期はスキップします。
- メッセージ詳細取得に失敗した場合、そのメッセージ以降の処理を止めて次周期で再試行します。
- Discord 通知に失敗した場合は `state.json` を進めません。
- そのため、通知に失敗したメールは次周期で再試行されます。

## ログ
- 既定の保存先: `logs/amazon_mail_notifier.log`
- ローテーション: 2MB x 5 世代
- 標準出力にも同じログを出します。

## systemd 運用
1. `deployment/systemd/amazon_mail_notifier.service` を `/etc/systemd/system/` に配置します。
2. `User`, `WorkingDirectory`, `ExecStart` を環境に合わせて修正します。
3. 反映します。

```bash
sudo systemctl daemon-reload
sudo systemctl enable amazon_mail_notifier.service
sudo systemctl restart amazon_mail_notifier.service
```

4. 状態を確認します。

```bash
sudo systemctl status amazon_mail_notifier.service
sudo journalctl -u amazon_mail_notifier.service -f
```

## 運用メモ
- `max_messages` は 1 監視周期あたりの最大流入数より大きくしてください。
- `state.json` を削除すると未処理境界が失われ、直近メールを再走査します。
- `config.json`、`credentials.json`、`token.json`、`state.json`、`logs/` は Git に含めないでください。

## 配布前クリーンアップ
```bash
make clean
make dist
```
