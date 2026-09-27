"""Parse callback data and persisted group setup drafts."""

from __future__ import annotations

import uuid
from typing import Any

from tg_jobs_searcher.services.keywords import KeywordInput, parse_keyword_input
from tg_jobs_searcher.telegram.client import ResolvedGroup


def _callback_uuid(value: str | None, prefix: str) -> uuid.UUID | None:
    if value is None or not value.startswith(prefix):
        return None
    try:
        return uuid.UUID(hex=value.removeprefix(prefix))
    except ValueError:
        return None


def _draft_uuid(draft: dict[str, Any], key: str) -> uuid.UUID | None:
    value = draft.get(key)
    if not isinstance(value, str):
        return None
    try:
        return uuid.UUID(hex=value)
    except ValueError:
        return None


def _parse_mode_callback(value: str | None) -> tuple[str, str] | None:
    if value is None:
        return None
    parts = value.split(":")
    if len(parts) != 4 or parts[:2] != ["setup", "mode"] or parts[3] not in {"history", "new"}:
        return None
    nonce = parts[2]
    if len(nonce) != 32 or not nonce.isalnum():
        return None
    return nonce, parts[3]


def _resolved_group_from_draft(draft: dict[str, Any]) -> ResolvedGroup:
    group = draft["group"]
    if not isinstance(group, dict):
        raise ValueError("Invalid group draft")
    chat_id = group["telegram_chat_id"]
    title = group["title"]
    username = group.get("username")
    topic_id = group.get("topic_id")
    topic_title = group.get("topic_title")
    if not isinstance(chat_id, int) or not isinstance(title, str):
        raise ValueError("Invalid group draft")
    if username is not None and not isinstance(username, str):
        raise ValueError("Invalid group draft")
    if topic_id is not None and (not isinstance(topic_id, int) or topic_id <= 0):
        raise ValueError("Invalid group draft")
    if topic_title is not None and not isinstance(topic_title, str):
        raise ValueError("Invalid group draft")
    return ResolvedGroup(chat_id, title, username, topic_id, topic_title)


def _keywords_from_draft(draft: dict[str, Any]) -> list[KeywordInput]:
    raw_keywords = draft["keywords"]
    if not isinstance(raw_keywords, list):
        raise ValueError("Invalid keyword draft")
    keywords: list[KeywordInput] = []
    for item in raw_keywords:
        if not isinstance(item, dict) or not isinstance(item.get("value"), str):
            raise ValueError("Invalid keyword draft")
        if "terms" not in item:
            normalized_value = item.get("normalized_value")
            if not isinstance(normalized_value, str) or not normalized_value:
                raise ValueError("Invalid keyword draft")
            keywords.append(
                KeywordInput(
                    value=item["value"],
                    normalized_value=normalized_value,
                    terms=(normalized_value,),
                )
            )
            continue
        parsed = parse_keyword_input(item["value"])
        if len(parsed) != 1 or list(parsed[0].terms) != item["terms"]:
            raise ValueError("Invalid keyword draft")
        keywords.append(parsed[0])
    if not keywords:
        raise ValueError("Invalid keyword draft")
    return keywords
