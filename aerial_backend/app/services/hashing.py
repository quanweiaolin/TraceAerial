"""sha256计算与hex<->bytes互转，videos.sha256在DB里是BYTEA(32字节)。"""
import hashlib
from pathlib import Path


def compute_sha256(file_path: str | Path, chunk_size: int = 8 * 1024 * 1024) -> bytes:
    """流式计算大文件的sha256，chunk_size=8MB，避免几十GB的航测视频撑爆内存。"""
    h = hashlib.sha256()
    with open(file_path, "rb") as f:
        while chunk := f.read(chunk_size):
            h.update(chunk)
    return h.digest()  # 32字节，直接存BYTEA列


def sha256_hex_to_bytes(hex_str: str) -> bytes:
    return bytes.fromhex(hex_str)


def sha256_bytes_to_hex(b: bytes) -> str:
    return b.hex()
