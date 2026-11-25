"""地址/视频地址字段变更的审计记录写入。

只负责`session.add`一条`LocationChangeHistory`记录，不自己commit——
事务边界由调用方决定(项目是session-per-request模式，见app/db.py的
get_session)，这里commit了反而会提前结束调用方还没写完的事务。"""
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import LocationChangeHistory


def record_change(
    session: AsyncSession,
    *,
    entity_type: str,
    entity_id: int,
    field_name: str,
    old_value: str | None,
    new_value: str | None,
    changed_by: int | None,
    reason: str | None = None,
) -> None:
    session.add(
        LocationChangeHistory(
            entity_type=entity_type,
            entity_id=entity_id,
            field_name=field_name,
            old_value=old_value,
            new_value=new_value,
            changed_by=changed_by,
            reason=reason,
        )
    )
