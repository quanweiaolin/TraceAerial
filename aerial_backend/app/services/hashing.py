"""sha256/sha1计算与hex<->bytes互转，videos.sha256/sha1在DB里分别是
BYTEA(32字节)/BYTEA(20字节)。"""
import hashlib
from pathlib import Path


def compute_sha256(file_path: str | Path, chunk_size: int = 8 * 1024 * 1024) -> bytes:
    """流式计算大文件的sha256，chunk_size=8MB，避免几十GB的航测视频撑爆内存。"""
    h = hashlib.sha256()
    with open(file_path, "rb") as f:
        while chunk := f.read(chunk_size):
            h.update(chunk)
    return h.digest()  # 32字节，直接存BYTEA列


def compute_sha256_and_sha1(file_path: str | Path, chunk_size: int = 8 * 1024 * 1024) -> tuple[bytes, bytes]:
    """同一次流式读取顺带算出sha256和sha1，不用多读一遍文件——sha1是专门
    给阿里云下载前查重用的(阿里云PDS的content_hash字段本身就是sha1)，
    sha256还是内容身份的主键(2026-10-28加，见models.py的Video.sha1注释)。"""
    h256 = hashlib.sha256()
    h1 = hashlib.sha1()
    with open(file_path, "rb") as f:
        while chunk := f.read(chunk_size):
            h256.update(chunk)
            h1.update(chunk)
    return h256.digest(), h1.digest()  # 32字节, 20字节


def sha256_hex_to_bytes(hex_str: str) -> bytes:
    return bytes.fromhex(hex_str)


def sha256_bytes_to_hex(b: bytes) -> str:
    return b.hex()


def sha1_hex_to_bytes(hex_str: str) -> bytes:
    return bytes.fromhex(hex_str)


def sha1_bytes_to_hex(b: bytes) -> str:
    return b.hex()
