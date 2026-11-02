"""权限分组骨架，给以后"可执行命令"类工具(场景2)用的。

目前agent里一个mutating工具都没有(只有只读查询)，所以这个模块暂时不会
被任何地方调用。提前把这道口子留好，是因为权限模型如果等真的要加第一个
mutating工具时才设计，容易为了赶那一个工具而设计得过窄；现在先定好形状
(按飞书群/用户分组判断)，以后加工具时只要在工具函数开头查一下
is_privileged()。

当前两个集合都留空 = 没有任何群/用户能跑mutating工具(因为确实还没有
mutating工具)。真正开放给哪些群/人，等场景2要落地时再跟用户确认。
"""
from __future__ import annotations

PRIVILEGED_CHAT_IDS: set[str] = set()
PRIVILEGED_USER_IDS: set[str] = set()


def is_privileged(chat_id: str, user_id: str) -> bool:
    """这条消息所在的群/发送者本人，是否有权限触发会改变系统状态的工具
    (比如触发重新下载、重启某个后台任务)。只读查询不需要过这道检查。"""
    return chat_id in PRIVILEGED_CHAT_IDS or user_id in PRIVILEGED_USER_IDS
