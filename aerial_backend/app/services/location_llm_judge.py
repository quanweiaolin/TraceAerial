"""LLM地址判断(影子模式) + 人工一致率统计。

judge_async用Claude看两张截图(复核视频 vs 候选地址代表截图)判断"是不是
同一个地方"，结果只存进LocationReviewItem.llm_verdict，不参与自动决策
——纯粹是为了日后评估"LLM判断跟人工复核结果一致到什么程度"，所以调用
失败/超时/解析失败都必须原地捕获、返回一个占位结果，绝不向上抛异常
(见app/llm_agent/client.py: get_client在没配ANTHROPIC_AUTH_TOKEN时会
RuntimeError，这里也要接住，不能让复核队列主流程因为LLM这个旁路挂掉)。

JSON解析沿用app/llm_agent/runner.py里ask()解析Claude返回内容的同一套
做法：从message.content里挑出block.type == "text"的部分、拼起来再处理
(runner.py是直接拿去回复人；这里是拿这段文本再做json.loads)，不另外
发明一套从message对象取文本的逻辑。

compute_agreement_rate统计人工复核结果跟LLM旁路判断的一致率，用来判断
要不要开始考虑让LLM判断参与自动化("ready_for_automation")。
"""
from __future__ import annotations

import base64
import json
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.llm_agent.client import get_client, get_model
from app.models import LocationChangeHistory, LocationReviewItem
from app.services.hashing import sha256_bytes_to_hex

JUDGE_PROMPT = (
    "你会看到两张无人机航拍画面的截图：第一张是待复核视频的截图，第二张"
    "是某个候选地址当前的代表截图。请判断这两张截图拍的是不是同一个物理"
    "地点(不要求完全同一帧/同一时刻，只看地貌、建筑、道路等能代表地点"
    "本身的特征)。\n\n"
    "只返回一个JSON对象，不要有任何其它文字、不要用markdown代码块包"
    "裹，格式严格如下：\n"
    '{"same_location": true或false, "confidence": 0到1之间的小数, '
    '"reasoning": "一两句话说明判断依据"}'
)


def _encode_image_base64(path: str) -> str:
    return base64.b64encode(Path(path).read_bytes()).decode("ascii")


async def judge_async(
    item: LocationReviewItem, frame_a_path: str, frame_b_path: str
) -> dict:
    """调Claude看两张截图判断是否同一地址，纯影子模式——结果不参与任何
    自动决策，所以这里把一切失败都兜住，绝不向上抛异常(网络/API报错/
    返回内容解析失败都一样处理)，否则会卡住复核队列的主流程。"""
    try:
        frame_a_b64 = _encode_image_base64(frame_a_path)
        frame_b_b64 = _encode_image_base64(frame_b_path)

        client = get_client()
        message = await client.messages.create(
            model=get_model(),
            max_tokens=512,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/jpeg",
                                "data": frame_a_b64,
                            },
                        },
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/jpeg",
                                "data": frame_b_b64,
                            },
                        },
                        {"type": "text", "text": JUDGE_PROMPT},
                    ],
                }
            ],
        )

        # 跟app/llm_agent/runner.py的ask()同一套做法：从message.content里
        # 挑出block.type == "text"的部分拼起来，不发明另一套取文本逻辑。
        text_blocks = [block.text for block in message.content if block.type == "text"]
        raw_text = "\n".join(text_blocks).strip()

        # 模型偶尔还是会用```json包一层，容错剥掉再解析。
        if raw_text.startswith("```"):
            raw_text = raw_text.strip("`")
            if raw_text.startswith("json"):
                raw_text = raw_text[len("json"):]
            raw_text = raw_text.strip()

        parsed = json.loads(raw_text)
        return {
            "same_location": parsed.get("same_location"),
            "confidence": float(parsed.get("confidence", 0.0)),
            "reasoning": parsed.get("reasoning", ""),
        }
    except Exception as exc:  # noqa: BLE001 - 旁路判断，任何失败都不能向上抛
        return {
            "same_location": None,
            "confidence": 0.0,
            "reasoning": f"调用失败: {exc}",
        }


async def compute_agreement_rate(
    session: AsyncSession, *, exclude_corrected: bool = True
) -> dict:
    """统计所有status="decided"且llm_verdict不为空的复核条目，LLM旁路
    判断(llm_verdict.same_location)跟人工决策(human_decision==
    "same_location")是否一致。

    exclude_corrected为真时，跳过"这条视频的地址分配后来被人工用新决策
    覆盖过"的条目——判断方式：同一video_sha256在location_change_history
    里针对field_name="location_id"存在不止一条记录，说明location_id被
    改动过不止一次(apply_human_decision只在location_id真的变化时才记一
    条，见app/services/location_review.py)，即最初那次决策后来被纠正
    了，不该拿来评估LLM跟"第一次人工决策"的一致率。
    """
    stmt = select(LocationReviewItem).where(
        LocationReviewItem.status == "decided",
        LocationReviewItem.llm_verdict.isnot(None),
    )
    items = (await session.execute(stmt)).scalars().all()

    corrected_video_hexes: set[str] = set()
    if exclude_corrected:
        history_stmt = (
            select(LocationChangeHistory.entity_id)
            .where(
                LocationChangeHistory.entity_type == "video",
                LocationChangeHistory.field_name == "location_id",
            )
            .group_by(LocationChangeHistory.entity_id)
            .having(func.count() > 1)
        )
        corrected_video_hexes = set(
            (await session.execute(history_stmt)).scalars().all()
        )

    total = 0
    agreed = 0
    disagreed = 0
    for item in items:
        if exclude_corrected:
            video_hex = sha256_bytes_to_hex(item.video_sha256)
            if video_hex in corrected_video_hexes:
                continue

        llm_same = item.llm_verdict.get("same_location")
        human_same = item.human_decision == "same_location"

        total += 1
        if llm_same == human_same:
            agreed += 1
        else:
            disagreed += 1

    return {
        "total": total,
        "agreed": agreed,
        "disagreed": disagreed,
        "ready_for_automation": total >= 50 and disagreed == 0,
    }
