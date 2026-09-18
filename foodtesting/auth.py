"""角色与令牌：监管、实验室、商户三类内部角色，公众查询无需令牌。"""

from __future__ import annotations

from typing import Optional

from .models import User

SEED_USERS = [
    User(id="REG-01", name="王执法", role="regulator", token="token-regulator", org="市市场监管局"),
    User(id="LAB-01", name="实验室甲", role="lab", token="token-lab-a", org="实验室甲"),
    User(id="LAB-02", name="实验室乙", role="lab", token="token-lab-b", org="实验室乙"),
    User(id="MCH-01", name="桂香斋", role="merchant", token="token-merchant-1", org="桂香斋"),
    User(id="MCH-02", name="月满楼", role="merchant", token="token-merchant-2", org="月满楼"),
]


def seed_users(store) -> None:
    for user in SEED_USERS:
        store.users[user.id] = user
        store.users_by_token[user.token] = user


def authenticate(store, token: Optional[str]) -> Optional[User]:
    if not token:
        return None
    return store.users_by_token.get(token)
