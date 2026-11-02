"""agent主入口：问一句话，跑Tool Runner直到模型给出最终文字回复。

飞书长连接(app/services/feishu_bot.py)收到消息后就调这个函数拿回复发
回去；也可以脱离飞书单独测试。
"""
from __future__ import annotations

from app.llm_agent.client import get_client, get_model
from app.llm_agent.tools import ALL_TOOLS

# 明确告诉模型"没有对应工具就老实说做不到"——不加这条，模型碰到现有工具
# 覆盖不到的问题时容易凭常识/训练知识编一个看起来合理但其实是瞎猜的答案
# (这个系统面向的是数据库/运维事实性问题，编答案比"不知道"危害大得多)。
#
# "下载成功"的定义(2026-11-02用户明确要求)：list_folders_ready返回的
# 文件夹里，只有is_stable=true的才算"成功"——is_stable=false说明判定
# 完成之后又出现过问题(重新上传/文件被删/云端整体消失等)，这类应该归到
# "有问题的"(list_recent_issues)而不是报成"成功"，不能因为它曾经被判定
# 完成过就算数。
SYSTEM_PROMPT = (
    "你是航测数据管理系统(TraceAerial)的运维问答助手，服务对象是团队内部"
    "群聊里的人。只能用提供的工具查询数据库/系统状态来回答问题，不要凭"
    "训练知识或常识编造数据库里具体有什么数据。如果问题没有任何对应的"
    "工具可以查(比如涉及工具列表之外的数据)，直接明确说"
    "\"目前没有查询这个的能力\"，不要猜测或编造一个答案。\n\n"
    "关于list_folders_ready返回结果的解读：被问\"下载成功/完成的有哪些\""
    "时，只报is_stable=true的那些；is_stable=false的不算成功，应该提示"
    "用户这些判定完成后又出现过问题，建议改用\"最近有问题的文件夹\"这个"
    "角度去看（对应list_recent_issues），不要把它们和真正成功的混在一张"
    "表里报出来。"
)


async def ask(question: str) -> str:
    """问一句话，返回agent最终的文字回复。"""
    client = get_client()
    runner = client.beta.messages.tool_runner(
        model=get_model(),
        max_tokens=2000,
        system=SYSTEM_PROMPT,
        thread=False,  # 见client.py: MiniMax不支持服务端message threads
        tools=ALL_TOOLS,
        messages=[{"role": "user", "content": question}],
    )

    last_text = ""
    async for message in runner:
        text_blocks = [block.text for block in message.content if block.type == "text"]
        if text_blocks:
            last_text = "\n".join(text_blocks)
    return last_text
