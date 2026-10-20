"""e-Stat 統計地理情報システムからの国勢調査（小地域）の取得。

URL の形は config/estat.json のテンプレートで与える。テンプレートで取れなければ一覧ページから
リンクを探す。どちらでも取れなければ、試した URL と HTTP 状態を返して呼び出し側で停止する。
ZIP 以外（エラーページの HTML など）は成功とみなさない。原本は SHA-256 で名前を付けて保管する。
"""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import tempfile
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin

from ..db.connection import utc_now
from .http import Fetcher


@dataclass
class Fetched:
    item: str
    ok: bool
    url: str | None = None
    sha256: str | None = None
    body: bytes | None = None
    tried: list[dict] = field(default_factory=list)


class _Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.hrefs: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            href = dict(attrs).get("href")
            if href:
                self.hrefs.append(href)


def is_zip(body: bytes | None) -> bool:
    return bool(body) and body[:2] == b"PK"


def _try(fetcher: Fetcher, url: str, tried: list[dict]) -> bytes | None:
    resp = fetcher.get(url)
    if resp is None:
        tried.append({"url": url, "result": "failed_or_disallowed"})
        return None
    if not is_zip(resp.body):
        tried.append({"url": url, "result": f"not_zip(status={resp.status}, bytes={len(resp.body)})"})
        return None
    tried.append({"url": url, "result": "ok"})
    return resp.body


def fetch_boundary(fetcher: Fetcher, cfg: dict, pref: str) -> Fetched:
    out = Fetched("boundary", False)
    for tpl in cfg["boundary"]["url_templates"]:
        url = tpl.format(pref=pref)
        body = _try(fetcher, url, out.tried)
        if body:
            out.ok, out.url, out.body, out.sha256 = True, url, body, hashlib.sha256(body).hexdigest()
            return out
    return out


def fetch_table(fetcher: Fetcher, cfg: dict, table: dict, pref: str) -> Fetched:
    sid = table["stats_id"]
    out = Fetched(sid, False)
    for tpl in cfg.get("download_templates", []):
        url = tpl.format(stats_id=sid, pref=pref)
        body = _try(fetcher, url, out.tried)
        if body:
            out.ok, out.url, out.body, out.sha256 = True, url, body, hashlib.sha256(body).hexdigest()
            return out
    # 一覧ページからリンクを探す（statsId と都道府県コードを含むダウンロードリンク）
    for tpl in cfg.get("listing_templates", []):
        page = tpl.format(stats_id=sid, pref=pref)
        resp = fetcher.get(page)
        if resp is None:
            out.tried.append({"url": page, "result": "listing_failed"})
            continue
        parser = _Links()
        parser.feed(resp.body.decode("utf-8", "replace"))
        found = [urljoin(page, h) for h in parser.hrefs
                 if "statmap-search/data" in h and sid in h and re.search(rf"code={pref}(?!\d)", h)]
        out.tried.append({"url": page, "result": f"listing_links={len(found)}"})
        for url in found:
            body = _try(fetcher, url, out.tried)
            if body:
                out.ok, out.url, out.body, out.sha256 = True, url, body, hashlib.sha256(body).hexdigest()
                return out
    return out


def store(conn: sqlite3.Connection, raw_dir: Path, fetched: Fetched, pref: str, filename: str | None = None) -> str:
    """原本を raw/census/ に保存し、regions.raw_census に記録する。同じ内容は1つだけ保存する。"""
    sha = fetched.sha256
    path = raw_dir / "census" / sha[:2] / f"{sha}.zip"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent)
        with os.fdopen(fd, "wb") as fh:
            fh.write(fetched.body)
        os.replace(tmp, path)
    if hashlib.sha256(path.read_bytes()).hexdigest() != sha:
        raise RuntimeError(f"stored census file {path} does not match {sha}")
    conn.execute("INSERT OR IGNORE INTO regions.raw_census VALUES (?,?,?,?,?,?,?)",
                 (sha, fetched.url, filename, str(path.relative_to(raw_dir)), fetched.item, pref, utc_now()))
    return sha
