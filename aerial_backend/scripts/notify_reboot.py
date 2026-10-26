"""开机通知脚本，配合systemd的server-reboot-notify.service使用。

服务器重启后，pds-expansion2-shared.service(bindfs挂载)和aerial_backend
服务本身都不会自动拉起来(2026-10-26讨论：故意不自动拉aerial_backend，
因为经常要手动git pull之后再重启，不想被systemd抢着自动起一个旧代码的
实例)。这个脚本的职责仅限于"告诉人服务器重启了，该去处理了"：检查一下
Expansion2-shared这个共享盘挂没挂上，发一条飞书消息，提醒手动git pull+
重启aerial_backend。

挂载检测带了几次重试：pds-expansion2-shared.service是Type=simple，
systemd认为"进程起来了"这个unit就算启动完成，不代表bindfs真的已经挂载
完毕(systemd不会等它"ready")，所以这里自己poll几次再下结论，不依赖在
systemd那边加一个固定sleep硬等(多等了白等，少等了还是会误报)。

用法(配合对应的.service unit)：
    uv run python aerial_backend/scripts/notify_reboot.py
"""
import asyncio
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.services.notifier import send_feishu_message  # noqa: E402

MOUNT_POINT = "/mnt/Expansion2-shared"
POLL_ATTEMPTS = 5
POLL_INTERVAL_SECONDS = 2


def _wait_for_mount() -> bool:
    for _ in range(POLL_ATTEMPTS):
        if os.path.ismount(MOUNT_POINT):
            return True
        time.sleep(POLL_INTERVAL_SECONDS)
    return os.path.ismount(MOUNT_POINT)


async def main() -> None:
    mounted = _wait_for_mount()
    status = "已自动挂载" if mounted else "⚠️ 挂载失败，需要人工检查"
    await send_feishu_message(
        f"🔄 服务器重启了\n{MOUNT_POINT} 共享盘：{status}\n"
        f"阿里云自动下载以及其他数据库任务暂停，aerial_backend服务现在需要手动重启"
    )


if __name__ == "__main__":
    asyncio.run(main())
