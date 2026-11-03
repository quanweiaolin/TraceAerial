"""把数据库里查到的storage_path换成读起来更快的等价路径。

背景：`storage_path`存的是扫描时`AERIAL_SCAN_ROOTS`用的那个路径，目前
是`/mnt/Expansion2-shared/...`(走bindfs)。bindfs只是给阿里云下载流程
用的(下载工具会chmod，exFAT不支持chmod，bindfs负责吞掉这个调用)，跟
"读文件"这件事完全没关系，但读文件走这层照样要绕一圈FUSE——2026-11-03
实测：同一份已经在系统缓存里的数据，直连读2.9GB/s，走bindfs只有
2.8MB/s，差了1000倍。人工查看/拷贝数据的时候没必要忍这个开销，换成
直连的`/mnt/Expansion2/...`内容完全一样，快得多。

规则存在path_aliases.json里(不进git，见.gitignore——这个映射关系以后
大概率还要改，比如换盘/加新的bindfs挂载点，不想为了改一个路径前缀
就提交一次代码)，格式是`[[慢前缀, 快前缀], ...]`，按顺序匹配第一个
命中的前缀替换；都不匹配就原样返回(不是所有视频都在这条盘上，
AERIAL_SCAN_ROOTS还包括/mnt/Expansion、/mnt/nas/ADSafety这两个跟
bindfs完全无关的挂载点，不能无条件替换)。

首次使用: cp path_aliases.json.example path_aliases.json，默认内容
就是当前已知的那一条规则，不用改也能直接用。

用法：
    uv run python aerial_backend/scripts/fast_path.py "<storage_path>" ["<storage_path2>" ...]
也可以当模块用：
    from fast_path import to_fast_path
    to_fast_path("/mnt/Expansion2-shared/Aerial_Raw_Videos/xxx.MP4")
"""
from __future__ import annotations

import json
import sys
from functools import lru_cache
from pathlib import Path

_ALIASES_FILE = Path(__file__).resolve().parent.parent / "path_aliases.json"


@lru_cache(maxsize=1)
def _load_aliases() -> list[tuple[str, str]]:
    if not _ALIASES_FILE.exists():
        return []
    return [tuple(pair) for pair in json.loads(_ALIASES_FILE.read_text())]


def to_fast_path(storage_path: str) -> str:
    """命中path_aliases.json里某条慢前缀就替换成对应快前缀，内容是同一份
    文件，只是少绕bindfs这层；一个都不命中就原样返回(没配置文件/路径不在
    已知的慢挂载点上，都算不命中)。"""
    for slow_prefix, fast_prefix in _load_aliases():
        if storage_path.startswith(slow_prefix):
            return fast_prefix + storage_path[len(slow_prefix):]
    return storage_path


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    for p in sys.argv[1:]:
        print(to_fast_path(p))
