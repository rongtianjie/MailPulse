from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import User
from .security import hash_password, verify_password

USERNAME_PATTERN = re.compile(r"^[a-z0-9._-]{3,32}$")


def normalize_username(username: str) -> str:
    return username.strip().lower()


def validate_username(username: str) -> None:
    if not USERNAME_PATTERN.fullmatch(normalize_username(username)):
        raise ValueError("用户名仅支持 3-32 位字母、数字、下划线、连字符或点")


def create_user(
    session: Session,
    username: str,
    password: str,
    display_name: str = "",
    email: str | None = None,
    role: str = "user",
    must_change_password: bool = False,
) -> User:
    normalized = normalize_username(username)
    validate_username(normalized)
    _validate_password(password)
    if session.scalar(select(User).where(User.username == normalized)):
        raise ValueError("用户名已存在")
    normalized_email = None
    if email:
        normalized_email = email.strip().lower()
        if "@" not in normalized_email:
            raise ValueError("请输入有效的邮箱格式")
    user = User(
        username=normalized,
        email=normalized_email,
        display_name=display_name.strip() or normalized,
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


def authenticate(session: Session, username: str, password: str) -> User | None:
    user = session.scalar(
        select(User)
        .where(User.username == normalize_username(username), User.is_active.is_(True))
    )
    if not user or not verify_password(password, user.password_hash):
        return None
    return user
