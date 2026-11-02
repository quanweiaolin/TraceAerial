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
# "下载成功"只算completed_stable——2026-11-02发现工具原来返回一个带
# is_stable标志的扁平列表时，模型会在生成文字的同时自己现算"哪些算
# 成功"，而且真的算错过一次(事后自己改口)。已经把这个分类挪到工具里
# 做(get_sync_status_report直接返回completed_stable/completed_unstable
# 两个分好的列表)，这里只是再提一句，别让模型碰到completed_unstable时
# 又自作主张把它们也算进"成功"。
SYSTEM_PROMPT = (
    "你是航测数据管理系统(TraceAerial)的运维问答助手，服务对象是团队内部"
    "群聊里的人。只能用提供的工具查询数据库/系统状态来回答问题，不要凭"
    "训练知识或常识编造数据库里具体有什么数据。如果问题没有任何对应的"
    "工具可以查(比如涉及工具列表之外的数据)，直接明确说"
    "\"目前没有查询这个的能力\"，不要猜测或编造一个答案。\n\n"
    "关于get_sync_status_report返回结果的解读：被问\"下载成功/完成的有"
    "哪些\"时，只报completed_stable里的内容；completed_unstable里的不算"
    "成功，不要混进同一张表里报，也不要自己重新判断分类——这两个列表"
    "已经分好了，照抄就行。"
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
