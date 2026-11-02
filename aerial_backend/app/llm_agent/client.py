"""Claude API客户端(当前Provider: MiniMax的Anthropic兼容端点)。

auth_token/base_url/model都从app.config.settings读(对应.env里的
ANTHROPIC_AUTH_TOKEN/ANTHROPIC_BASE_URL/ANTHROPIC_MODEL，具体怎么映射见
config.py)，不依赖Anthropic SDK自己探测os.environ——那种探测只在SDK
自己所在的进程环境里才生效，换一种启动方式(systemd EnvironmentFile vs
直接uvicorn/uv run)能不能读到不确定，走settings跟项目其它配置项路径
一致、更可控。

2026-11-02实测确认：MiniMax的兼容端点不支持Tool Runner默认依赖的
"message threads" beta(服务端维护对话状态)，工具调用会报
`tool result's tool id not found`。runner.py里固定加thread=False，
让Tool Runner每轮重发完整消息历史(classic模式)，这个问题才会消失。
换成真正的Anthropic官方API之后这个限制应该不需要了，但thread=False
本身无害，先不急着去掉。
"""
from __future__ import annotations

from functools import lru_cache

from anthropic import AsyncAnthropic

from app.config import settings


@lru_cache(maxsize=1)
def get_client() -> AsyncAnthropic:
    if not settings.anthropic_auth_token:
        raise RuntimeError("ANTHROPIC_AUTH_TOKEN未配置，无法创建Claude API客户端")
    return AsyncAnthropic(
        auth_token=settings.anthropic_auth_token,
        base_url=settings.anthropic_base_url or None,
    )


def get_model() -> str:
    if not settings.anthropic_model:
        raise RuntimeError("ANTHROPIC_MODEL未配置，无法确定用哪个模型")
    return settings.anthropic_model
