import json
from pathlib import Path

import pytest

from amazon_notify import gmail_client
from amazon_notify.config import RuntimePaths
from amazon_notify.domain import AuthStatus


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
    original_import_error = gmail_client.GOOGLE_IMPORT_ERROR
    yield
    gmail_client.GOOGLE_IMPORT_ERROR = original_import_error


def _paths_for(tmp_path: Path) -> RuntimePaths:
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    return RuntimePaths(
        runtime_dir=runtime_dir,
        config=runtime_dir / "config.json",
        credentials=runtime_dir / "credentials.json",
        token=runtime_dir / "token.json",
        default_log=runtime_dir / "logs" / "amazon_mail_notifier.log",
    )


def test_run_oauth_flow_uses_local_server_and_saves_token(monkeypatch, tmp_path: Path) -> None:
    paths = _paths_for(tmp_path)
    token_path = paths.token
    credentials_path = paths.credentials
    credentials_path.write_text("{}", encoding="utf-8")
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

    returned = gmail_client.run_oauth_flow(paths=paths)
    assert returned is creds
    assert token_path.read_text(encoding="utf-8") == '{"token":"x"}'


def test_run_oauth_flow_falls_back_to_console(monkeypatch, tmp_path: Path) -> None:
    paths = _paths_for(tmp_path)
    token_path = paths.token
    credentials_path = paths.credentials
    credentials_path.write_text("{}", encoding="utf-8")
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

    returned = gmail_client.run_oauth_flow(paths=paths)
    assert returned is creds
    assert token_path.read_text(encoding="utf-8") == '{"token":"console"}'


def test_run_oauth_flow_returns_none_when_console_also_fails(monkeypatch, tmp_path: Path) -> None:
    paths = _paths_for(tmp_path)
    paths.credentials.write_text("{}", encoding="utf-8")
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

    assert gmail_client.run_oauth_flow(paths=paths) is None


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


def test_record_transient_issue_suppresses_alert_before_persistence_threshold(monkeypatch, tmp_path: Path) -> None:
    state_file = tmp_path / "state.json"
    state = {"last_message_id": "x"}

    alerts: list[str] = []
    monkeypatch.setattr(gmail_client, "send_discord_alert", lambda _w, message: alerts.append(message) or True)
    monkeypatch.setattr(gmail_client.time, "time", lambda: 1000.0)

    sent = gmail_client.record_transient_issue(
        state,
        state_file,
        TimeoutError("timed out"),
        webhook_url="https://discord.invalid/webhook",
        alert_message="transient issue",
        min_alert_duration_seconds=60.0,
        alert_cooldown_seconds=60.0,
    )

    assert not sent
    assert alerts == []
    saved = json.loads(state_file.read_text(encoding="utf-8"))
    assert saved["transient_network_issue_active"] is True
    assert saved["transient_network_issue_occurrences"] == 1


def test_record_transient_issue_alerts_after_threshold_and_respects_cooldown(monkeypatch, tmp_path: Path) -> None:
    state_file = tmp_path / "state.json"
    state = {"last_message_id": "x"}

    alerts: list[str] = []
    monkeypatch.setattr(gmail_client, "send_discord_alert", lambda _w, message: alerts.append(message) or True)

    now_values = iter([1000.0, 1070.0, 1100.0, 1140.0])
    monkeypatch.setattr(gmail_client.time, "time", lambda: next(now_values))

    first = gmail_client.record_transient_issue(
        state,
        state_file,
        TimeoutError("timed out"),
        webhook_url="https://discord.invalid/webhook",
        alert_message="transient issue",
        min_alert_duration_seconds=60.0,
        alert_cooldown_seconds=60.0,
    )
    second = gmail_client.record_transient_issue(
        state,
        state_file,
        TimeoutError("timed out"),
        webhook_url="https://discord.invalid/webhook",
        alert_message="transient issue",
        min_alert_duration_seconds=60.0,
        alert_cooldown_seconds=60.0,
    )
    third = gmail_client.record_transient_issue(
        state,
        state_file,
        TimeoutError("timed out"),
        webhook_url="https://discord.invalid/webhook",
        alert_message="transient issue",
        min_alert_duration_seconds=60.0,
        alert_cooldown_seconds=60.0,
    )
    fourth = gmail_client.record_transient_issue(
        state,
        state_file,
        TimeoutError("timed out"),
        webhook_url="https://discord.invalid/webhook",
        alert_message="transient issue",
        min_alert_duration_seconds=60.0,
        alert_cooldown_seconds=60.0,
    )

    assert [first, second, third, fourth] == [False, True, False, True]
    assert len(alerts) == 2
    saved = json.loads(state_file.read_text(encoding="utf-8"))
    assert saved["transient_network_issue_occurrences"] == 4
    assert saved["transient_network_issue_notified"] is True


