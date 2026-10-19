"""行政コード・統計値・URL の正規化。

先頭ゼロの欠落（Excel が数値として読んだ 1101 など）は黙って補わず、
桁数が合わない値は例外にする。補完してよいのは呼び出し側が桁数を
明示した場合だけ。
"""

from __future__ import annotations

import re
import unicodedata
from typing import Mapping

VALID_STATES = ("observed", "zero", "missing", "suppressed", "merged", "undefined")


class CodeError(ValueError):
    pass


def _digits(raw: object, field: str) -> str:
    if isinstance(raw, float):
        # 1101.0 のような float は Excel 経由でゼロが落ちた可能性が高い
        raise CodeError(f"{field}: float value {raw!r} is not accepted; read the column as text")
    text = unicodedata.normalize("NFKC", str(raw)).strip()
    if not text.isdigit():
        raise CodeError(f"{field}: non-digit value {raw!r}")
    return text


def prefecture_code(raw: object, *, pad_from_int: bool = False) -> str:
    text = _digits(raw, "prefecture_code")
    if len(text) == 1 and (pad_from_int or isinstance(raw, int)):
        text = text.zfill(2)
    if len(text) != 2 or not 1 <= int(text) <= 47:
        raise CodeError(f"prefecture_code: {raw!r} is not 01-47")
    return text


def municipality_check_digit(code5: str) -> str:
    """JIS X 0402 / 全国地方公共団体コードの検査数字。"""
    weights = (6, 5, 4, 3, 2)
    total = sum(int(d) * w for d, w in zip(code5, weights))
    return str((11 - total % 11) % 10)


def municipality_code(raw: object, *, verify_check_digit: bool = True) -> str:
    """5桁（検査数字なし）に正規化する。6桁なら検査数字を検証して落とす。"""
    text = _digits(raw, "municipality_code")
    if len(text) == 6:
        body, check = text[:5], text[5]
        if verify_check_digit and municipality_check_digit(body) != check:
            raise CodeError(f"municipality_code: check digit mismatch for {raw!r}")
        text = body
    if len(text) != 5:
        raise CodeError(f"municipality_code: {raw!r} must be 5 or 6 digits")
    prefecture_code(text[:2])
    return text


def town_key(raw: object, *, width: int | None = None) -> str:
    """町丁目キー（e-Stat KEY_CODE など）。width 指定時のみゼロ埋めする。"""
    text = _digits(raw, "town_key")
    if width is not None:
        if len(text) > width:
            raise CodeError(f"town_key: {raw!r} longer than width {width}")
        text = text.zfill(width)
    if len(text) < 7:
        raise CodeError(f"town_key: {raw!r} is too short to contain a municipality code")
    prefecture_code(text[:2])
    return text


# e-Stat 小地域集計の表記。データセットごとに凡例を確認し、必要なら上書きする。
DEFAULT_ESTAT_SYMBOLS: Mapping[str, str] = {
    "-": "zero",          # 該当数値なし
    "X": "suppressed",    # 秘匿
    "x": "suppressed",
    "…": "missing",       # 不詳・調査対象外
    "...": "missing",
    "***": "missing",
    "": "missing",
}


def parse_stat_value(raw: object, symbols: Mapping[str, str] = DEFAULT_ESTAT_SYMBOLS) -> tuple[float | None, str]:
    """統計表のセル値を (value, state) に変換する。"""
    if raw is None:
        return None, "missing"
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return (float(raw), "zero") if raw == 0 else (float(raw), "observed")
    text = unicodedata.normalize("NFKC", str(raw)).strip()
    if text in symbols:
        state = symbols[text]
        return (0.0, state) if state == "zero" else (None, state)
    try:
        value = float(text.replace(",", ""))
    except ValueError as exc:
        raise ValueError(f"unrecognized statistical value {raw!r}") from exc
    return (value, "zero") if value == 0 else (value, "observed")


# e-Stat 境界データ HTKSYORI（秘匿処理）の値。定義書で取込前に必ず確認すること。
DEFAULT_HTKSYORI: Mapping[str, str] = {
    "0": "none",
    "1": "merged_into_other",   # 秘匿のため他地域へ合算された地域
    "2": "merge_target",        # 合算を受け入れた地域
}


def secrecy_state(htksyori: object, htksaki: object,
                  mapping: Mapping[str, str] = DEFAULT_HTKSYORI) -> tuple[str, str | None]:
    key = "" if htksyori is None else unicodedata.normalize("NFKC", str(htksyori)).strip()
    if key not in mapping:
        raise ValueError(f"unknown HTKSYORI value {htksyori!r}; extend the mapping after checking the spec")
    state = mapping[key]
    if state == "merged_into_other":
        if htksaki in (None, ""):
            raise ValueError("merged area without HTKSAKI (merge target)")
        return state, town_key(htksaki)
    return state, None


def normalize_text(raw: object) -> str:
    """表記ゆれ比較用：NFKC、空白除去。"""
    if raw is None:
        return ""
    text = unicodedata.normalize("NFKC", str(raw))
    return re.sub(r"\s+", "", text)


_WAYBACK = re.compile(r"^https?://web\.archive\.org/web/\d{1,14}(?:[a-z]{2}_)?/(.+)$", re.IGNORECASE)


def canonical_url(url: str) -> str:
    """Wayback のラッパーを外し、scheme・host・末尾スラッシュを揃える。"""
    text = url.strip()
    match = _WAYBACK.match(text)
    if match:
        text = match.group(1)
    text = re.sub(r"^https?://", "", text, flags=re.IGNORECASE)
    host, _, path = text.partition("/")
    host = host.lower().removesuffix(":80").removesuffix(":443")
    path = path.split("#", 1)[0].rstrip("/")
    return f"{host}/{path}" if path else host
