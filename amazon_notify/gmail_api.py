from __future__ import annotations

from typing import Any


def start_gmail_watch(
    service: Any,
    *,
    topic_name: str,
    label_ids: list[str] | None = None,
    label_filter_action: str = "include",
) -> dict[str, Any]:
    action = label_filter_action.strip().lower()
    if action not in {"include", "exclude"}:
        raise ValueError("label_filter_action must be 'include' or 'exclude'")

    body: dict[str, Any] = {"topicName": topic_name}
    if label_ids:
        body["labelIds"] = label_ids
        body["labelFilterAction"] = action.upper()

    return service.users().watch(userId="me", body=body).execute()


def list_recent_messages_page(
    service: Any,
    *,
    query: str,
    max_results: int,
    page_token: str | None = None,
) -> tuple[list[dict[str, str]], str | None]:
    request_payload: dict[str, Any] = {
        "userId": "me",
        "q": query,
        "maxResults": max_results,
    }
    if page_token:
        request_payload["pageToken"] = page_token
    result = service.users().messages().list(**request_payload).execute()
    return result.get("messages", []), result.get("nextPageToken")


def list_recent_messages(
    service: Any, query: str, max_results: int
) -> list[dict[str, str]]:
    messages, _next_page_token = list_recent_messages_page(
        service,
        query=query,
        max_results=max_results,
    )
    return messages


def get_message_detail(service: Any, message_id: str) -> dict[str, Any]:
    return (
        service.users()
        .messages()
        .get(
            userId="me",
            id=message_id,
            format="full",
        )
        .execute()
    )
