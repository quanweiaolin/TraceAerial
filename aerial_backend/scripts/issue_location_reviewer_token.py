"""给某个operator发一个地址复核白名单的长期token。

明文token只在这次脚本运行时打印一次，之后DB里只存它的sha256摘要，
再也看不到明文——如果忘了记下来，只能重新跑一次发一个新token（旧的
那个明文也就没人知道了，但它本身还在DB里是合法token，直到被撤销）。

用法(在aerial_backend目录下)：
    uv run python scripts/issue_location_reviewer_token.py --operator-username someone
"""
import argparse
import asyncio

from sqlalchemy import select

from app.db import SessionContext
from app.models import Operator
from app.services.location_auth import issue_token


async def main(operator_username: str) -> None:
    async with SessionContext() as session:
        result = await session.execute(
            select(Operator).where(Operator.username == operator_username)
        )
        operator = result.scalars().first()
        if operator is None:
            print(f"未找到username={operator_username}的operator")
            return

        raw_token = await issue_token(session, operator.id)
        await session.commit()

        print(f"已为operator {operator.username}(id={operator.id})发放token：")
        print(raw_token)
        print("这是唯一一次能看到明文的机会，请立刻保存，之后无法再次查看。")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--operator-username", required=True)
    args = parser.parse_args()
    asyncio.run(main(args.operator_username))
