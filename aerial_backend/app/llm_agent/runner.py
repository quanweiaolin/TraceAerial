"""agent主入口：问一句话，跑Tool Runner直到模型给出最终文字回复。

飞书这边长连接/事件处理还没接（需要用户去飞书开放平台申请自建应用的
App ID/Secret，见讨论记录），先提供这一个函数，方便直接单独测试，
以后飞书收到消息后就调这个函数拿回复去发。
"""
from __future__ import annotations

from app.llm_agent.client import get_client, get_model
from app.llm_agent.tools import ALL_TOOLS

# 明确告诉模型"没有对应工具就老实说做不到"——不加这条，模型碰到现有工具
# 覆盖不到的问题时容易凭常识/训练知识编一个看起来合理但其实是瞎猜的答案
# (这个系统面向的是数据库/运维事实性问题，编答案比"不知道"危害大得多)。
SYSTEM_PROMPT = (
    "你是航测数据管理系统(TraceAerial)的运维问答助手，服务对象是团队内部"
    "群聊里的人。只能用提供的工具查询数据库/系统状态来回答问题，不要凭"
    "训练知识或常识编造数据库里具体有什么数据。如果问题没有任何对应的"
    "工具可以查(比如涉及工具列表之外的数据)，直接明确说"
    "\"目前没有查询这个的能力\"，不要猜测或编造一个答案。"
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
