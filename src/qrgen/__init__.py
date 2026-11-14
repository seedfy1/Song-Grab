"""qrcode（BSD-2-Clause, (c) Lincoln Loop）的纯 Python 子集，随 App 一起分发。

只保留编码所需的部分：不依赖 Pillow，只产出模块矩阵（get_matrix()）。
上游：https://github.com/lincolnloop/python-qrcode
"""
from __future__ import annotations

from .main import QRCode, make

__all__ = ["QRCode", "make"]
