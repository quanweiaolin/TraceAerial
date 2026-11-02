"""飞书LLM问答agent，在aerial_backend进程内跑(不是独立服务)。

调用Anthropic SDK(当前Provider是MiniMax的Anthropic兼容端点，见
client.py)，用Tool Runner做agentic loop，工具都是我们自己的只读查询
函数(tools/queries.py)。

架构上预留了4类场景(2026-11-02确定)，目前只实现了第1类：

1. 日常查询——已实现。把现有查询脚本/接口包装成@beta_async_tool函数，
   见tools/queries.py。
2. 可执行命令类工具(比如触发重新下载、重启某个后台任务)——暂不实现
   任何这类工具，但权限分组这道口子先留好，见permissions.py：以后
   加这类工具时，执行前先查is_privileged()，不是等真的要加的时候
   才设计分组，避免那时候要回头改已经写好的工具注册逻辑。
3. 自动巡检(agent主动发现/处理运维问题，不只是现有scanner/aliyun_sync
   那种固定规则巡检)——暂无需求，不占位实现，只是不让上面的结构挡路。
4. 日志诊断(agent读日志帮着定位问题)——同3，暂无需求。
"""
