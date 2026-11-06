"""给学生用的"紧急下载"命令行工具：输入一个完整会话文件夹名
(city_site_drone_height_person_date)，立刻从阿里云PDS下载+扫描入库，
不用等常规自动化流程那个稳定期判断。

真正的业务逻辑在app.services.urgent_download(2026-11-05拆出去的，因为
要加一个HTTP接口POST /aliyun/urgent-download，不想让app.api.routers
反过来import scripts/)——这个文件只是命令行入口，负责：
  1. 前台同步校验(会话名格式完全匹配 + 云端drive根目录下确认真实存在)，
     任何一步不过直接终端报错退出，不留痕迹
  2. 校验通过后spawn一个detach的后台子进程(`python 本文件
     --worker-mode ...`)去跑app.services.urgent_download.run_worker，
     前台立刻把终端还给用户
  3. --worker-mode: 被(1)或者HTTP接口spawn出来的那个子进程自己，不是
     给人直接用的入口

用法：
    uv run python aerial_backend/scripts/urgent_download.py \
        --session-name shanghai_jianchuanroad_mavic4pro_120m_wangxiangyu_20261104

完整的风险说明/安全边界见app/services/urgent_download.py模块docstring。
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # aerial_backend/

from app.services.session_naming import SESSION_FOLDER_DEVICES, SESSION_FOLDER_RE  # noqa: E402
from app.services.urgent_download import (  # noqa: E402
    SessionNotFoundError,
    resolve_at_root,
    run_worker,
    spawn_background_worker,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--session-name", required=True,
        help="完整会话文件夹名，格式: city_site_drone_height_person_date",
    )
    parser.add_argument("--worker-mode", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--worker-drive-id", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--worker-cloud-folder-id", default=None, help=argparse.SUPPRESS)
    args = parser.parse_args()

    if args.worker_mode:
        # 内部用：main()检测到这个标志时，是被前台进程(本文件自己或者HTTP
        # 接口)spawn出来的后台worker，不是用户直接调用的入口，不要在命令行
        # 手动传这几个--worker-*参数
        asyncio.run(run_worker(args.session_name, args.worker_drive_id, args.worker_cloud_folder_id))
        return

    # 第一步：会话名格式完全匹配校验(不是"路径里某一段能匹配上"那种宽松做法)
    if SESSION_FOLDER_RE.match(args.session_name) is None:
        print(f"❌ 会话名格式不对: {args.session_name}")
        print("正确格式: city_site_drone_height_person_date")
        print("例如: shanghai_jianchuanroad_mavic4pro_120m_wangxiangyu_20261104")
        print(f"机型只认: {', '.join(SESSION_FOLDER_DEVICES)}（新机型需要先找管理员加进白名单）")
        print("本次不会下载，请改成正确格式后重试")
        sys.exit(1)

    # 第二步：云端drive根目录下确认真的存在这个文件夹(只查根目录，不递归搜索)
    print(f"正在云端查找 /{args.session_name} ...")
    try:
        drive_id, cloud_folder_id = resolve_at_root(args.session_name)
    except SessionNotFoundError as exc:
        print(f"❌ {exc}")
        print("本次不会下载，请检查云端位置/名称后重试")
        sys.exit(1)
    print(f"✅ 找到了，drive_id={drive_id}")

    # 第三步：转后台，前台立刻把终端还给用户
    log_path = spawn_background_worker(args.session_name, drive_id, cloud_folder_id)
    print("✅ 校验通过，已转入后台下载，不再占用终端")
    print(f"后台日志: {log_path}")
    print("完成后飞书会提示；长时间没反应请重新尝试下载或联系管理员")


if __name__ == "__main__":
    main()
