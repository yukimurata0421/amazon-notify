from pathlib import Path


def test_application_code_does_not_invoke_service_restart_or_reboot() -> None:
    package_root = Path(__file__).resolve().parents[2] / "amazon_notify"
    py_files = package_root.rglob("*.py")

    for path in py_files:
        text = path.read_text(encoding="utf-8")
        assert "systemctl restart" not in text
        assert "reboot" not in text
