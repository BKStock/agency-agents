"""事業者名の変化の分類。

指定データの事業者名は、年によって書き方が変わる（略称と正式名、「株式会社」の有無、全角と半角）。
また、社名変更や合併で名前が変わることもある。これらは「番号の持ち主が別の会社に移った」こととは
違うので、次のように分けて数える。元の行（旧の値・新の値）はそのまま残す。

* notation    書き方の違いだけ（NFKC・空白・「株式会社」等を除くと同じ）
* rename      同じ法人の略称・正式名・商号変更（config/carriers.json の groups）
* succession  合併などによる別法人からの引継ぎ（config/carriers.json の successions）
* vacated     事業者が空欄になった（番号の返却など）
* assigned    空欄だった事業者が入った（新たな指定など）
* transfer    上のどれにも当たらない、別の事業者への変化
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from ..normalize import compact

CORPORATE_TOKENS = ("株式会社", "(株)", "有限会社", "合同会社")
TYPES = ("notation", "rename", "succession", "vacated", "assigned", "transfer")


def name_key(name: object) -> str:
    text = compact(name)
    for token in CORPORATE_TOKENS:
        text = text.replace(token, "")
    return text


@dataclass
class CarrierMap:
    entity_of: dict[str, str] = field(default_factory=dict)       # name_key → 法人ID
    successions: set[tuple[str, str]] = field(default_factory=set)  # (旧の法人ID, 新の法人ID)

    @classmethod
    def load(cls, path: str | Path | None) -> "CarrierMap":
        if path is None or not Path(path).is_file():
            return cls()
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        cm = cls()
        for g in data.get("groups", []):
            for name in g["names"]:
                cm.entity_of[name_key(name)] = g["id"]
        for s in data.get("successions", []):
            cm.successions.add((cm.entity(s["from"]), cm.entity(s["to"])))
        return cm

    def entity(self, name: object) -> str:
        key = name_key(name)
        return self.entity_of.get(key, key)

    def classify(self, old: object, new: object) -> str:
        old_key, new_key = name_key(old), name_key(new)
        if not new_key:
            return "vacated"
        if not old_key:
            return "assigned"
        if old_key == new_key:
            return "notation"
        old_entity, new_entity = self.entity(old), self.entity(new)
        if old_entity == new_entity:
            return "rename"
        if (old_entity, new_entity) in self.successions:
            return "succession"
        return "transfer"
