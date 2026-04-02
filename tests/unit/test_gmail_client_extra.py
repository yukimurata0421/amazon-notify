import json
from pathlib import Path

import pytest

from amazon_notify import config, gmail_client


class _DummyCreds:
    def __init__(
        self,
        *,
        valid: bool = True,
        expired: bool = False,
        refresh_token: str | None = "r",
        json_text: str = "{}",
    ):
        self.valid = valid
        self.expired = expired
        self.refresh_token = refresh_token
        self._json_text = json_text
        self.refresh_outcomes: list[Exception | None] = []

    def to_json(self) -> str:
        return self._json_text

    def refresh(self, _request) -> None:
        if not self.refresh_outcomes:
            return
        outcome = self.refresh_outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome


@pytest.fixture(autouse=True)
def restore_globals() -> None:
    original_token_path = config.TOKEN_PATH
    original_credentials_path = config.CREDENTIALS_PATH
    original_import_error = gmail_client.GOOGLE_IMPORT_ERROR
    yield
    config.TOKEN_PATH = original_token_path
    config.CREDENTIALS_PATH = original_credentials_path
    gmail_client.GOOGLE_IMPORT_ERROR = original_import_error


def test_run_oauth_flow_uses_local_server_and_saves_token(monkeypatch, tmp_path: Path) -> None:
    token_path = tmp_path / "token.json"
    credentials_path = tmp_path / "credentials.json"
    credentials_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(config, "TOKEN_PATH", token_path)
    monkeypatch.setattr(config, "CREDENTIALS_PATH", credentials_path)
    monkeypatch.setattr(gmail_client, "ensure_google_dependencies", lambda: None)

    creds = _DummyCreds(json_text='{"token":"x"}')

    class Flow:
        def run_local_server(self, port: int):
            assert port == 0
            return creds

    class FlowFactory:
        @staticmethod
        def from_client_secrets_file(path: str, scopes):
            assert path == str(credentials_path)
            assert scopes == gmail_client.SCOPES
            return Flow()

    monkeypatch.setattr(gmail_client, "InstalledAppFlow", FlowFactory)

    returned = gmail_client.run_oauth_flow()
    assert returned is creds
    assert token_path.read_text(encoding="utf-8") == '{"token":"x"}'


def test_run_oauth_flow_falls_back_to_console(monkeypatch, tmp_path: Path) -> None:
    token_path = tmp_path / "token.json"
    credentials_path = tmp_path / "credentials.json"
    credentials_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(config, "TOKEN_PATH", token_path)
    monkeypatch.setattr(config, "CREDENTIALS_PATH", credentials_path)
    monkeypatch.setattr(gmail_client, "ensure_google_dependencies", lambda: None)

    creds = _DummyCreds(json_text='{"token":"console"}')

    class Flow:
        def run_local_server(self, port: int):
            raise RuntimeError("local failed")

        def run_console(self):
            return creds

    monkeypatch.setattr(
        gmail_client,
        "InstalledAppFlow",
        type(
            "FlowFactory",
            (),
            {"from_client_secrets_file": staticmethod(lambda *_args, **_kwargs: Flow())},
        ),
    )

    returned = gmail_client.run_oauth_flow()
    assert returned is creds
    assert token_path.read_text(encoding="utf-8") == '{"token":"console"}'


def test_run_oauth_flow_returns_none_when_console_also_fails(monkeypatch) -> None:
    monkeypatch.setattr(gmail_client, "ensure_google_dependencies", lambda: None)

    class Flow:
        def run_local_server(self, port: int):
            raise RuntimeError("local failed")

        def run_console(self):
            raise RuntimeError("console failed")

    monkeypatch.setattr(
        gmail_client,
        "InstalledAppFlow",
        type(
            "FlowFactory",
            (),
            {"from_client_secrets_file": staticmethod(lambda *_args, **_kwargs: Flow())},
        ),
    )

    assert gmail_client.run_oauth_flow() is None


