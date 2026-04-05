import re

import pytest

from amazon_notify import text


@pytest.mark.parametrize(
    ("from_header", "expected"),
    [
        ("Amazon.co.jp <order-update@amazon.co.jp>", "order-update@amazon.co.jp"),
        ("\"Amazon.co.jp\" <shipment-tracking@amazon.co.jp>", "shipment-tracking@amazon.co.jp"),
        ("=?UTF-8?B?QW1hem9uLmNvLmpw?= <auto-confirm@amazon.co.jp>", "auto-confirm@amazon.co.jp"),
        ("Amazon Notifications <order+test@amazon.co.jp>", "order+test@amazon.co.jp"),
    ],
)
def test_extract_email_address_handles_multiple_formats(from_header: str, expected: str) -> None:
    assert text.extract_email_address(from_header) == expected


@pytest.mark.parametrize(
    ("from_header", "pattern", "expected"),
    [
        ("Amazon.co.jp <order-update@amazon.co.jp>", r"amazon\.co\.jp$", True),
        ("no-reply@example.com", r"amazon\.co\.jp$", False),
    ],
)
def test_is_amazon_mail_regression_cases(from_header: str, pattern: str, expected: bool) -> None:
    assert text.is_amazon_mail(from_header, re.compile(pattern)) is expected
