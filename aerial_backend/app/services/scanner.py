"""
扫盘服务。

流程：
  1. 遍历 root_path 下的视频文件 -> 写 scan_discoveries(pending)
  2. 逐个算sha256 -> 更新 hash_status=done, 填sha256
  3. 查 videos 表：
     - 不存在该sha256:
         - 同一storage_path没有活着的旧记录 -> 真新视频 -> INSERT videos，
           is_new_task=True，发 new_task 即时通知
         - 同一storage_path有活着的旧记录(sha256不同) -> 原地重新编码(长期瘦身
           转码任务)，不是新视频 -> 旧记录deleted_at软删除下线，新记录
           replaces_sha256指回旧sha256，不发即时通知(走每日汇总统计)
     - 已存在 -> 只关联 resolved_video_sha256，is_new_task=False，不重复建任务
       (同一内容换了路径/被复制，不应该产生第二条任务流)；同时把该视频的
       storage_path更新成这次扫到的新路径——如果一整个"飞行会话文件夹"
       (含子目录)这次全部从旧位置挪到了新位置(旧文件夹下不再剩任何活着的
       视频)，额外发一条folder_moved通知，只挪了部分文件的不够文件夹粒度，
       不发(2026-10-21需求，见_folder_root/relocations)
  4. storage_path里如果能找到符合"飞行会话文件夹"命名规范的那一段(City_Site_
     Drone_Height_Person_Date，见services/session_naming.py)，顺带把
     device_id/collected_by补上——设备型号/人名在devices/operators表里找不到
     就自动建一条新记录(简单优先，发现拼写不一致导致的重复记录后续backfill
     合并即可)，匹配不到(路径不符合这个命名规范)就保持原样不处理

可以由 APScheduler 定时调用，也可以挂在 FastAPI 的 POST /scan/trigger 接口里异步跑。

------------------------------------------------------------------
【跨程序集成】其他连同一个Postgres实例的程序(比如拿Claude API跑整图
environment标注那个)，不用轮询，直接在自己的连接上执行一次
`LISTEN aerial_new_folder_video_names;`，就能实时收到新视频的推送。粒度是
文件夹：这次扫描在同一个文件夹(storage_path的直接父目录)下发现了几个真新
视频，就在这个文件夹对应的payload里列出这几个视频的文件名，不是逐个视频
单独推一条。同一个文件夹如果后续(比如下一次扫描)又有新视频上传进来，会
再推一条新的payload，里面只有这次新增的文件名(不是历史累计的全部)——
下游各自攒着拼就行。payload是JSON字符串，形如
{"folder": "<文件夹绝对路径>", "video_names": ["a.MP4", "b.MP4", ...]}；
单个文件夹一次性到几百个视频很常见，payload超过Postgres 8000字节上限时
会自动按video_names切成多条NOTIFY(folder相同)，不会报错(见_chunk_video_names)。
用的是Postgres原生的NOTIFY/LISTEN，跟着事务走(本函数这边commit了才真的
发出去，SAVEPOINT因并发冲突回滚的那次不会误发)，不需要额外加HTTP接口/
消息队列(2026-10-22达成一致)。
------------------------------------------------------------------
"""
import asyncio
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Device, Notification, Operator, ScanDiscovery, ScanJob, Video
from app.services.hashing import compute_sha256
from app.services.session_naming import find_session_match
from app.services.video_metadata import extract_new_video_fields

VIDEO_EXTENSIONS = {".mp4", ".avi", ".mov", ".mkv"}
COMMIT_BATCH_SIZE = 100  # 每处理这么多个文件落库一次，避免大目录跑到一半被杀/OOM全部白扫
NEW_VIDEO_NOTIFY_CHANNEL = "aerial_new_folder_video_names"  # 跨程序LISTEN用的channel名，见上面模块docstring
NOTIFY_PAYLOAD_MAX_BYTES = 7800  # 留点余量，Postgres NOTIFY payload硬上限是8000字节


def _chunk_video_names(folder: str, names: list[str], max_bytes: int = NOTIFY_PAYLOAD_MAX_BYTES) -> list[list[str]]:
    """把一个文件夹里新发现的视频文件名列表切成若干块，保证每块序列化成
    {"folder": folder, "video_names": [...]}这个JSON之后不超过max_bytes字节——
    单个文件夹一次性到几百个视频很常见(实测见过279个)，不切的话payload肯定
    超过Postgres NOTIFY的8000字节硬上限，直接报错。"""
    chunks: list[list[str]] = []
    current: list[str] = []
    for name in names:
        candidate = current + [name]
        payload_size = len(json.dumps({"folder": folder, "video_names": candidate}).encode("utf-8"))
        if current and payload_size > max_bytes:
            chunks.append(current)
            current = [name]
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


