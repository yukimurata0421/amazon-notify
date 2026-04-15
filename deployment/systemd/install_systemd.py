#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import getpass
import pwd
import shutil
import subprocess
import sys
from pathlib import Path


def _run(cmd: list[str]) -> None:
    subprocess.run(cmd, check=True)


def _render_unit(
    src: Path,
    *,
    system_user: str,
    base_dir: Path,
    default_config_path: Path,
    default_heartbeat_path: Path,
    config_path: Path,
    heartbeat_path: Path,
) -> str:
    content = src.read_text(encoding="utf-8")
    content = content.replace("User=YOUR_USER", f"User={system_user}")
    content = content.replace("/opt/amazon-notify", str(base_dir))
    content = content.replace(str(default_config_path), str(config_path))
    content = content.replace(str(default_heartbeat_path), str(heartbeat_path))
    return content


def _install_text(dest: Path, content: str) -> None:
    dest.write_text(content, encoding="utf-8")
    os.chmod(dest, 0o644)


def _install_file(src: Path, dest: Path) -> None:
    shutil.copyfile(src, dest)
    os.chmod(dest, 0o644)


def main() -> int:
    script_dir = Path(__file__).resolve().parent
    repo_dir = script_dir.parent.parent
    parser = argparse.ArgumentParser(
        description="Install amazon-notify systemd units (standard or hybrid)."
    )
    parser.add_argument("--mode", choices=("standard", "hybrid"), default="hybrid")
    parser.add_argument("--base-dir", default=str(repo_dir))
    parser.add_argument(
        "--system-user",
        default=os.environ.get("SUDO_USER") or getpass.getuser(),
    )
    parser.add_argument("--config-path", default="")
    parser.add_argument("--heartbeat-path", default="")
    parser.add_argument("--no-enable-now", action="store_true")
    parser.add_argument("--no-install-deps", action="store_true")
    args = parser.parse_args()

    if os.geteuid() != 0:
        print("Please run as root (sudo).", file=sys.stderr)
        return 1

    try:
        pwd.getpwnam(args.system_user)
    except KeyError:
        print(f"Invalid --system-user: {args.system_user}", file=sys.stderr)
        return 1

    base_dir = Path(args.base_dir).expanduser().resolve()
    if not base_dir.exists():
        print(f"--base-dir does not exist: {base_dir}", file=sys.stderr)
        return 1

    default_config_path = base_dir / "config.json"
    default_heartbeat_path = base_dir / "runtime/pubsub-heartbeat.txt"
    config_path = (
        Path(args.config_path).expanduser().resolve()
        if args.config_path
        else default_config_path
    )
    heartbeat_path = (
        Path(args.heartbeat_path).expanduser().resolve()
        if args.heartbeat_path
        else default_heartbeat_path
    )

    if not args.no_install_deps:
        venv_path = base_dir / ".venv"
        if not venv_path.exists():
            _run(["python3", "-m", "venv", str(venv_path)])
        _run([str(venv_path / "bin/python"), "-m", "pip", "install", "--upgrade", "pip"])
        if args.mode == "hybrid":
            _run([str(venv_path / "bin/pip"), "install", "-e", f"{base_dir}[pubsub]"])
        else:
            _run([str(venv_path / "bin/pip"), "install", "-e", str(base_dir)])

    required_scripts = [
        "notify_on_failure.py",
        "renew_watch.py",
        "watchdog_restart_main.py",
    ]
    for name in required_scripts:
        path = base_dir / "deployment/systemd" / name
        if not path.exists():
            print(f"{name} not found under {base_dir}/deployment/systemd", file=sys.stderr)
            return 1
        os.chmod(path, 0o755)

    alert_env = base_dir / "deployment/systemd/amazon-notify-alert.env"
    if not alert_env.exists():
        shutil.copyfile(
            script_dir / "amazon-notify-alert.env.example",
            alert_env,
        )
        os.chmod(alert_env, 0o644)

    renew_env = base_dir / "deployment/systemd/amazon-notify-watch-renew.env"
    if not renew_env.exists():
        shutil.copyfile(
            script_dir / "amazon-notify-watch-renew.env.example",
            renew_env,
        )
        os.chmod(renew_env, 0o644)

    unit_dir = Path("/etc/systemd/system")
    render_targets = [
        ("amazon-notify.service", "amazon-notify.service"),
        ("amazon-notify-alert@.service", "amazon-notify-alert@.service"),
    ]
    if args.mode == "hybrid":
        render_targets.extend(
            [
                ("amazon-notify-pubsub.service", "amazon-notify-pubsub.service"),
                ("amazon-notify-fallback.service", "amazon-notify-fallback.service"),
                ("amazon-notify-main-watchdog.service", "amazon-notify-main-watchdog.service"),
                ("amazon-notify-watch-renew.service", "amazon-notify-watch-renew.service"),
            ]
        )

    for src_name, dest_name in render_targets:
        rendered = _render_unit(
            script_dir / src_name,
            system_user=args.system_user,
            base_dir=base_dir,
            default_config_path=default_config_path,
            default_heartbeat_path=default_heartbeat_path,
            config_path=config_path,
            heartbeat_path=heartbeat_path,
        )
        _install_text(unit_dir / dest_name, rendered)

    if args.mode == "hybrid":
        _install_file(
            script_dir / "amazon-notify-fallback.timer",
            unit_dir / "amazon-notify-fallback.timer",
        )
        _install_file(
            script_dir / "amazon-notify-main-watchdog.timer",
            unit_dir / "amazon-notify-main-watchdog.timer",
        )
        _install_file(
            script_dir / "amazon-notify-watch-renew.timer",
            unit_dir / "amazon-notify-watch-renew.timer",
        )

    _run(["systemctl", "daemon-reload"])

    if not args.no_enable_now:
        if args.mode == "hybrid":
            _run(["systemctl", "enable", "--now", "amazon-notify-pubsub.service"])
            _run(["systemctl", "enable", "--now", "amazon-notify-fallback.timer"])
            _run(["systemctl", "enable", "--now", "amazon-notify-main-watchdog.timer"])
            _run(["systemctl", "enable", "--now", "amazon-notify-watch-renew.timer"])
        else:
            _run(["systemctl", "enable", "--now", "amazon-notify.service"])

    print("Install complete.")
    print(f"Mode: {args.mode}")
    print(f"Base dir: {base_dir}")
    print(f"System user: {args.system_user}")
    print(f"Config path: {config_path}")
    print(f"Heartbeat path: {heartbeat_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
