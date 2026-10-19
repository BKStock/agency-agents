"""HTTP 取得。通信層（Transport）は差し替え可能で、テストでは模擬応答を使う。

* ホストごとに最小間隔を空ける（既定 3 秒）
* 429 / 5xx / 通信失敗は指数バックオフで再試行（既定 最大4回：2, 4, 8 秒）
* robots.txt を守る
* すべての試行を記録関数に渡す（fetch_log に保存される）
"""

from __future__ import annotations

import hashlib
import time
import urllib.error
import urllib.request
import urllib.robotparser
from dataclasses import dataclass, field
from typing import Callable, Protocol
from urllib.parse import urlsplit

RETRY_STATUS = {429, 500, 502, 503, 504}


@dataclass
class Response:
    url: str
    status: int
    body: bytes
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.body).hexdigest()


class Transport(Protocol):
    def get(self, url: str, headers: dict[str, str]) -> Response: ...


class UrllibTransport:
    def __init__(self, timeout: float = 60.0):
        self.timeout = timeout

    def get(self, url: str, headers: dict[str, str]) -> Response:
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return Response(resp.geturl(), resp.status, resp.read(),
                                {k.lower(): v for k, v in resp.headers.items()})
        except urllib.error.HTTPError as exc:
            return Response(url, exc.code, exc.read() or b"", {k.lower(): v for k, v in exc.headers.items()})


LogFn = Callable[[str, int, int | None, str | None, str | None], None]


class Fetcher:
    def __init__(self, transport: Transport, *, user_agent: str, min_interval: float = 3.0,
                 max_attempts: int = 4, backoff: float = 2.0, respect_robots: bool = True,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic,
                 log: LogFn | None = None):
        self.transport, self.user_agent = transport, user_agent
        self.min_interval, self.max_attempts, self.backoff = min_interval, max_attempts, backoff
        self.respect_robots, self.sleep, self.clock = respect_robots, sleep, clock
        self.log = log or (lambda *a: None)
        self._last: dict[str, float] = {}
        self._robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}

    def _wait(self, host: str) -> None:
        last = self._last.get(host)
        if last is not None:
            remaining = self.min_interval - (self.clock() - last)
            if remaining > 0:
                self.sleep(remaining)
        self._last[host] = self.clock()

    def _allowed(self, url: str) -> bool:
        if not self.respect_robots:
            return True
        parts = urlsplit(url)
        host = f"{parts.scheme}://{parts.netloc}"
        if host not in self._robots:
            parser = None
            try:
                self._wait(parts.netloc)
                resp = self.transport.get(f"{host}/robots.txt", {"User-Agent": self.user_agent})
                if resp.ok:
                    parser = urllib.robotparser.RobotFileParser()
                    parser.parse(resp.body.decode("utf-8", "replace").splitlines())
            except OSError:
                parser = None
            self._robots[host] = parser
        parser = self._robots[host]
        return parser is None or parser.can_fetch(self.user_agent, url)

    def get(self, url: str) -> Response | None:
        """取得に成功すれば Response、robots で禁止・全試行失敗なら None。"""
        if not self._allowed(url):
            self.log(url, 0, None, "disallowed_by_robots", None)
            return None
        host = urlsplit(url).netloc
        for attempt in range(1, self.max_attempts + 1):
            self._wait(host)
            try:
                resp = self.transport.get(url, {"User-Agent": self.user_agent})
            except OSError as exc:
                self.log(url, attempt, None, f"{type(exc).__name__}: {exc}", None)
                resp = None
            else:
                self.log(url, attempt, resp.status, None if resp.ok else f"http_{resp.status}",
                         resp.sha256 if resp.ok else None)
                if resp.ok:
                    return resp
                if resp.status not in RETRY_STATUS:
                    return None
            if attempt < self.max_attempts:
                self.sleep(self.backoff * 2 ** (attempt - 1))
        return None