async def _get_or_create_device(session: AsyncSession, model: str) -> int:
    """按型号名找devices表，找不到就自动建一条(device_code就用型号名本身，
    这类自动建的记录本来就没有单独的编号体系)。device_code是唯一索引，
    保证不会重复建。"""
    result = await session.execute(select(Device).where(Device.device_code == model))
    device = result.scalar_one_or_none()
    if device is None:
        device = Device(device_code=model, model=model)
        session.add(device)
        await session.flush()
    return device.device_id


async def _get_or_create_operator(session: AsyncSession, username: str) -> int:
    """按用户名(拼音)找operators表，找不到就自动建一条。飞手自己打的拼音
    可能大小写/拼写不完全一致，导致同一个人建出多条记录——这个先接受，
    发现问题后用backfill脚本合并，不在扫盘这里做复杂的模糊匹配。"""
    result = await session.execute(select(Operator).where(Operator.username == username))
    operator = result.scalar_one_or_none()
    if operator is None:
        operator = Operator(username=username)
        session.add(operator)
        await session.flush()
    return operator.id


def _folder_root(path_str: str) -> str:
    """"文件夹"的定义：优先取路径里"飞行会话文件夹"命名规范那一段(含)为止的
    前缀(比如.../2026_nanning_AerialVideos_Q3/nanning_xxx_20261020这一级)，
    这样整个会话(连同它下面全部DJI_xxx子目录)被当成一个移动单位；路径不
    符合这套命名规范(没有session_name这一段)的，退化成用它的直接父目录。
    用于检测"整个文件夹被挪动位置"，见run_scan里的relocations。"""
    path = Path(path_str)
    match = find_session_match(path)
    if match is None:
        return str(path.parent)
    session_name = match.group(0)
    parts = path.parts
    for i, part in enumerate(parts):
        if part == session_name:
            return str(Path(*parts[: i + 1]))
    return str(path.parent)


async def _apply_session_metadata(session: AsyncSession, video: Video, storage_path: Path) -> None:
    """从storage_path里找"飞行会话文件夹"命名规范的那一段(City_Site_Drone_
    Height_Person_Date)，补上device_id/collected_by。找不到就什么都不做——
    不是所有视频都来自这套命名规范(比如历史数据/其他来源)，这是尽力而为的
    补全，不是强制要求。"""
    match = find_session_match(storage_path)
    if match is None:
        return
    video.device_id = await _get_or_create_device(session, match.group("drone"))
    video.collected_by = await _get_or_create_operator(session, match.group("person"))


