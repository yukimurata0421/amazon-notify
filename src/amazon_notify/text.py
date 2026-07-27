import re
from email.header import decode_header, make_header
from re import Pattern


def decode_mime_words(value: str) -> str:
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def extract_email_address(value: str) -> str:
    decoded = decode_mime_words(value)
    match = re.search(
        r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}",
        decoded,
    )
    return match.group(0) if match else decoded


def is_amazon_mail(from_header: str, pattern: Pattern[str]) -> bool:
    email = extract_email_address(from_header).lower()
    return pattern.search(email) is not None


def build_gmail_message_url(message_id: str) -> str:
    return f"https://mail.google.com/mail/u/0/#inbox/{message_id}"
