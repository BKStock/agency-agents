"""総務省ページ・Wayback からの原本収集。

総務省ページの URL とファイル名の規則は config/sources.json で設定する
（実ページで要確認）。系統（地域 01〜09）は表の中の市外局番から決める
（ingest/designation.py）。総務省のファイル名は連番 ID なので、リンク側の
series は参考情報（既定 "auto"）にとどめる。
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote, urljoin, urlsplit

from ..db.connection import utc_now
from ..normalize import canonical_url, split_wayback
from . import archive
from .http import Fetcher


@dataclass(frozen=True)
class Link:
    url: str
    series: str
    text: str


@dataclass(frozen=True)
class Collected:
    sha256: str
    series: str
    url: str
    origin: str
    wayback_ts: str | None
    is_new: bool


class _Anchors(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links: list[tuple[str, str]] = []
        self._href: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self._href = dict(attrs).get("href")
            self._text = []

    def handle_data(self, data):
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._href:
            self.links.append((self._href, "".join(self._text).strip()))
            self._href = None


def discover_links(html: str, base_url: str, patterns: list[dict]) -> list[Link]:
    parser = _Anchors()
    parser.feed(html)
    out, seen = [], set()
    for href, text in parser.links:
        url = urljoin(base_url, href)
        _, original = split_wayback(url)
        for p in patterns:
            m = re.search(p["regex"], original)
            if not m:
                continue
            series = m.groupdict().get("series") or p.get("series") or "auto"
            key = canonical_url(original)
            if key not in seen:
                seen.add(key)
                out.append(Link(original, series, text))
            break
    return out


def load_sources(path: str | Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def make_logger(conn: sqlite3.Connection, origin: str):
    def log(url, attempt, status, error, sha):
        conn.execute("INSERT INTO numbers.fetch_log (url, canonical, origin, attempt, status, error, sha256,"
                     " fetched_at) VALUES (?,?,?,?,?,?,?,?)",
                     (url, canonical_url(url), origin, attempt, status, error, sha, utc_now()))
    return log


def collect_official(fetcher: Fetcher, conn: sqlite3.Connection, raw_dir: Path, sources: dict) -> list[Collected]:
    out = []
    for page in sources["official_pages"]:
        resp = fetcher.get(page["url"])
        if resp is None:
            continue
        html = resp.body.decode(page.get("encoding", "utf-8"), "replace")
        for link in discover_links(html, page["url"], page["file_patterns"]):
            file_resp = fetcher.get(link.url)
            if file_resp is None:
                continue
            sha, new = archive.store(conn, raw_dir, file_resp.body, url=link.url, origin="official",
                                     series=link.series)
            out.append(Collected(sha, link.series, link.url, "official", None, new))
    return out


def cdx_query(fetcher: Fetcher, url: str, *, cdx_endpoint: str, date_from: str | None = None) -> list[dict]:
    q = (f"{cdx_endpoint}?url={quote(url, safe='')}&output=json"
         "&fl=timestamp,original,statuscode,digest,mimetype&filter=statuscode:200&collapse=digest")
    if date_from:
        q += f"&from={date_from}"
    resp = fetcher.get(q)
    if resp is None or not resp.body.strip():
        return []
    rows = json.loads(resp.body.decode("utf-8"))
    if not rows:
        return []
    header, *data = rows
    return [dict(zip(header, r)) for r in data]


def collect_wayback(fetcher: Fetcher, conn: sqlite3.Connection, raw_dir: Path, sources: dict) -> list[Collected]:
    """過去のページ版からファイルリンクを集め、ファイルごとに CDX で内容の異なる版だけを取得する。"""
    wb = sources["wayback"]
    files: dict[str, Link] = {}
    for page in sources["official_pages"]:
        for snap in cdx_query(fetcher, page["url"], cdx_endpoint=wb["cdx_endpoint"], date_from=wb.get("from")):
            resp = fetcher.get(f"{wb['replay_prefix']}{snap['timestamp']}id_/{snap['original']}")
            if resp is None:
                continue
            html = resp.body.decode(page.get("encoding", "utf-8"), "replace")
            for link in discover_links(html, snap["original"], page["file_patterns"]):
                files.setdefault(canonical_url(link.url), link)
    out = []
    for key, link in sorted(files.items()):
        done = {r["wayback_ts"] for r in conn.execute(
            "SELECT wayback_ts FROM numbers.raw_source WHERE canonical = ? AND origin = 'wayback'", (key,))}
        for snap in cdx_query(fetcher, link.url, cdx_endpoint=wb["cdx_endpoint"], date_from=wb.get("from")):
            if snap["timestamp"] in done:
                continue
            replay = f"{wb['replay_prefix']}{snap['timestamp']}id_/{snap['original']}"
            resp = fetcher.get(replay)
            if resp is None:
                continue
            sha, new = archive.store(conn, raw_dir, resp.body, url=replay, origin="wayback", series=link.series,
                                     filename=Path(urlsplit(snap["original"]).path).name,
                                     wayback_ts=snap["timestamp"])
            out.append(Collected(sha, link.series, link.url, "wayback", snap["timestamp"], new))
    return out


def import_local(conn: sqlite3.Connection, raw_dir: Path, path: Path, series: str = "auto") -> Collected:
    """手元のファイルを原本として取り込む（元ファイルは読むだけで変更しない）。"""
    sha, new = archive.store(conn, raw_dir, path.read_bytes(), url=path.resolve().as_uri(), origin="manual",
                             series=series, filename=path.name)
    return Collected(sha, series, path.resolve().as_uri(), "manual", None, new)


def migrate_raw(conn: sqlite3.Connection, raw_dir: Path, old_root: Path) -> dict:
    """古いデータ置き場から原本と入手経路・取得記録を写す（古い側は読み取り専用で、変更しない）。"""
    old_db = old_root / "db" / "numbers.sqlite3"
    if old_root.resolve() == raw_dir.parent.resolve():
        raise ValueError("--from must be a different data directory")
    if not old_db.is_file():
        raise FileNotFoundError(old_db)
    old = sqlite3.connect(f"{old_db.resolve().as_uri()}?mode=ro", uri=True)
    old.row_factory = sqlite3.Row
    files = new = 0
    try:
        paths = {r["sha256"]: r["stored_path"] for r in old.execute("SELECT sha256, stored_path FROM raw_file")}
        for r in old.execute("SELECT * FROM raw_source ORDER BY fetched_at"):
            body = (old_root / "raw" / paths[r["sha256"]]).read_bytes()
            if hashlib.sha256(body).hexdigest() != r["sha256"]:
                raise archive.ArchiveCorruption(f"old archive file for {r['sha256']} is altered")
            _, is_new = archive.store(conn, raw_dir, body, url=r["url"], origin=r["origin"], series=r["series"],
                                      filename=r["filename"], wayback_ts=r["wayback_ts"], fetched_at=r["fetched_at"])
            files += 1
            new += is_new
        logs = old.execute("SELECT url, canonical, origin, attempt, status, error, sha256, fetched_at"
                           " FROM fetch_log ORDER BY fetch_id").fetchall()
        conn.executemany("INSERT INTO numbers.fetch_log (url, canonical, origin, attempt, status, error, sha256,"
                         " fetched_at) VALUES (?,?,?,?,?,?,?,?)", [tuple(x) for x in logs])
    finally:
        old.close()
    return {"sources": files, "raw_files": new, "fetch_log": len(logs)}
