from __future__ import annotations

import re

from starlette.types import ASGIApp, Message, Receive, Scope, Send


class SessionCookiePolicyMiddleware:
    """Remove persistence from session cookies unless the user opted in."""

    def __init__(self, app: ASGIApp, session_cookie: str) -> None:
        self.app = app
        self.cookie_prefix = f"{session_cookie}=".encode("latin-1")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                session = scope.get("session", {})
                if not session.get("remember_me", False):
                    message["headers"] = [
                        (name, self._remove_max_age(value))
                        if name.lower() == b"set-cookie"
                        and value.startswith(self.cookie_prefix)
                        else (name, value)
                        for name, value in message["headers"]
                    ]
            await send(message)

        await self.app(scope, receive, send_wrapper)

    @staticmethod
    def _remove_max_age(value: bytes) -> bytes:
        return re.sub(rb";\s*Max-Age=[^;]*", b"", value, flags=re.IGNORECASE)
