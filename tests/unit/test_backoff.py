from amazon_notify import backoff


def test_next_delay_seconds_uses_exponential_backoff_with_cap() -> None:
    assert backoff.next_delay_seconds(1, base_delay=1.0, max_delay=30.0) == 1.0
    assert backoff.next_delay_seconds(2, base_delay=1.0, max_delay=30.0) == 2.0
    assert backoff.next_delay_seconds(5, base_delay=1.0, max_delay=8.0) == 8.0


def test_parse_retry_after_seconds_from_seconds_and_invalid_value() -> None:
    assert backoff.parse_retry_after_seconds(None) is None
    assert backoff.parse_retry_after_seconds("   ") is None
    assert backoff.parse_retry_after_seconds("5") == 5.0
    assert backoff.parse_retry_after_seconds("invalid") is None


def test_next_delay_seconds_raises_for_invalid_arguments() -> None:
    try:
        backoff.next_delay_seconds(0, base_delay=1.0, max_delay=10.0)
        raise AssertionError("expected ValueError")
    except ValueError:
        pass
    try:
        backoff.next_delay_seconds(1, base_delay=0.0, max_delay=10.0)
        raise AssertionError("expected ValueError")
    except ValueError:
        pass
    try:
        backoff.next_delay_seconds(1, base_delay=1.0, max_delay=0.0)
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


def test_parse_retry_after_seconds_from_http_date() -> None:
    # Future date to avoid timezone/timing sensitivity.
    assert backoff.parse_retry_after_seconds("Wed, 21 Oct 2099 07:28:00 GMT") is not None
    assert backoff.parse_retry_after_seconds("Wed, 21 Oct 2099 07:28:00") is not None