async def run_scan(session: AsyncSession, root_path: str) -> ScanJob:
    """
    完整流程，需要scan_jobs/scan_discoveries/notifications三张表已建好(建表SQL见
    enable_scan_pipeline.sql)。

    ------------------------------------------------------------------
    【历史记录，供以后需要时参考，不是活代码】
    早期这几张表还没建的阶段，这个函数有一个 videos_only: bool = False 参数：
    传True时，ScanJob/ScanDiscovery/Notification只在内存里用来算计数、不
    session.add()，commit时就只有Video会真正落库，用于绕开还没建的表手动测
    "扫盘写videos表"这一步。三张表建好、切回默认参数后就不再需要了。
    如果以后又出现"部分表还没建，想只测videos这一步"的场景，可以从git历史
    找回这段逻辑：`git show 08b1c36:aerial_backend/app/services/scanner.py`
    (或更早的 70162d1/2c04b21)。
    ------------------------------------------------------------------
    """
    job = ScanJob(root_path=root_path)
    session.add(job)
    await session.flush()  # 拿到job.id

    files_found = files_new = files_duplicate = 0
    # (old_folder, new_folder) -> 这次扫描里从old_folder挪到new_folder的旧路径列表，
    # 扫完整个root后统一检查old_folder是不是已经被挪空了(见循环结束后那段)
    relocations: dict[tuple[str, str], list[str]] = defaultdict(list)
    # (old_folder, new_folder) -> 两边都真实存在、内容重复的新路径列表，
    # 扫完整个root后统一报一次(且只报一次，见循环结束后那段)
    duplicates: dict[tuple[str, str], list[str]] = defaultdict(list)
    # 文件夹绝对路径 -> 这次扫描在这个文件夹里新发现的视频文件名列表，
    # 扫完整个root后按文件夹统一给跨程序LISTEN订阅者推一次(见模块docstring)
    new_videos_by_folder: dict[str, list[str]] = defaultdict(list)

    for path in Path(root_path).rglob("*"):
        if not path.is_file() or path.suffix.lower() not in VIDEO_EXTENSIONS:
            continue
        if path.name.startswith("._"):
            # macOS拷贝到非HFS+文件系统(NAS的exFAT/SMB/NFS)时自动生成的
            # AppleDouble元数据"影子"文件，体积很小(常见4096字节)，跟真实
            # 视频内容无关，不是"正在上传中"——直接跳过，不算files_found
            continue
        if ".compress_tmp." in path.name:
            # 视频瘦身(重新编码)转码过程中的临时输出文件，转码完成后会被
            # 重命名/清理掉，扫盘时如果恰好撞见半成品不应该当成正式视频记录
            continue
        files_found += 1
        # print(f"[scan] scanning: {path}", flush=True)
        file_size = path.stat().st_size

        # 缓存命中：之前扫过同路径+同大小的文件、且hash算完了，直接复用sha256，
        # 不重新读整个文件。判断依据只用路径+大小，不含mtime——够用且没有额外
        # 表结构成本，适合"归档只增不改"的场景；如果文件曾被原地替换成同样大小
        # 的不同内容，这里会误判成"没变"，目前认为概率低到可以接受。
        cache_result = await session.execute(
            select(ScanDiscovery.sha256)
            .where(ScanDiscovery.file_path == str(path))
            .where(ScanDiscovery.file_size_bytes == file_size)
            .where(ScanDiscovery.hash_status == "done")
            .order_by(ScanDiscovery.discovered_at.desc())
            .limit(1)
        )
        cached_sha256 = cache_result.scalar_one_or_none()

        discovery = ScanDiscovery(
            scan_job_id=job.id,
            file_path=str(path),
            file_size_bytes=file_size,
            hash_status="done" if cached_sha256 is not None else "hashing",
            sha256=cached_sha256,
        )
        session.add(discovery)
        await session.flush()

        if cached_sha256 is not None:
            digest = cached_sha256
        else:
            try:
                # compute_sha256是同步阻塞IO(读整个视频文件)，直接调用会卡住
                # 整个asyncio事件循环，导致同一进程里其他协程(比如每分钟一次的
                # scheduled_notify)被拖慢甚至"missed"。丢进线程池执行。
                digest = await asyncio.to_thread(compute_sha256, path)
            except OSError as e:
                discovery.hash_status = "error"
                discovery.error_message = str(e)
                continue

            discovery.sha256 = digest
            discovery.hash_status = "done"

        existing = await session.get(Video, digest)
        if existing is None:
            # 新sha256，但可能不是"全新视频"——如果同一个storage_path上已经有
            # 一条还活着(deleted_at IS NULL)的旧记录，说明是原地重新编码(长期
            # 瘦身转码任务)覆盖了同一份文件，不是真新视频。videos.storage_path
            # 上有WHERE deleted_at IS NULL的唯一索引，旧记录不软删除的话下面
            # insert会直接报错。
            #
            # 注意：这个判定要求路径跟旧记录完全相等——如果瘦身的同时文件也被
            # 挪了位置(新路径+新sha256)，这里会找不到old_active，不会被识别成
            # 瘦身：旧记录永远不会下线，新文件被当成完全无关的新视频入库，丢失
            # replaces_sha256血缘。这种"挪动+瘦身"同时发生的情况没法靠代码可靠
            # 判断(文件名/大小这类特征去猜对应哪条旧记录是不可靠的启发式，猜错
            # 比不关联更糟)，只能从操作流程上避免：要求两步分开做，先挪动(内容
            # 不变，下面else分支会识别成"整体移动")，确认生效后再原地瘦身
            # (2026-10-21达成一致，见README.md "videos表字段说明"那段)
            old_active_result = await session.execute(
                select(Video)
                .where(Video.storage_path == str(path))
                .where(Video.deleted_at.is_(None))
            )
            old_active = old_active_result.scalar_one_or_none()
            is_re_encode = old_active is not None

            # 技术元数据(时长/分辨率/帧率/编码/collected_at)+GPS：ffprobe只读文件头，
            # 不会像sha256那样耗时；但内部调subprocess仍是阻塞调用，同样丢线程池，
            # 不占用事件循环
            new_fields = await asyncio.to_thread(extract_new_video_fields, path)
            video = Video(
                sha256=digest,
                original_name=path.name,
                storage_path=str(path),
                file_size_bytes=file_size,
                replaces_sha256=old_active.sha256 if is_re_encode else None,
                **new_fields,
            )
            try:
                # 用SAVEPOINT包住这个INSERT：aliyun下载完成后自动触发的run_scan
                # 和每小时一次的scheduled_scan可能并发扫到同一批刚落地的文件，
                # 两边都在session.get查到"不存在"之后抢着插入同一个sha256，
                # 后提交的那边会在这里撞videos_pkey唯一键冲突。只回滚到
                # SAVEPOINT(不影响这次扫描这之前已经flush/commit的其他文件)，
                # 把这次冲突当成"内容已存在"处理，而不是让整个扫描任务崩掉
                # (2026-10-09发现：炸出来的异常会让当前还没commit的这一批
                # discovery/video/notification全部回滚)
                async with session.begin_nested():
                    session.add(video)
                    # storage_path里如果能解析出"飞行会话文件夹"命名规范(见
                    # services/session_naming.py)，顺带补上device_id/collected_by；
                    # 解析不出来就保持原样(不是所有视频都来自这套命名规范)
                    await _apply_session_metadata(session, video, path)
                    await session.flush()  # 必须在这里flush，确保video先落库，
                    # 不然下面Notification.video_sha256(外键指向videos.sha256)可能
                    # 在同一次自动flush里insert顺序不对，报FK违反(见2026-09-07的事故)
            except IntegrityError:
                # begin_nested()的SAVEPOINT回滚会自动把这次失败insert的video
                # 对象从session里摘掉，这里不需要(也不能)再手动expunge一次——
                # 之前这行session.expunge(video)本身才是bug：对象已经不在
                # session里了，再调用expunge会报InvalidRequestError("Instance
                # ... is not present in this Session")，被aliyun_sync.py的
                # 异常处理捕获，误报成"扫描入库失败"(2026-10-20生产端发现)
                files_duplicate += 1
                discovery.is_new_task = False
            else:
                files_new += 1
                discovery.is_new_task = True

                if is_re_encode:
                    # 旧记录软删除下线，不物理删除，历史数据还在；不发即时飞书通知，
                    # 走每日汇总(services/notifier.send_daily_summary)统计"今日更新了多少视频"。
                    # 汇总要等第二天9点才发、还按天切窗口，扫描当下看不出真实检测到几个，
                    # 所以这里直接打印，扫完数一下log就知道这次扫描实际检测到多少次瘦身更新
                    print(
                        f"[scan] 检测到瘦身转码: {path}  "
                        f"{old_active.sha256.hex()[:12]}... -> {digest.hex()[:12]}...",
                        flush=True,
                    )
                    old_active.deleted_at = datetime.now(timezone.utc)
                else:
                    # 真新增：insert videos 会触发DB里 trg_init_pipeline_status(若已建)，
                    # 自动为它生成8个阶段的pending状态行；触发器还没建时这一步只是单纯insert
                    session.add(
                        Notification(
                            video_sha256=digest,
                            event_type="new_task",
                            channel="feishu",
                            payload={"file_path": str(path), "original_name": path.name},
                        )
                    )
                    # 记一下，扫完整个root后按文件夹统一给跨程序LISTEN订阅者
                    # 推一次(见模块docstring"跨程序集成")
                    new_videos_by_folder[str(path.parent)].append(path.name)
        else:
            files_duplicate += 1
            discovery.is_new_task = False
            old_path = existing.storage_path
            if old_path != str(path):
                # 同一份内容换了路径——但"换了路径"有两种真实情况，必须先
                # 查一下DB里记录的旧路径磁盘上是否还真的有文件，才能分清楚：
                #   - 旧路径磁盘上已经不在了 -> 真的被挪动/重命名了 -> 更新
                #     storage_path，按文件夹粒度记relocation候选(见下面)
                #   - 旧路径磁盘上还在 -> 不是挪动，是两边各有一份实打实的
                #     重复文件 -> storage_path保持冻结不动(不要跟着这次扫到
                #     的路径乱跳，不然下次扫到另一边又会跳回去，来回刷)，
                #     按文件夹粒度记duplicate候选，只报一次(2026-10-22改：
                #     之前不管三七二十一都更新storage_path，两份都真实存在
                #     时会导致每次扫描都在两个路径间来回标记"挪动"，见
                #     2026-10-21晚上nanning/shanghai那几个文件夹反复刷飞书
                #     通知的事故)
                old_folder = _folder_root(old_path)
                new_folder = _folder_root(str(path))
                if Path(old_path).exists():
                    if old_folder != new_folder:
                        duplicates[(old_folder, new_folder)].append(str(path))
                else:
                    existing.storage_path = str(path)
                    if old_folder != new_folder:
                        relocations[(old_folder, new_folder)].append(old_path)

        discovery.resolved_video_sha256 = digest

        # 每处理满COMMIT_BATCH_SIZE个文件落库一次，而不是等全部扫完才提交：
        # 1) 大目录跑到一半被杀(OOM/tmux没了)，已经处理过的视频不会白扫
        # 2) 已提交对象会被session释放，内存不会随扫描进度一直往上涨
        job.files_found = files_found
        job.files_new = files_new
        job.files_duplicate = files_duplicate
        if files_found % COMMIT_BATCH_SIZE == 0:
            await session.commit()
            print(f"[scan] committed at files_found={files_found}", flush=True)

    if relocations:
        # 只有old_folder下已经一个活着的视频都不剩(这次扫描把它见到的全部
        # 内容都挪去了new_folder)，才算达到"文件夹"粒度——只挪了一部分文件
        # 的，old_folder下还会剩下别的视频，remaining>0，不报
        await session.flush()
        for (old_folder, new_folder), old_paths in relocations.items():
            remaining_result = await session.execute(
                select(func.count())
                .select_from(Video)
                .where(Video.storage_path.like(f"{old_folder}/%"))
                .where(Video.deleted_at.is_(None))
            )
            if remaining_result.scalar_one() == 0:
                file_count = len(old_paths)
                print(
                    f"[scan] 检测到文件夹整体移动: {old_folder} -> {new_folder}，"
                    f"共{file_count}个文件",
                    flush=True,
                )
                session.add(
                    Notification(
                        event_type="folder_moved",
                        channel="feishu",
                        payload={
                            "old_folder": old_folder,
                            "new_folder": new_folder,
                            "file_count": file_count,
                        },
                    )
                )

    if duplicates:
        # storage_path已经冻结不动，所以同一对(old_folder, new_folder)以后
        # 每次扫描都会重新判定成"重复"——只在还没为这一对发过通知时才发，
        # 不然会跟改之前的flapping问题一样变成每小时刷一次。等人工清理掉
        # 其中一份、磁盘上old_path真的不存在了，下次扫描会自然判定成"真·
        # 挪动"(走上面relocations那条路)，不再落到这里
        for (old_folder, new_folder), new_paths in duplicates.items():
            already_notified_result = await session.execute(
                select(func.count())
                .select_from(Notification)
                .where(Notification.event_type == "folder_duplicate")
                .where(Notification.payload["old_folder"].astext == old_folder)
                .where(Notification.payload["new_folder"].astext == new_folder)
            )
            if already_notified_result.scalar_one() > 0:
                continue
            file_count = len(new_paths)
            print(
                f"[scan] 检测到文件夹磁盘重复(两边都有文件，storage_path冻结不变): "
                f"{old_folder} <-> {new_folder}，共{file_count}个文件",
                flush=True,
            )
            session.add(
                Notification(
                    event_type="folder_duplicate",
                    channel="feishu",
                    payload={
                        "old_folder": old_folder,
                        "new_folder": new_folder,
                        "file_count": file_count,
                    },
                )
            )

    for folder, names in new_videos_by_folder.items():
        for chunk in _chunk_video_names(folder, names):
            await session.execute(
                text("SELECT pg_notify(:channel, :payload)"),
                {
                    "channel": NEW_VIDEO_NOTIFY_CHANNEL,
                    "payload": json.dumps({"folder": folder, "video_names": chunk}),
                },
            )

    job.files_found = files_found
    job.files_new = files_new
    job.files_duplicate = files_duplicate
    job.status = "completed"
    job.finished_at = datetime.now(timezone.utc)

    await session.commit()
    print(
        f"[scan] 扫描完成: root={root_path} files_found={files_found} "
        f"files_new={files_new} files_duplicate={files_duplicate}",
        flush=True,
    )
    return job
