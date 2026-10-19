"""原本の保管。内容の SHA-256 を名前にし、同じ内容は一度だけ保存する。"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

from ..db.connection import utc_now
from ..normalize import canonical_url


class ArchiveCorruption(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def stored_path(raw_dir: Path, sha: str, filename: str) -> Path:
    ext = Path(filename).suffix.lower()[:8] if filename else ""
    return raw_dir / sha[:2] / f"{sha}{ext}"


def store(conn: sqlite3.Connection, raw_dir: Path, body: bytes, *, url: str, origin: str, series: str,
          filename: str | None = None, wayback_ts: str | None = None) -> tuple[str, bool]:
    """原本を保存して (sha256, 新規か) を返す。既存なら内容を再検証する。"""
    sha = hashlib.sha256(body).hexdigest()
    name = filename or Path(urlsplit(url).path).name
    now = utc_now()
    row = conn.execute("SELECT stored_path FROM numbers.raw_file WHERE sha256 = ?", (sha,)).fetchone()
    is_new = row is None
    if is_new:
        path = stored_path(raw_dir, sha, name)
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            fd, tmp = tempfile.mkstemp(dir=path.parent)
            with os.fdopen(fd, "wb") as fh:
                fh.write(body)
            os.replace(tmp, path)
        if sha256_file(path) != sha:
            raise ArchiveCorruption(f"{path} does not match {sha}")
        conn.execute("INSERT INTO numbers.raw_file VALUES (?,?,?,?)",
                     (sha, len(body), str(path.relative_to(raw_dir)), now))
    else:
        path = raw_dir / row["stored_path"]
        if not path.is_file() or sha256_file(path) != sha:
            raise ArchiveCorruption(f"archived file {path} is missing or altered")
    conn.execute("INSERT OR IGNORE INTO numbers.raw_source VALUES (?,?,?,?,?,?,?,?)",
                 (sha, url, canonical_url(url), origin, wayback_ts, name, series, now))
    return sha, is_new


def read(conn: sqlite3.Connection, raw_dir: Path, sha: str) -> bytes:
    row = conn.execute("SELECT stored_path FROM numbers.raw_file WHERE sha256 = ?", (sha,)).fetchone()
    if row is None:
        raise KeyError(sha)
    body = (raw_dir / row["stored_path"]).read_bytes()
    if hashlib.sha256(body).hexdigest() != sha:
        raise ArchiveCorruption(sha)
    return body
