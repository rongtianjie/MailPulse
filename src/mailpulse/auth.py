from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import User
from .security import hash_password, verify_password


def create_user(
    session: Session,
    email: str,
    password: str,
    display_name: str = "",
    role: str = "user",
    must_change_password: bool = False,
) -> User:
    normalized = email.strip().lower()
    if not normalized or "@" not in normalized:
        raise ValueError("请输入有效的邮箱格式账号")
    _validate_password(password)
    if session.scalar(select(User).where(User.email == normalized)):
        raise ValueError("用户账号已存在")
    user = User(
        email=normalized,
        display_name=display_name.strip() or normalized.split("@", 1)[0],
        password_hash=hash_password(password),
        must_change_password=must_change_password,
        role=role,
    )
    session.add(user)
    session.flush()
    return user


def set_password(user: User, password: str, *, must_change_password: bool = False) -> None:
    _validate_password(password)
    user.password_hash = hash_password(password)
    user.must_change_password = must_change_password


def _validate_password(password: str) -> None:
    if len(password) < 8:
        raise ValueError("密码长度至少为 8 个字符")


def authenticate(session: Session, email: str, password: str) -> User | None:
    user = session.scalar(
        select(User).where(User.email == email.strip().lower(), User.is_active.is_(True))
    )
    if not user or not verify_password(password, user.password_hash):
        return None
    return user
