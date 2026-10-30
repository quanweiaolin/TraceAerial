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

扫到的"真新视频"除了发飞书，还会写进notifications表等process_pending_notifications
按文件夹+静默期debounce后统一处理——这一步顺带也会给跨程序的Postgres
LISTEN订阅者推送，细节见services/notifier.py模块docstring。
"""
import asyncio
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import Device, Location, Notification, Operator, ScanDiscovery, ScanJob, Video
from app.services.geo import distance_3d_meters
from app.services.hashing import compute_sha256_and_sha1
from app.services.notifier import send_feishu_message
from app.services.session_naming import find_session_match
from app.services.video_metadata import extract_new_video_fields

VIDEO_EXTENSIONS = {".mp4", ".avi", ".mov", ".mkv"}
COMMIT_BATCH_SIZE = 100  # 每处理这么多个文件落库一次，避免大目录跑到一半被杀/OOM全部白扫


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
    Height_Person_Date)，补上device_id/collected_by/city/flight_height，
    并尝试给这个视频分配地址(location_id，见_assign_city_site)。找不到就
    什么都不做——不是所有视频都来自这套命名规范(比如历史数据/其他来源)，
    这是尽力而为的补全，不是强制要求。"""
    match = find_session_match(storage_path)
    if match is None:
        return
    video.device_id = await _get_or_create_device(session, match.group("drone"))
    video.collected_by = await _get_or_create_operator(session, match.group("person"))
    video.city = match.group("city")
    video.flight_height = int(match.group("height"))
    await _assign_city_site(session, video, match.group("city"), match.group("site"))