def test_record_transient_issue_clamps_negative_thresholds(monkeypatch, tmp_path: Path) -> None:
    state_file = tmp_path / "state.json"
    state = {"last_message_id": "x"}

    alerts: list[str] = []
    monkeypatch.setattr(gmail_client, "send_discord_alert", lambda _w, message: alerts.append(message) or True)
    monkeypatch.setattr(gmail_client.time, "time", lambda: 1000.0)

    sent = gmail_client.record_transient_issue(
        state,
        state_file,
        TimeoutError("timed out"),
        webhook_url="https://discord.invalid/webhook",
        alert_message="transient issue",
        min_alert_duration_seconds=-1.0,
        alert_cooldown_seconds=-2.0,
    )

    assert sent is True
    assert alerts == ["transient issue"]
    saved = json.loads(state_file.read_text(encoding="utf-8"))
    assert saved["transient_network_issue_notified"] is True


def test_refresh_with_retry_retries_on_transient_and_then_succeeds(monkeypatch) -> None:
    creds = _DummyCreds()
    creds.refresh_outcomes = [TimeoutError("timed out"), None]
    sleeps: list[int] = []
    monkeypatch.setattr(gmail_client.time, "sleep", lambda sec: sleeps.append(sec))

    result = gmail_client.refresh_with_retry(
        creds,
        retries=3,
        base_delay=1,
        request_factory=lambda: object(),
    )
    assert result is None
    assert sleeps == [1]


def test_refresh_with_retry_returns_non_transient_error_without_sleep(monkeypatch) -> None:
    creds = _DummyCreds()
    error = RuntimeError("fatal")
    creds.refresh_outcomes = [error]
    monkeypatch.setattr(gmail_client, "is_transient_network_error", lambda exc: False)

    sleeps: list[int] = []
    monkeypatch.setattr(gmail_client.time, "sleep", lambda sec: sleeps.append(sec))

    result = gmail_client.refresh_with_retry(
        creds,
        retries=3,
        base_delay=1,
        request_factory=lambda: object(),
    )
    assert result is error
    assert sleeps == []


def test_refresh_with_retry_raises_for_invalid_retries() -> None:
    with pytest.raises(ValueError):
        gmail_client.refresh_with_retry(_DummyCreds(), retries=0)


def test_refresh_with_retry_uses_dependency_guard_when_request_factory_missing(monkeypatch) -> None:
    calls: list[bool] = []
    monkeypatch.setattr(gmail_client, "ensure_google_dependencies", lambda: calls.append(True))
    monkeypatch.setattr(gmail_client, "Request", lambda: object())

    result = gmail_client.refresh_with_retry(_DummyCreds(), retries=1)
    assert result is None
    assert calls == [True]


def test_get_gmail_service_refresh_success_writes_token(monkeypatch, tmp_path: Path) -> None:
    paths = _paths_for(tmp_path)
    token_path = paths.token
    token_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(gmail_client, "ensure_google_dependencies", lambda: None)

    creds = _DummyCreds(valid=False, expired=True, refresh_token="r", json_text='{"new":"token"}')
    monkeypatch.setattr(gmail_client.Credentials, "from_authorized_user_file", lambda *_args, **_kwargs: creds)
    monkeypatch.setattr(gmail_client, "refresh_with_retry", lambda *_args, **_kwargs: None)
    service_obj = object()
    captured_kwargs: dict = {}

    def fake_build(*_args, **kwargs):
        captured_kwargs.update(kwargs)
        return service_obj

    monkeypatch.setattr(gmail_client, "build", fake_build)

    service = gmail_client.get_gmail_service(paths=paths)
    assert service is service_obj
    assert token_path.read_text(encoding="utf-8") == '{"new":"token"}'
    assert captured_kwargs["cache_discovery"] is False


def test_get_gmail_service_refresh_transient_error_marks_issue(monkeypatch, tmp_path: Path) -> None:
    paths = _paths_for(tmp_path)
    token_path = paths.token
    token_path.write_text("{}", encoding="utf-8")
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
        paths=paths,
        transient_alert_min_duration_seconds=0.0,
        transient_alert_cooldown_seconds=0.0,
    )
    assert service is None
    assert alerts
    saved = json.loads(state_file.read_text(encoding="utf-8"))
    assert saved["transient_network_issue_active"] is True


def test_get_gmail_service_refresh_fatal_with_interactive_recovers(monkeypatch, tmp_path: Path) -> None:
    paths = _paths_for(tmp_path)
    token_path = paths.token
    token_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(gmail_client, "ensure_google_dependencies", lambda: None)

    creds = _DummyCreds(valid=False, expired=True, refresh_token="r")
    monkeypatch.setattr(gmail_client.Credentials, "from_authorized_user_file", lambda *_args, **_kwargs: creds)
    monkeypatch.setattr(gmail_client, "refresh_with_retry", lambda *_args, **_kwargs: RuntimeError("fatal"))
    monkeypatch.setattr(gmail_client, "run_oauth_flow", lambda *_args, **_kwargs: _DummyCreds(valid=True))
    monkeypatch.setattr(gmail_client, "build", lambda *_args, **_kwargs: object())

    alerts: list[str] = []
    monkeypatch.setattr(gmail_client, "send_discord_alert", lambda webhook_url, message: alerts.append(message) or True)

    service = gmail_client.get_gmail_service(
        webhook_url="https://discord.invalid/webhook",
        allow_oauth_interactive=True,
        paths=paths,
    )
    assert service is not None
    assert alerts