def test_notify_token_recovery_skips_when_state_not_active(monkeypatch, tmp_path: Path) -> None:
    state = {"last_message_id": "x"}
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps(state), encoding="utf-8")

    calls: list[str] = []
    monkeypatch.setattr(
        gmail_client,
        "send_discord_recovery",
        lambda webhook_url, message: calls.append(message) or True,
    )

    gmail_client.notify_token_recovery_if_needed("https://discord.invalid/webhook", state, state_file)
    assert calls == []


def test_refresh_with_retry_retries_on_transient_and_then_succeeds(monkeypatch) -> None:
    creds = _DummyCreds()
    creds.refresh_outcomes = [TimeoutError("timed out"), None]
    sleeps: list[int] = []
    monkeypatch.setattr(gmail_client.time, "sleep", lambda sec: sleeps.append(sec))

    result = gmail_client.refresh_with_retry(creds, retries=3, base_delay=1)
    assert result is None
    assert sleeps == [1]


def test_refresh_with_retry_returns_non_transient_error_without_sleep(monkeypatch) -> None:
    creds = _DummyCreds()
    error = RuntimeError("fatal")
    creds.refresh_outcomes = [error]
    monkeypatch.setattr(gmail_client, "is_transient_network_error", lambda exc: False)

    sleeps: list[int] = []
    monkeypatch.setattr(gmail_client.time, "sleep", lambda sec: sleeps.append(sec))

    result = gmail_client.refresh_with_retry(creds, retries=3, base_delay=1)
    assert result is error
    assert sleeps == []


def test_get_gmail_service_refresh_success_writes_token(monkeypatch, tmp_path: Path) -> None:
    token_path = tmp_path / "token.json"
    token_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(config, "TOKEN_PATH", token_path)
    monkeypatch.setattr(gmail_client, "ensure_google_dependencies", lambda: None)

    creds = _DummyCreds(valid=False, expired=True, refresh_token="r", json_text='{"new":"token"}')
    monkeypatch.setattr(gmail_client.Credentials, "from_authorized_user_file", lambda *_args, **_kwargs: creds)
    monkeypatch.setattr(gmail_client, "refresh_with_retry", lambda *_args, **_kwargs: None)
    service_obj = object()
    monkeypatch.setattr(gmail_client, "build", lambda *_args, **_kwargs: service_obj)

    service = gmail_client.get_gmail_service()
    assert service is service_obj
    assert token_path.read_text(encoding="utf-8") == '{"new":"token"}'


def test_get_gmail_service_refresh_transient_error_marks_issue(monkeypatch, tmp_path: Path) -> None:
    token_path = tmp_path / "token.json"
    token_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(config, "TOKEN_PATH", token_path)
    monkeypatch.setattr(gmail_client, "ensure_google_dependencies", lambda: None)

    creds = _DummyCreds(valid=False, expired=True, refresh_token="r")
    monkeypatch.setattr(gmail_client.Credentials, "from_authorized_user_file", lambda *_args, **_kwargs: creds)
    monkeypatch.setattr(gmail_client, "refresh_with_retry", lambda *_args, **_kwargs: TimeoutError("timed out"))

    alerts: list[str] = []
    monkeypatch.setattr(gmail_client, "send_discord_alert", lambda webhook_url, message: alerts.append(message) or True)

    state_file = tmp_path / "state.json"
    state = {"last_message_id": "x"}
    service = gmail_client.get_gmail_service(
        webhook_url="https://discord.invalid/webhook",
        state=state,
        state_file=state_file,
        allow_oauth_interactive=False,
    )
    assert service is None
    assert alerts
    saved = json.loads(state_file.read_text(encoding="utf-8"))
    assert saved["transient_network_issue_active"] is True


