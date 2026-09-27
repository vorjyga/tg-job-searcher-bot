"""Telegram forum topic identity for incoming and historical messages."""


def message_topic_id(message: object) -> int | None:
    reply = getattr(message, "reply_to", None)
    if reply is not None:
        top_id = getattr(reply, "reply_to_top_id", None)
        if top_id is not None:
            return top_id
        if getattr(reply, "forum_topic", False):
            return getattr(reply, "reply_to_msg_id", None)
    return getattr(message, "id", None)


def message_in_topic(message: object, topic_id: int) -> bool:
    return message_topic_id(message) == topic_id
