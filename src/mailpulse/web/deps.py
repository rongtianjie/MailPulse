from __future__ import annotations

from collections.abc import Generator

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from ..db import build_session_factory
from ..models import User


def get_db(request: Request) -> Generator[Session, None, None]:
    factory = getattr(request.app.state, "session_factory", None) or build_session_factory()
    session = factory()
    try:
        yield session
    finally:
        session.close()


def authenticated_user(request: Request, db: Session = Depends(get_db)) -> User:
    user_id = request.session.get("user_id")
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_303_SEE_OTHER,
            headers={"Location": "/login"},
        )
    user = db.get(User, int(user_id))
    if not user or not user.is_active:
        request.session.clear()
        raise HTTPException(
            status_code=status.HTTP_303_SEE_OTHER,
            headers={"Location": "/login"},
        )
    # The session is client-side. Bind it to this concrete user record so a
    # reset database cannot accidentally reuse an old cookie for a new user
    # with the same numeric primary key.
    if request.session.get("user_created_at") != user.created_at.isoformat():
        request.session.clear()
        raise HTTPException(
            status_code=status.HTTP_303_SEE_OTHER,
            headers={"Location": "/login"},
        )
    expected_mode = "admin" if user.role == "admin" else "user"
    login_mode = request.session.get("login_mode")
    if login_mode is None:
        # Keep sessions created before the dual-mode login change usable.
        request.session["login_mode"] = expected_mode
    elif login_mode != expected_mode:
        request.session.clear()
        raise HTTPException(
            status_code=status.HTTP_303_SEE_OTHER,
            headers={"Location": "/login"},
        )
    return user


def current_user(user: User = Depends(authenticated_user)) -> User:
    """Return a regular user for the user-facing application routes."""
    if user.role != "user":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="管理员账号不能访问用户工作台"
        )
    return user


def admin_user(user: User = Depends(authenticated_user)) -> User:
    if user.role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="需要管理员权限")
    return user