def test_get_gmail_service_refresh_fatal_with_interactive_recovers(monkeypatch, tmp_path: Path) -> None:
    token_path = tmp_path / "token.json"
    token_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(config, "TOKEN_PATH", token_path)
    monkeypatch.setattr(gmail_client, "ensure_google_dependencies", lambda: None)

    creds = _DummyCreds(valid=False, expired=True, refresh_token="r")
    monkeypatch.setattr(gmail_client.Credentials, "from_authorized_user_file", lambda *_args, **_kwargs: creds)
    monkeypatch.setattr(gmail_client, "refresh_with_retry", lambda *_args, **_kwargs: RuntimeError("fatal"))
    monkeypatch.setattr(gmail_client, "run_oauth_flow", lambda: _DummyCreds(valid=True))
    monkeypatch.setattr(gmail_client, "build", lambda *_args, **_kwargs: object())

    alerts: list[str] = []
    monkeypatch.setattr(gmail_client, "send_discord_alert", lambda webhook_url, message: alerts.append(message) or True)

    service = gmail_client.get_gmail_service(
        webhook_url="https://discord.invalid/webhook",
        allow_oauth_interactive=True,
    )
    assert service is not None
    assert alerts


def test_get_gmail_service_invalid_token_without_refresh_token_marks_issue(monkeypatch, tmp_path: Path) -> None:
    token_path = tmp_path / "token.json"
    token_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(config, "TOKEN_PATH", token_path)
    monkeypatch.setattr(gmail_client, "ensure_google_dependencies", lambda: None)
    monkeypatch.setattr(
        gmail_client.Credentials,
        "from_authorized_user_file",
        lambda *_args, **_kwargs: _DummyCreds(valid=False, expired=False, refresh_token=None),
    )

    alerts: list[str] = []
    monkeypatch.setattr(gmail_client, "send_discord_alert", lambda webhook_url, message: alerts.append(message) or True)

    state_file = tmp_path / "state.json"
    state = {"last_message_id": "x"}
    service = gmail_client.get_gmail_service(
        webhook_url="https://discord.invalid/webhook",
        state=state,
        state_file=state_file,
        allow_oauth_interactive=False,
    )
    assert service is None
    assert alerts


def test_get_gmail_service_build_error_paths(monkeypatch, tmp_path: Path) -> None:
    token_path = tmp_path / "token.json"
    token_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(config, "TOKEN_PATH", token_path)
    monkeypatch.setattr(gmail_client, "ensure_google_dependencies", lambda: None)
    monkeypatch.setattr(
        gmail_client.Credentials,
        "from_authorized_user_file",
        lambda *_args, **_kwargs: _DummyCreds(valid=True),
    )

    state_file = tmp_path / "state.json"
    state = {"last_message_id": "x"}

    monkeypatch.setattr(gmail_client, "build", lambda *_args, **_kwargs: (_ for _ in ()).throw(TimeoutError("timed out")))
    transient = gmail_client.get_gmail_service(
        webhook_url="https://discord.invalid/webhook",
        state=state,
        state_file=state_file,
    )
    assert transient is None
    saved = json.loads(state_file.read_text(encoding="utf-8"))
    assert saved["transient_network_issue_active"] is True

    monkeypatch.setattr(gmail_client, "build", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("fatal")))
    fatal = gmail_client.get_gmail_service()
    assert fatal is None


def test_list_and_get_message_helpers() -> None:
    class _Exec:
        def __init__(self, payload: dict):
            self.payload = payload

        def execute(self) -> dict:
            return self.payload

    class _Messages:
        def list(self, *, userId: str, q: str, maxResults: int):
            assert userId == "me"
            assert q == "in:inbox"
            assert maxResults == 20
            return _Exec({"messages": [{"id": "m-1"}]})

        def get(self, *, userId: str, id: str, format: str):
            assert userId == "me"
            assert id == "m-1"
            assert format == "full"
            return _Exec({"id": "m-1", "snippet": "hello"})

    class _Users:
        def messages(self):
            return _Messages()

    class _Service:
        def users(self):
            return _Users()

    service = _Service()
    assert gmail_client.list_recent_messages(service, query="in:inbox", max_results=20) == [{"id": "m-1"}]
    assert gmail_client.get_message_detail(service, "m-1")["snippet"] == "hello"
