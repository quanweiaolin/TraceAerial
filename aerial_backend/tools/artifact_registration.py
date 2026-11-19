"""给下游各stage(感知跟踪处理/数据集生成/场景提取等)登记"我这次产出了什么"
用的客户端helper。设计见TraceAerial仓库里
docs/superpowers/specs/2026-11-16-pipeline-artifact-provenance-design.md。

这个文件故意只用标准库、不import任何其他nds_code模块——以后别的同学接入
新stage，"安装"这件事就是复制这一个文件过去，不需要对方的仓库结构跟这里
一致，也不需要装任何依赖。

调用方只传自己实际读/写的文件绝对路径，不用自己算视频sha256(后端按
videos.storage_path反查)。网络/后端不可达不是数据问题，会被追加进本地
待重试文件，下次调用时自动补发；422(路径查不到)是数据问题，记error日志，
不写入待重试文件(重试也不会成功)。
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import urllib.error
from pathlib import Path
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)

DEFAULT_BACKEND_BASE_URL = "http://127.0.0.1:8088"
try:
    DEFAULT_PENDING_FILE = Path.home() / ".aerial_pipeline" / "pending_artifacts.jsonl"
except RuntimeError:
    # Path.home()在极少数环境(容器里没有HOME/密码库查不到当前用户)会抛异常——
    # import这个模块本身绝不能因为这个失败，退化成相对路径也比直接崩溃好
    DEFAULT_PENDING_FILE = Path(".aerial_pipeline") / "pending_artifacts.jsonl"

# register_artifact被调用的那一刻，从这个文件自身所在的位置往上找git仓库——
# 不依赖调用方的cwd，调用方在别的目录下跑脚本也不会找错仓库。测试通过
# mock.patch这个模块级常量来控制"当前代码仓库"指向哪个临时仓库。
_GIT_SEARCH_ROOT = Path(__file__)

# code_commit_override的默认值用这个哨兵对象，不用None——
# 这样才能区分"调用方没传这个参数"(自动探测当前HEAD)和
# "调用方显式传了None"(补录古早数据，确实想不起来了，不要猜)。
_UNSET = object()


def _find_git_root(start_path: Path) -> Path | None:
    """往上找git仓库根目录；任何文件系统异常(符号链接环、权限问题，这个
    项目之前在NAS/bindfs上吃过不少这类苦头)都当成"找不到仓库"处理，
    不往外抛——调用方(无论是register_artifact自己还是detect_git_info/
    _resolve_commit_override)都不应该因为这个崩溃。"""
    try:
        current = start_path.parent.resolve()
        for candidate in (current, *current.parents):
            if (candidate / ".git").exists():
                return candidate
    except (OSError, RuntimeError):  # RuntimeError: resolve()撞上符号链接环
        return None
    return None


def detect_git_info(start_path: Path) -> tuple[str | None, bool | None]:
    """从start_path往上找git仓库，返回(commit_hash, is_dirty)；
    找不到仓库，或者git命令本身出问题，都返回(None, None)——
    git不存在或者行为异常绝不能让调用方崩溃。"""
    try:
        repo_root = _find_git_root(start_path)
        if repo_root is None:
            return None, None
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_root, capture_output=True, text=True, timeout=5, check=True,
        ).stdout.strip()
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=repo_root, capture_output=True, text=True, timeout=5, check=True,
        ).stdout
        return commit, bool(status.strip())
    except (OSError, subprocess.SubprocessError):
        return None, None


def _resolve_commit_override(value: str, start_path: Path) -> str | None:
    """学生补录古早数据时提供的commit值(哪怕只是前几位)，用git原生的
    短hash解析能力校验+补全成完整40位hash；解析不出来(记错了/不是
    一个有效commit/没有仓库)就返回None，不抛异常。"""
    try:
        repo_root = _find_git_root(start_path)
        if repo_root is None:
            return None
        result = subprocess.run(
            ["git", "rev-parse", "--verify", f"{value}^{{commit}}"],
            cwd=repo_root, capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            return None
        return result.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None


def _post(payload: dict, backend_base_url: str) -> None:
    request = Request(
        f"{backend_base_url}/pipeline/artifacts",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=10) as response:
        response.read()


def _flush_pending(pending_file: Path, backend_base_url: str) -> None:
    if not pending_file.exists():
        return
    lines = pending_file.read_text(encoding="utf-8").splitlines()
    still_pending: list[str] = []
    stop_trying = False
    for line in lines:
        if stop_trying:
            still_pending.append(line)
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            # 这一行本身就是坏的(比如崩溃中途写了一半)，留着也没用、永远
            # 发不出去，会一直卡住后面所有正常记录——直接丢弃并记录下来，
            # 不要无限期占着队列
            logger.error("待重试文件里有一行解析不了，丢弃: %r", line)
            continue
        try:
            _post(payload, backend_base_url)
        except urllib.error.HTTPError as exc:
            if exc.code == 422:
                logger.error(
                    "待重试记录422(数据问题，不再重试): %s, 响应: %s",
                    payload, exc.read().decode("utf-8", errors="replace"),
                )
            else:
                still_pending.append(line)
                stop_trying = True
        except Exception:  # noqa: BLE001  网络问题的具体异常类型很多，统一当作"暂时发不出去"
            still_pending.append(line)
            stop_trying = True

    if still_pending:
        # 原地write_text不是原子的，写到一半被杀掉会丢光剩下的待重试记录；
        # 先写临时文件再os.replace，保证这一步要么整体成功要么完全不生效
        tmp_path = pending_file.with_suffix(pending_file.suffix + ".tmp")
        tmp_path.write_text("\n".join(still_pending) + "\n", encoding="utf-8")
        os.replace(tmp_path, pending_file)
    else:
        pending_file.unlink(missing_ok=True)


def register_artifact(
    stage_code: str,
    input_video_paths: list[str],
    output_path: str,
    metadata: dict,
    *,
    backend_base_url: str = DEFAULT_BACKEND_BASE_URL,
    pending_file: Path | None = None,
    code_commit_override: str | None = _UNSET,
) -> None:
    """登记一个pipeline产物。永不抛异常——注册失败绝不能影响调用方真正
    的产出。

    code_commit_override:
      - 不传(默认): 自动探测_GIT_SEARCH_ROOT所在仓库当前的HEAD+dirty状态
        (正常跑pipeline时的情况)。
      - 显式传None: 补录古早数据、确实想不起来是哪个commit了，code_commit
        /code_dirty都留空，不做任何自动探测(自动填当前HEAD对古早数据是
        误导，不是不精确，是错)。
      - 传一个字符串(哪怕只是前几位): 用git原生短hash解析校验+补全，
        解析成功只填code_commit(code_dirty留空，因为"当时工作区是否
        干净"这个信息本来就无法回溯)；解析失败记warning，同样留空，
        但不阻止这条产物正常注册。
    """
    pending_file = pending_file or DEFAULT_PENDING_FILE
    try:
        _flush_pending(pending_file, backend_base_url)
    except Exception:  # noqa: BLE001  补发旧记录失败不能挡住这次新产物的注册
        logger.warning("补发待重试队列时出错，忽略，继续注册这次产物", exc_info=True)

    if code_commit_override is _UNSET:
        code_commit, code_dirty = detect_git_info(_GIT_SEARCH_ROOT)
    elif code_commit_override is None:
        code_commit, code_dirty = None, None
    else:
        resolved = _resolve_commit_override(code_commit_override, _GIT_SEARCH_ROOT)
        if resolved is None:
            logger.warning("code_commit_override解析不出来，留空继续注册: %s", code_commit_override)
            code_commit, code_dirty = None, None
        else:
            code_commit, code_dirty = resolved, None

    repo_root = _find_git_root(_GIT_SEARCH_ROOT)
    body = {
        "stage_code": stage_code,
        "input_video_paths": input_video_paths,
        "output_path": output_path,
        "metadata": metadata,
        "operator_name": os.environ.get("USER"),
        "code_repo": repo_root.name if repo_root else None,
        "code_commit": code_commit,
        "code_dirty": code_dirty,
    }

    try:
        _post(body, backend_base_url)
    except urllib.error.HTTPError as exc:
        if exc.code == 422:
            logger.error(
                "产物注册422(数据问题，不会重试): %s, 响应: %s",
                body, exc.read().decode("utf-8", errors="replace"),
            )
        else:
            logger.warning("产物注册失败(HTTP %s)，写入待重试队列: %s", exc.code, body)
            _append_pending(body, pending_file)
    except Exception:  # noqa: BLE001  网络不通/后端不可达，不是数据问题，待重试
        logger.warning("产物注册失败(网络/后端不可达)，写入待重试队列: %s", body, exc_info=True)
        _append_pending(body, pending_file)


def _append_pending(body: dict, pending_file: Path) -> None:
    pending_file.parent.mkdir(parents=True, exist_ok=True)
    with pending_file.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(body) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--retry-pending", action="store_true", help="手动补发待重试队列")
    args = parser.parse_args()
    if args.retry_pending:
        _flush_pending(DEFAULT_PENDING_FILE, DEFAULT_BACKEND_BASE_URL)
        remaining = len(DEFAULT_PENDING_FILE.read_text(encoding="utf-8").splitlines()) \
            if DEFAULT_PENDING_FILE.exists() else 0
        print(f"补发完成，还剩{remaining}条待重试")