async def _assign_city_site(
    session: AsyncSession, video: Video, city: str, raw_site: str
) -> None:
    """GPS+高度聚类的"领导者算法"(在线/增量版，2026-10-29设计讨论敲定)：
    按locations表里这个city下已有的全部地址，逐个检查新点跟该地址下videos
    表里现有全部成员(不是只跟某个中心点比)的三维距离，要求全部<=阈值才算
    可以加入——locations表本身只存地址标签+阈值+创建时间，不单独存成员
    坐标，"簇内全部成员"直接就是videos表里location_id指向这个地址的记录，
    省一份重复存储，也不会因为两份数据不同步出问题。

    候选地址范围是整个city下的全部已有地址，不限制必须跟这次视频文件夹
    自己写的site分段同名——物理聚类应该只看GPS位置，跟飞手文件夹怎么
    命名无关，site分段只用来给"新建的地址"取名字前缀。

    没有GPS坐标(collect_lon/lat为空)、或者自身飞行不稳定(is_stable为
    False，SRT轨迹本身跨度太大、单点GPS不代表真实拍摄位置)的视频不参与
    聚类，video.location_id保持NULL；地址一旦分配不会再改(调用方保证
    video是刚入库、location_id还是空的新记录，不会对已有地址的视频重新
    判定)。"""
    if video.collect_lon is None or video.collect_lat is None:
        return
    if video.is_stable is False:
        return
    height = video.srt_height if video.srt_height is not None else float(video.flight_height)

    existing_locations_result = await session.execute(
        select(Location.id, Location.site).where(Location.city == city).order_by(Location.created_at)
    )
    for candidate_id, candidate_site in existing_locations_result:
        members_result = await session.execute(
            select(Video.collect_lon, Video.collect_lat, Video.srt_height, Video.flight_height)
            .where(Video.location_id == candidate_id)
            .where(Video.deleted_at.is_(None))
        )
        members = members_result.all()
        if not members:
            continue
        fits = all(
            distance_3d_meters(
                video.collect_lon, video.collect_lat, height,
                m_lon, m_lat, m_srt_height if m_srt_height is not None else float(m_flight_height),
            ) <= settings.address_cluster_max_diameter_meters
            for m_lon, m_lat, m_srt_height, m_flight_height in members
        )
        if fits:
            video.location_id = candidate_id
            return

    # 没有任何已有地址能装下这个新点，新建一个地址。命名"{raw_site}_{序号}"，
    # 序号在同city+同raw_site前缀下递增，不同raw_site前缀各自独立计数
    prefix_sites_result = await session.execute(
        select(Location.site)
        .where(Location.city == city)
        .where(Location.site.like(f"{raw_site}_%"))
    )
    existing_indexes = [
        int(label[len(raw_site) + 1:])
        for (label,) in prefix_sites_result
        if label[len(raw_site) + 1:].isdigit()
    ]
    new_site = f"{raw_site}_{max(existing_indexes, default=0) + 1}"
    new_location = Location(
        city=city,
        site=new_site,
        threshold_meters=settings.address_cluster_max_diameter_meters,
    )
    session.add(new_location)
    await session.flush()  # 拿到new_location.id
    video.location_id = new_location.id


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

        sha1_digest = None  # 只有真正读文件重新算哈希时才会有值，见下面
        if cached_sha256 is not None:
            digest = cached_sha256
        else:
            try:
                # compute_sha256_and_sha1是同步阻塞IO(读整个视频文件)，直接
                # 调用会卡住整个asyncio事件循环，导致同一进程里其他协程(比如
                # 每分钟一次的scheduled_notify)被拖慢甚至"missed"。丢进线程池
                # 执行。顺带算出sha1(2026-10-28加，专门给阿里云下载前查重用，
                # 不用额外多读一遍文件)
                digest, sha1_digest = await asyncio.to_thread(compute_sha256_and_sha1, path)
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
            new_fields = await asyncio.to_thread(
                extract_new_video_fields, path, settings.flight_stability_max_drift_meters
            )
            video = Video(
                sha256=digest,
                sha1=sha1_digest,
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
                    # 旧记录软删除下线，不物理删除，历史数据还在。瘦身汇总
                    # (services/notifier.send_daily_summary)还是照常跑，
                    # 但2026-10-28改成额外发一条即时飞书通知——跟new_task用
                    # 同一套"按文件夹+10分钟静默期"分组debounce(见notifier.py)，
                    # 不是逐个视频立刻发，不然09-29那次一晚上202个瘦身更新会
                    # 刷屏
                    print(
                        f"[scan] 检测到瘦身转码: {path}  "
                        f"{old_active.sha256.hex()[:12]}... -> {digest.hex()[:12]}...",
                        flush=True,
                    )
                    old_active.deleted_at = datetime.now(timezone.utc)
                    session.add(
                        Notification(
                            video_sha256=digest,
                            event_type="re_encoded",
                            channel="feishu",
                            payload={"file_path": str(path), "original_name": path.name},
                        )
                    )
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


async def check_local_file_integrity(session: AsyncSession) -> None:
    """定期健康检查(建议每周跑一次，见app/main.py的调度注册)：扫一遍videos
    表里deleted_at IS NULL的活记录，检查storage_path对应的文件是不是还
    真的存在。只检测、只报警，不碰任何下载/重新扫描/写库操作——这类问题
    之前只能靠巧合发现(2026-10-29：backfill_video_sha1.py重新读文件时
    碰巧撞上FileNotFoundError才发现一个session丢了36个文件)，而且只覆盖
    走阿里云同步这条路径进来、还在被心跳监控的那一部分视频；这里做成
    全系统统一的检查，不限制来源，也不要求文件夹还在被别的地方监控。

    按"文件夹"(storage_path的直接父目录)粒度汇总成一条飞书消息，不是
    每个缺失文件单独发一条——一个文件夹丢了几十个文件只应该报一条，
    不刷屏。"""
    result = await session.execute(select(Video.storage_path).where(Video.deleted_at.is_(None)))
    paths = [row[0] for row in result.all()]

    semaphore = asyncio.Semaphore(32)

    async def _exists(path: str) -> bool:
        async with semaphore:
            return await asyncio.to_thread(Path(path).is_file)

    exists_flags = await asyncio.gather(*(_exists(p) for p in paths))

    missing_by_folder: dict[str, int] = defaultdict(int)
    for path, exists in zip(paths, exists_flags):
        if not exists:
            missing_by_folder[str(Path(path).parent)] += 1

    if not missing_by_folder:
        print(f"[check_missing] 本地文件完整性检查：共{len(paths)}条活记录，全部正常", flush=True)
        return

    total_missing = sum(missing_by_folder.values())
    detail = "\n".join(f"  {folder}：缺失{count}个" for folder, count in sorted(missing_by_folder.items()))
    print(
        f"[check_missing] 发现{total_missing}个文件缺失，涉及{len(missing_by_folder)}个文件夹：\n{detail}",
        flush=True,
    )
    await send_feishu_message(
        f"【本地文件检查】🚨 发现{total_missing}个已登记视频的本地文件缺失，"
        f"涉及{len(missing_by_folder)}个文件夹：\n{detail}"
    )
