from __future__ import annotations

import argparse
import sys


def stderr_error(message: str) -> None:
    sys.stderr.write(f"[ERROR] {message}\n")


def validate_action_conflicts(args: argparse.Namespace) -> None:
    action_flags = (
        ("--reauth", args.reauth),
        ("--health-check", args.health_check),
        ("--validate-config", args.validate_config),
        ("--test-discord", args.test_discord),
        ("--setup-watch", args.setup_watch),
        ("--rebuild-indexes", args.rebuild_indexes),
        ("--status", args.status),
        ("--doctor", args.doctor),
        ("--verify-state", args.verify_state),
        ("--metrics", args.metrics),
        ("--streaming-pull", args.streaming_pull),
        ("--scenario-harness", args.scenario_harness),
    )
    selected_actions = [name for name, enabled in action_flags if enabled]
    if len(selected_actions) <= 1:
        return
    joined = ", ".join(selected_actions)
    stderr_error(f"action flags は同時指定できません。1つだけ指定してください: {joined}")
    raise SystemExit(1)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Amazon配送メールを監視してDiscordに通知"
    )
    parser.add_argument(
        "--config",
        type=str,
        default="config.json",
        help="設定ファイルの保存先。相対パスはカレントディレクトリ基準。",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="1回だけ実行して終了する（デフォルトはループ実行）",
    )
    parser.add_argument(
        "--interval",
        type=int,
        help="ループ実行時の待ち時間（秒）。configのpoll_interval_secondsより優先される。",
    )
    parser.add_argument(
        "--reauth",
        action="store_true",
        help="対話OAuthで token.json を再作成して終了する。",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Gmail取得と判定のみ実行し、Discord送信とstate更新を行わない。",
    )
    parser.add_argument(
        "--test-discord",
        action="store_true",
        help="設定済み Discord Webhook へテスト通知を送信して終了する。",
    )
    parser.add_argument(
        "--validate-config",
        action="store_true",
        help="設定ファイルを検証して終了する。",
    )
    parser.add_argument(
        "--rebuild-indexes",
        action="store_true",
        help="events/runs の index snapshot を再構築して終了する。",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="運用サマリ(frontier/incident/failure/整合性)を表示して終了する。",
    )
    parser.add_argument(
        "--doctor",
        action="store_true",
        help="state/events/runs/index の整合性診断(JSON)を表示して終了する。",
    )
    parser.add_argument(
        "--verify-state",
        action="store_true",
        help="--doctor と同じ整合性検査(JSON)。定期バッチ向けの別名。",
    )
    parser.add_argument(
        "--metrics",
        action="store_true",
        help="運用メトリクス(JSON 既定)を表示して終了する。",
    )
    parser.add_argument(
        "--metrics-plain",
        action="store_true",
        help="--metrics を簡易テキストで出力する。",
    )
    parser.add_argument(
        "--metrics-window",
        type=int,
        default=50,
        help="--metrics の直近 run 集計に使う最大件数。",
    )
    parser.add_argument(
        "--health-check",
        action="store_true",
        help="実行前提のヘルスチェック結果をJSONで出力して終了する。",
    )
    parser.add_argument(
        "--log-file",
        type=str,
        help="ログファイルの保存先（未指定時は logs/amazon_mail_notifier.log）。",
    )
    parser.add_argument(
        "--streaming-pull",
        action="store_true",
        help="Pub/Sub StreamingPull でイベント駆動実行する。",
    )
    parser.add_argument(
        "--pubsub-subscription",
        type=str,
        help="StreamingPull 対象の subscription（projects/.../subscriptions/...）。",
    )
    parser.add_argument(
        "--pubsub-flow-max-messages",
        type=int,
        default=50,
        help="StreamingPull の flow control max_messages。",
    )
    parser.add_argument(
        "--pubsub-pending-warn-threshold",
        "--pubsub-trigger-queue-size",
        type=int,
        dest="pubsub_pending_warn_threshold",
        default=256,
        help="StreamingPull の pending backlog 警告しきい値（旧名 --pubsub-trigger-queue-size 互換）。",
    )
    parser.add_argument(
        "--heartbeat-file",
        type=str,
        help="StreamingPull 用 heartbeat ファイル。相対パスは runtime 基準。",
    )
    parser.add_argument(
        "--heartbeat-interval-seconds",
        type=float,
        help="StreamingPull が heartbeat を更新する間隔秒。",
    )
    parser.add_argument(
        "--heartbeat-max-age-seconds",
        type=float,
        help="fallback-watchdog が異常判定に使う heartbeat 最大許容秒。",
    )
    parser.add_argument(
        "--main-service-name",
        type=str,
        help="fallback-watchdog で監視するメイン系 systemd サービス名。",
    )
    parser.add_argument(
        "--fallback-watchdog",
        action="store_true",
        help="--once 実行時にメイン系を監視し、健全ならポーリングをスキップする。",
    )
    parser.add_argument(
        "--setup-watch",
        action="store_true",
        help="Gmail watch を Pub/Sub topic に登録して終了する。",
    )
    parser.add_argument(
        "--pubsub-topic",
        type=str,
        help="watch 登録先 topic（projects/.../topics/...）。",
    )
    parser.add_argument(
        "--watch-label-ids",
        type=str,
        default="INBOX",
        help="watch 対象の Gmail label（カンマ区切り、既定は INBOX）。",
    )
    parser.add_argument(
        "--watch-label-filter-action",
        choices=("include", "exclude"),
        default="include",
        help="watch の labelFilterAction。",
    )
    parser.add_argument(
        "--watch-retries",
        type=int,
        default=4,
        help="watch API 登録時の最大リトライ回数。",
    )
    parser.add_argument(
        "--watch-base-delay",
        type=float,
        default=1.0,
        help="watch API 登録リトライの初期待機秒。",
    )
    parser.add_argument(
        "--watch-max-delay",
        type=float,
        default=60.0,
        help="watch API 登録リトライの最大待機秒。",
    )
    parser.add_argument(
        "--scenario-harness",
        action="store_true",
        help="障害注入シナリオを実行して検証結果をJSONで出力する。",
    )
    parser.add_argument(
        "--scenario-names",
        type=str,
        default="",
        help="実行するシナリオ名（カンマ区切り）。未指定時は全件。",
    )
    return parser
