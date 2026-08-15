from __future__ import annotations

MAX_ERROR_DETAIL = 300


def error_message(exc: Exception, context: str = "") -> str:
    """Build a user-facing failure message with the real cause, truncated.

    The exception detail is single-line normalized and capped so credentials or
    long server dumps cannot leak into status fields.
    """
    prefix = type(exc).__name__
    if context:
        prefix = f"{prefix}: {context}"
    detail = " ".join(str(exc).split())[:MAX_ERROR_DETAIL]
    return f"{prefix}：{detail}" if detail else prefix
