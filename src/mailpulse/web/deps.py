from __future__ import annotations

from collections.abc import Generator

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from ..db import build_session_factory
from ..models import User


def get_db() -> Generator[Session, None, None]:
    session = build_session_factory()()
    try:
        yield session
    finally:
        session.close()


def authenticated_user(request: Request, db: Session = Depends(get_db)) -> User:
    user_id = request.session.get("user_id")
    if not user_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="请先登录")
    user = db.get(User, int(user_id))
    if not user or not user.is_active:
        request.session.clear()
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="账号不可用")
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
