"""データ置き場の解決。既存資産（~/official_phone_data）の配下には決して書かない。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_DATA_DIR = Path("~/landline_atlas_data")
# 既存システムの資産。新システムはここに書き込まない
PROTECTED_DIRS = (Path("~/official_phone_data"),)


class ProtectedPathError(PermissionError):
    pass


@dataclass(frozen=True)
class DataPaths:
    root: Path

    @property
    def raw(self) -> Path:
        return self.root / "raw"

    @property
    def db(self) -> Path:
        return self.root / "db"

    @property
    def runs(self) -> Path:
        return self.root / "runs"

    @property
    def exports(self) -> Path:
        return self.root / "exports"

    @property
    def backups(self) -> Path:
        return self.root / "backups"

    def ensure(self) -> "DataPaths":
        for p in (self.raw, self.db, self.runs, self.exports, self.backups):
            p.mkdir(parents=True, exist_ok=True)
        return self


def check_writable_location(path: Path, protected: tuple[Path, ...] = PROTECTED_DIRS) -> Path:
    resolved = path.expanduser().resolve()
    for p in protected:
        guard = p.expanduser().resolve()
        if resolved == guard or resolved.is_relative_to(guard):
            raise ProtectedPathError(f"{resolved} is inside protected legacy directory {guard}")
    return resolved


def resolve(data_dir: str | Path | None = None) -> DataPaths:
    root = Path(data_dir or os.environ.get("LANDLINE_ATLAS_DATA") or DEFAULT_DATA_DIR)
    return DataPaths(check_writable_location(root))
