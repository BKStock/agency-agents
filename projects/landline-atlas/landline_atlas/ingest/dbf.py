"""dBase III（Shapefile の .dbf）の読み取り。標準ライブラリだけで、属性表だけを読む。"""

from __future__ import annotations

import struct


def read_dbf(body: bytes, encoding: str = "cp932") -> list[dict]:
    if len(body) < 32:
        raise ValueError("not a dBase file")
    n_records, header_len, record_len = struct.unpack("<IHH", body[4:12])
    fields = []
    pos = 32
    while pos < header_len - 1 and body[pos] != 0x0D:
        raw_name = body[pos:pos + 11].split(b"\x00", 1)[0]
        ftype = chr(body[pos + 11])
        length = body[pos + 16]
        decimals = body[pos + 17]
        fields.append((raw_name.decode("ascii", "replace"), ftype, length, decimals))
        pos += 32
    out = []
    start = header_len
    for i in range(n_records):
        rec = body[start + i * record_len: start + (i + 1) * record_len]
        if not rec or rec[0:1] == b"*":  # 削除済みの行
            continue
        off = 1
        row = {}
        for name, ftype, length, decimals in fields:
            raw = rec[off:off + length]
            off += length
            text = raw.decode(encoding, "replace").strip()
            if ftype in ("N", "F"):
                if text in ("", "*" * len(text)):
                    row[name] = None
                else:
                    num = float(text)
                    row[name] = int(num) if decimals == 0 and num.is_integer() else num
            else:
                row[name] = text  # C（文字）・D（日付）などは文字列のまま（先頭ゼロを保つ）
        out.append(row)
    return out


def write_dbf(rows: list[dict], fields: list[tuple[str, str, int, int]], encoding: str = "cp932") -> bytes:
    """テスト用の最小限の書き出し。fields は (名前, 型 C/N, 長さ, 小数桁)。"""
    header_len = 32 + 32 * len(fields) + 1
    record_len = 1 + sum(f[2] for f in fields)
    head = struct.pack("<B3BIHH20x", 3, 126, 1, 1, len(rows), header_len, record_len)
    desc = b"".join(struct.pack("<11sc4xBB14x", n.encode("ascii"), t.encode("ascii"), ln, dc)
                    for n, t, ln, dc in fields)
    body = bytearray(head + desc + b"\x0d")
    for r in rows:
        body += b" "
        for n, t, ln, dc in fields:
            v = r.get(n)
            if t == "N":
                s = "" if v is None else (f"{v:.{dc}f}" if dc else str(int(v)))
                body += s.rjust(ln).encode("ascii")[:ln]
            else:
                body += ("" if v is None else str(v)).encode(encoding).ljust(ln)[:ln]
    body += b"\x1a"
    return bytes(body)
