"""正規化：行政コード・局番・統計記号・URL・和暦日付・表記ゆれ。"""

from __future__ import annotations

import re
import unicodedata
from datetime import date
from typing import Mapping


class CodeError(ValueError):
    pass


def nfkc(raw: object) -> str:
    return "" if raw is None else unicodedata.normalize("NFKC", str(raw)).strip()


def compact(raw: object) -> str:
    """表記ゆれ比較用：NFKC＋空白除去。"""
    return re.sub(r"\s+", "", nfkc(raw))


def _digits(raw: object, field: str) -> str:
    if isinstance(raw, float):
        raise CodeError(f"{field}: float {raw!r} rejected (read the column as text)")
    text = nfkc(raw)
    if not text.isdigit():
        raise CodeError(f"{field}: non-digit {raw!r}")
    return text


def prefecture_code(raw: object) -> str:
    text = _digits(raw, "prefecture_code")
    if isinstance(raw, int) and len(text) == 1:
        text = text.zfill(2)
    if len(text) != 2 or not 1 <= int(text) <= 47:
        raise CodeError(f"prefecture_code {raw!r} is not 01-47")
    return text


def check_digit(code5: str) -> str:
    """全国地方公共団体コードの検査数字。"""
    total = sum(int(d) * w for d, w in zip(code5, (6, 5, 4, 3, 2)))
    return str((11 - total % 11) % 10)


def municipality_code(raw: object) -> str:
    text = _digits(raw, "municipality_code")
    if len(text) == 6:
        if check_digit(text[:5]) != text[5]:
            raise CodeError(f"municipality_code {raw!r}: check digit mismatch")
        text = text[:5]
    if len(text) != 5:
        raise CodeError(f"municipality_code {raw!r} must have 5 or 6 digits")
    prefecture_code(text[:2])
    return text


def town_key(raw: object) -> str:
    text = _digits(raw, "town_key")
    if len(text) < 7:
        raise CodeError(f"town_key {raw!r} is too short")
    municipality_code(text[:5])
    return text


def phone_part(raw: object, field: str, *, leading_zero: bool) -> tuple[str, bool]:
    """表のセルから局番を文字列で取り出す。(値, 補正したか) を返す。

    xls の数値セルは float で来るため、整数値なら文字列化する。市外局番は必ず 0 で
    始まるので、0 が落ちていれば補い「補正あり」として記録する（黙って補わない）。
    """
    repaired = False
    if isinstance(raw, float):
        if not raw.is_integer():
            raise CodeError(f"{field}: non-integer number {raw!r}")
        raw = int(raw)
    text = compact(raw).replace("-", "").replace("(", "").replace(")", "")
    if not text.isdigit():
        raise CodeError(f"{field}: non-digit {raw!r}")
    if leading_zero and not text.startswith("0"):
        text, repaired = "0" + text, True
    return text, repaired


STAT_SYMBOLS: Mapping[str, str] = {
    "-": "zero", "X": "suppressed", "x": "suppressed",
    "…": "missing", "...": "missing", "***": "missing", "": "missing",
}


def stat_value(raw: object, symbols: Mapping[str, str] = STAT_SYMBOLS) -> tuple[float | None, str]:
    """統計表のセルを (value, state) へ。'-' は 0、'X' は秘匿、空欄は未取得。"""
    if raw is None:
        return None, "missing"
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return float(raw), ("zero" if raw == 0 else "observed")
    text = nfkc(raw)
    if text in symbols:
        state = symbols[text]
        return (0.0 if state == "zero" else None), state
    value = float(text.replace(",", ""))
    return value, ("zero" if value == 0 else "observed")


# e-Stat の秘匿処理（HTKSYORI）。2020 年・大阪府の実データで確認：
# 1 の行は値を持ち GASSAN に合算元を列挙する（合算先）。2 の行は値が X で HTKSAKI に合算先を持つ。
SECRECY_CODES: Mapping[str, str] = {"0": "none", "1": "merge_target", "2": "merged_into_other"}


def merge_target_key(own_key: str, htksaki: object) -> str:
    """HTKSAKI を町丁目コードにする。実データでは市区町村内のコード（6桁や4桁）なので、自分の市区町村コードを前に付ける。"""
    text = _digits(htksaki, "htksaki")
    return town_key(text if len(text) >= 7 else own_key[:5] + text)


def secrecy(htksyori: object, htksaki: object, mapping: Mapping[str, str] = SECRECY_CODES, *, own_key: str = ""):
    key = nfkc(htksyori) or "0"
    if key not in mapping:
        raise ValueError(f"unknown HTKSYORI {htksyori!r}; check the e-Stat definition and extend the mapping")
    state = mapping[key]
    if state == "merged_into_other":
        if not nfkc(htksaki):
            raise ValueError("merged area without HTKSAKI")
        return state, merge_target_key(own_key, htksaki)
    return state, None


_WAYBACK = re.compile(r"^https?://web\.archive\.org/web/(\d{1,14})(?:[a-z]{2}_)?/(.+)$", re.IGNORECASE)


def split_wayback(url: str) -> tuple[str | None, str]:
    m = _WAYBACK.match(url.strip())
    return (m.group(1), m.group(2)) if m else (None, url.strip())


def canonical_url(url: str) -> str:
    """Wayback 包装・scheme・host の大小・既定ポート・末尾スラッシュ・断片を除いた比較用 URL。"""
    _, text = split_wayback(url)
    text = re.sub(r"^https?://", "", text, flags=re.IGNORECASE)
    host, _, path = text.partition("/")
    host = host.lower().removesuffix(":80").removesuffix(":443")
    path = path.split("#", 1)[0].rstrip("/")
    return f"{host}/{path}" if path else host


_ERAS = {"令和": 2018, "平成": 1988, "昭和": 1925}
_WAREKI = re.compile(r"(令和|平成|昭和)\s*(元|\d{1,2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日")
_SEIREKI = re.compile(r"(20\d{2}|19\d{2})\s*[年/.-]\s*(\d{1,2})\s*[月/.-]\s*(\d{1,2})")


def parse_japanese_date(text: str) -> date | None:
    """「令和6年4月1日現在」「2024年4月1日」「2024/04/01」などから日付を読む。"""
    t = nfkc(text)
    m = _WAREKI.search(t)
    if m:
        year = 1 if m.group(2) == "元" else int(m.group(2))
        return date(_ERAS[m.group(1)] + year, int(m.group(3)), int(m.group(4)))
    m = _SEIREKI.search(t)
    if m:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    return None
