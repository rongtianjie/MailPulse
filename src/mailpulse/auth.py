from __future__ import annotations

import re

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from .models import User
from .security import hash_password, verify_password

USERNAME_PATTERN = re.compile(r"^[a-z0-9._-]{3,32}$")
INTERNAL_USER_PREFIX = "__admin_user_"


def normalize_username(username: str) -> str:
    return username.strip().lower()


def validate_username(username: str) -> None:
    normalized = normalize_username(username)
    if normalized.startswith(INTERNAL_USER_PREFIX):
        raise ValueError("该用户名保留给系统内部身份使用")
    if not USERNAME_PATTERN.fullmatch(normalized):
        raise ValueError("用户名仅支持 3-32 位字母、数字、下划线、连字符或点")


def create_user(
    session: Session,
    username: str,
    password: str,
    display_name: str = "",
    role: str = "user",
    must_change_password: bool = False,
) -> User:
    normalized = normalize_username(username)
    validate_username(normalized)
    _validate_password(password)
    if session.scalar(select(User).where(User.username == normalized)):
        raise ValueError("用户名已存在")
    user = User(
        username=normalized,
        display_name=display_name.strip() or normalized,
        password_hash=hash_password(password),
        must_change_password=must_change_password,
        role=role,
    )
    session.add(user)
    session.flush()
    if role == "admin":
        ensure_user_mode_identity(session, user)
    return user


def ensure_user_mode_identity(session: Session, admin_user: User) -> User:
    """Create or repair the private user identity paired with an administrator."""
    if admin_user.role != "admin":
        raise ValueError("只有管理员账号可以创建用户模式身份")
    if admin_user.paired_user is not None and admin_user.paired_user.role == "user":
        return admin_user.paired_user
    paired = session.scalar(
        select(User).where(User.paired_user_id == admin_user.id, User.role == "user")
    )
    if paired is None:
        paired = User(
            username=f"{INTERNAL_USER_PREFIX}{admin_user.id}",
            display_name=admin_user.display_name,
            password_hash=admin_user.password_hash,
            must_change_password=admin_user.must_change_password,
            role="user",
            paired_user_id=admin_user.id,
        )
        session.add(paired)
        session.flush()
    admin_user.paired_user_id = paired.id
    admin_user.paired_user = paired
    session.flush()
    return paired


def set_password(user: User, password: str, *, must_change_password: bool = False) -> None:
    _validate_password(password)
    password_hash = hash_password(password)
    identities = [user]
    if user.paired_user is not None:
        identities.append(user.paired_user)
    for identity in identities:
        identity.password_hash = password_hash
        identity.must_change_password = must_change_password


def _validate_password(password: str) -> None:
    if len(password) < 8:
        raise ValueError("密码长度至少为 8 个字符")


def authenticate(session: Session, username: str, password: str) -> User | None:
    user = session.scalar(
        select(User)
        .where(
            User.username == normalize_username(username),
            User.is_active.is_(True),
            or_(User.role == "admin", User.paired_user_id.is_(None)),
        )
    )
    if not user or not verify_password(password, user.password_hash):
        return None
    return user
