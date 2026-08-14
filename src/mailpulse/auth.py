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
) -> User:
    normalized = email.strip().lower()
    if not normalized or "@" not in normalized:
        raise ValueError("请输入有效的邮箱格式账号")
    if len(password) < 8:
        raise ValueError("密码长度至少为 8 个字符")
    if session.scalar(select(User).where(User.email == normalized)):
        raise ValueError("用户账号已存在")
    user = User(
        email=normalized,
        display_name=display_name.strip() or normalized.split("@", 1)[0],
        password_hash=hash_password(password),
        role=role,
    )
    session.add(user)
    session.flush()
    return user


def authenticate(session: Session, email: str, password: str) -> User | None:
    user = session.scalar(
        select(User).where(User.email == email.strip().lower(), User.is_active.is_(True))
    )
    if not user or not verify_password(password, user.password_hash):
        return None
    return user
