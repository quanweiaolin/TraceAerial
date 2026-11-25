"""地址复核白名单鉴权：operator凭一个长期token（不是登录态session）调用
location_review相关接口。token明文只在issue_token调用的那一刻存在并返回给
调用者，DB里(OperatorApiToken.token_hash)只存它的sha256摘要，即使DB泄露
也不能反推出明文token。

require_location_reviewer校验失败的几种原因（header缺失/格式不对、token
不存在、token被撤销、operator没有can_review_location权限）统一返回同一个
401错误信息，不区分具体原因，避免给攻击者分辨信息的机会。
"""
from __future__ import annotations

import hashlib
import secrets

from fastapi import Depends, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import Operator, OperatorApiToken

_AUTH_ERROR = "无效或无权限的token"


def hash_token(raw: str) -> str:
    """token明文的sha256摘要(hex)，DB里只存这个，不存明文。"""
    return hashlib.sha256(raw.encode()).hexdigest()


async def issue_token(session: AsyncSession, operator_id: int) -> str:
    """生成一个新token，写入token_hash，返回明文——这个明文只在这一次
    调用里存在，之后无法从DB反查出来，调用方要自己负责展示/保存。"""
    raw = secrets.token_urlsafe(32)
    session.add(OperatorApiToken(operator_id=operator_id, token_hash=hash_token(raw)))
    return raw


async def require_location_reviewer(
    authorization: str | None = Header(None),
    session: AsyncSession = Depends(get_session),
) -> Operator:
    """FastAPI依赖：解析`Authorization: Bearer <token>`，校验token有效
    (存在+未撤销)且关联operator有can_review_location权限，否则统一抛
    401 + 同一个错误信息。"""
    if authorization is None or not authorization.startswith("Bearer "):
        raise HTTPException(401, _AUTH_ERROR)

    raw_token = authorization[len("Bearer "):].strip()
    if not raw_token:
        raise HTTPException(401, _AUTH_ERROR)

    result = await session.execute(
        select(OperatorApiToken).where(
            OperatorApiToken.token_hash == hash_token(raw_token),
            OperatorApiToken.revoked_at.is_(None),
        )
    )
    api_token = result.scalars().first()
    if api_token is None:
        raise HTTPException(401, _AUTH_ERROR)

    operator = await session.get(Operator, api_token.operator_id)
    if operator is None or not operator.can_review_location:
        raise HTTPException(401, _AUTH_ERROR)

    return operator