def test_get_gmail_service_invalid_token_without_refresh_token_marks_issue(monkeypatch, tmp_path: Path) -> None:
    paths = _paths_for(tmp_path)
    token_path = paths.token
    token_path.write_text("{}", encoding="utf-8")
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
        paths=paths,
    )
    assert service is None
    assert alerts


def test_get_gmail_service_build_error_paths(monkeypatch, tmp_path: Path) -> None:
    paths = _paths_for(tmp_path)
    token_path = paths.token
    token_path.write_text("{}", encoding="utf-8")
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
        paths=paths,
    )
    assert transient is None
    saved = json.loads(state_file.read_text(encoding="utf-8"))
    assert saved["transient_network_issue_active"] is True

    monkeypatch.setattr(gmail_client, "build", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("fatal")))
    fatal = gmail_client.get_gmail_service(paths=paths)
    assert fatal is None


def test_get_gmail_service_exposes_auth_status(monkeypatch, tmp_path: Path) -> None:
    paths = _paths_for(tmp_path)
    token_path = paths.token
    token_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(gmail_client, "ensure_google_dependencies", lambda: None)
    monkeypatch.setattr(
        gmail_client.Credentials,
        "from_authorized_user_file",
        lambda *_args, **_kwargs: _DummyCreds(valid=True),
    )
    monkeypatch.setattr(gmail_client, "build", lambda *_args, **_kwargs: object())

    service, status = gmail_client.get_gmail_service_with_status(paths=paths)
    assert service is not None
    assert status == AuthStatus.READY


def test_list_and_get_message_helpers() -> None:
    class _Exec:
        def __init__(self, payload: dict):
            self.payload = payload

        def execute(self) -> dict:
            return self.payload

    class _Messages:
        def list(self, *, userId: str, q: str, maxResults: int, pageToken: str | None = None):
            assert userId == "me"
            assert q == "in:inbox"
            assert maxResults == 20
            if pageToken == "page-2":
                return _Exec({"messages": [{"id": "m-2"}], "nextPageToken": None})
            return _Exec({"messages": [{"id": "m-1"}], "nextPageToken": "page-2"})

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
    page_messages, page_token = gmail_client.list_recent_messages_page(
        service,
        query="in:inbox",
        max_results=20,
        page_token="page-2",
    )
    assert page_messages == [{"id": "m-2"}]
    assert page_token is None
    assert gmail_client.get_message_detail(service, "m-1")["snippet"] == "hello"


def test_start_gmail_watch_with_retry_retries_transient_errors(monkeypatch) -> None:
    calls = {"count": 0}

    class _Exec:
        def execute(self):
            calls["count"] += 1
            if calls["count"] == 1:
                raise TimeoutError("timed out")
            return {"historyId": "123"}

    class _Users:
        def watch(self, *, userId: str, body: dict):
            assert userId == "me"
            assert body["topicName"] == "projects/p/topics/t"
            return _Exec()

    class _Service:
        def users(self):
            return _Users()

    sleeps: list[float] = []
    monkeypatch.setattr(gmail_client.time, "sleep", lambda sec: sleeps.append(sec))

    response = gmail_client.start_gmail_watch_with_retry(
        _Service(),
        topic_name="projects/p/topics/t",
        retries=2,
        base_delay=0.5,
        max_delay=10.0,
    )
    assert response["historyId"] == "123"
    assert sleeps == [0.5]


def test_get_gmail_service_uses_explicit_runtime_paths(monkeypatch, tmp_path: Path) -> None:
    token_path = tmp_path / "runtime" / "token.json"
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text("{}", encoding="utf-8")
    runtime_paths = RuntimePaths(
        runtime_dir=token_path.parent,
        config=token_path.parent / "config.json",
        credentials=token_path.parent / "credentials.json",
        token=token_path,
        default_log=token_path.parent / "logs" / "amazon_mail_notifier.log",
    )

    monkeypatch.setattr(gmail_client, "ensure_google_dependencies", lambda: None)
    captured_path: dict[str, str] = {}

    def fake_from_authorized_user_file(path: str, *_args, **_kwargs):
        captured_path["token_path"] = path
        return _DummyCreds(valid=True)

    monkeypatch.setattr(
        gmail_client.Credentials,
        "from_authorized_user_file",
        fake_from_authorized_user_file,
    )
    monkeypatch.setattr(gmail_client, "build", lambda *_args, **_kwargs: object())

    service = gmail_client.get_gmail_service(paths=runtime_paths)
    assert service is not None
    assert captured_path["token_path"] == str(token_path)
