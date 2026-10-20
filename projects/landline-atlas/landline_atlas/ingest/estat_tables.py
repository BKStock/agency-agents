"""e-Stat からダウンロードした国勢調査（小地域）の ZIP の解析と取込。

統計表 ZIP の中の CSV は、1行目が列コード（KEY_CODE, HTKSYORI, …, T001081001 など）、
2行目が日本語の見出し、3行目以降がデータ、という形を想定する。列は日本語の見出しで探し、
当たらない・複数に当たる場合は取り込まずに報告する。

行は町丁・字等の「いちばん細かい階層」だけを使う。ある KEY_CODE を先頭に持つ、より長い
KEY_CODE があれば、それは上位の階層（大字の合計など）なので除く（二重計上の防止）。

秘匿・合算は表ごとに付く（実データでは人口総数の表に秘匿が無く、年齢・世帯・住宅の表にある）。
町丁目の秘匿の状態は、取得した全ての表を合わせて決める（secrecy_by_town）。
"""

from __future__ import annotations

import csv
import io
import sqlite3
import zipfile
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from ..normalize import CodeError, compact, nfkc, secrecy, stat_value, town_key
from ..regions.features import Feature, add_source, new_load, put
from .dbf import read_dbf


@dataclass
class Table:
    member: str
    codes: list[str]
    labels: list[str]
    rows: list[list[str]]


def _decode(raw: bytes) -> str:
    for enc in ("utf-8-sig", "cp932"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    raise ValueError("cannot decode CSV (tried UTF-8 and CP932)")


def parse_table_zip(body: bytes) -> Table:
    with zipfile.ZipFile(io.BytesIO(body)) as zf:
        members = [i for i in zf.infolist() if PurePosixPath(i.filename.lower()).suffix in (".txt", ".csv")]
        if not members:
            raise ValueError("no .txt/.csv inside the ZIP")
        info = members[0]
        rows = list(csv.reader(io.StringIO(_decode(zf.read(info)))))
    if not rows or "KEY_CODE" not in [nfkc(c) for c in rows[0]]:
        raise ValueError(f"{info.filename}: first row has no KEY_CODE column")
    codes = [nfkc(c) for c in rows[0]]
    key_idx = codes.index("KEY_CODE")
    has_labels = len(rows) > 1 and not nfkc(rows[1][key_idx] if key_idx < len(rows[1]) else "").isdigit()
    labels = [nfkc(c) for c in rows[1]] if has_labels else list(codes)
    data = rows[2:] if has_labels else rows[1:]
    return Table(info.filename, codes, labels, data)


def match_features(table: Table, specs: dict) -> dict:
    """特徴量ごとに見出しで列を探す。{feature: {"status", "index", "label", "candidates"}}"""
    stat_cols = [i for i, c in enumerate(table.codes) if c.startswith("T")]
    out = {}
    for feature, spec in specs.items():
        inc = [compact(w) for w in spec.get("include", [])]
        exc = [compact(w) for w in spec.get("exclude", [])]
        hits = [i for i in stat_cols
                if all(w in compact(table.labels[i]) for w in inc) and not any(w in compact(table.labels[i]) for w in exc)]
        status = "ok" if len(hits) == 1 else ("none" if not hits else "multiple")
        out[feature] = {"status": status, "index": hits[0] if len(hits) == 1 else None,
                        "label": table.labels[hits[0]] if len(hits) == 1 else None,
                        "candidates": [f"{table.codes[i]}:{table.labels[i]}" for i in hits][:10]}
    return out


def leaf_rows(table: Table) -> tuple[list[list[str]], dict]:
    """町丁・字等の最下層の行だけを返す。除いた行の件数も返す。"""
    k = table.codes.index("KEY_CODE")
    keyed = []
    skipped = {"not_town_key": 0, "upper_level": 0}
    for r in table.rows:
        raw = nfkc(r[k]) if k < len(r) else ""
        try:
            keyed.append((town_key(raw), r))
        except CodeError:
            skipped["not_town_key"] += 1
    keys = sorted({key for key, _ in keyed})
    parents = {a for a, b in zip(keys, keys[1:]) if b.startswith(a)}
    out = []
    for key, r in keyed:
        if key in parents:
            skipped["upper_level"] += 1
        else:
            out.append(r)
    return out, skipped


def _col(table: Table, name: str) -> int | None:
    return table.codes.index(name) if name in table.codes else None


@dataclass
class Secrecy:
    states: dict[str, tuple[str, str | None]] = field(default_factory=dict)  # town_key → (状態, 合算先)
    conflicts: list[dict] = field(default_factory=list)
    into_upper_level: int = 0   # 親の大字（上位の階層。町丁目マスタに入らない）へ合算された町丁目

    def counts(self, keys) -> dict:
        out = {"none": 0, "merged_into_other": 0, "merge_target": 0}
        for k in keys:
            out[self.states.get(k, ("none", None))[0]] += 1
        return {**out, "merged_into_upper_level": self.into_upper_level, "conflicts": self.conflicts[:20]}


def secrecy_by_town(tables: list[Table]) -> Secrecy:
    """全ての表の HTKSYORI/HTKSAKI を合わせて町丁目ごとの秘匿の状態を決める。

    秘匿なし（0）の表は他の表の秘匿と矛盾しない（表によって秘匿の有無が違う）。
    2つの表が違う秘匿の状態・合算先を示したら矛盾として返す（どちらかを推測で選ばない）。
    """
    out = Secrecy()
    upper: set[str] = set()
    for table in tables:
        leaves, _ = leaf_rows(table)
        k, hk, hs = table.codes.index("KEY_CODE"), _col(table, "HTKSYORI"), _col(table, "HTKSAKI")
        if hk is None:
            continue
        all_keys = {nfkc(r[k]) for r in table.rows if k < len(r)}
        leaf_keys = {nfkc(r[k]) for r in leaves}
        upper |= all_keys - leaf_keys
        for r in leaves:
            key = town_key(nfkc(r[k]))
            got = secrecy(r[hk] if hk < len(r) else "0", r[hs] if hs is not None and hs < len(r) else None, own_key=key)
            if got[0] == "none":
                continue
            prev = out.states.setdefault(key, got)
            if prev != got:
                out.conflicts.append({"town_key": key, "states": [list(prev), list(got)]})
    out.into_upper_level = sum(1 for s, m in out.states.values() if s == "merged_into_other" and m in upper)
    return out


def describe_table(table: Table, spec: dict) -> dict:
    leaves, skipped = leaf_rows(table)
    hk = _col(table, "HTKSYORI")
    secrecy_values = {}
    if hk is not None:
        for r in leaves:
            v = nfkc(r[hk]) if hk < len(r) else ""
            secrecy_values[v] = secrecy_values.get(v, 0) + 1
    return {"member": table.member, "rows": len(table.rows), "town_rows": len(leaves), "skipped": skipped,
            "columns": [f"{c}:{lab}" for c, lab in zip(table.codes, table.labels)][:60],
            "column_count": len(table.codes),
            "matches": match_features(table, spec.get("features", {})), "htksyori_values": secrecy_values}


def _computed(spec: dict) -> dict[str, list[str]]:
    """computed の式（["a", "+", "b", "-", "c"]）を検査して返す。演算子は + と - だけ。"""
    out = {}
    for name, expr in spec.get("computed", {}).items():
        if len(expr) % 2 == 0 or any(op not in ("+", "-") for op in expr[1::2]):
            raise ValueError(f"computed {name}: expected [feature, '+'|'-', feature, ...], got {expr}")
        out[name] = expr
    return out


def _evaluate(expr: list[str], values: dict, source_id: str) -> Feature | None:
    terms = [values.get(n) for n in expr[0::2]]
    if any(t is None for t in terms):
        return None
    bad = next((t for t in terms if not t.usable), None)
    if bad is not None:
        return Feature(None, bad.state, source_id)
    total = terms[0].value
    for op, t in zip(expr[1::2], terms[1:]):
        total = total + t.value if op == "+" else total - t.value
    return Feature.of(total, source_id)


def import_table(conn: sqlite3.Connection, table: Table, spec: dict, year: int, version: str, source_id: str,
                 secrecy_map: Secrecy | None = None) -> dict:
    """見出しで当たった列だけを取り込む。当たらない・複数の列は取り込まず、結果に書く。

    町丁目を作る表では、secrecy_map（全ての表から決めた秘匿の状態）があればそれを使う。
    """
    matches = match_features(table, spec.get("features", {}))
    bad = {f: m for f, m in matches.items() if m["status"] != "ok"}
    leaves, skipped = leaf_rows(table)
    k = table.codes.index("KEY_CODE")
    created = 0
    if spec.get("creates_towns"):
        hk, hs = _col(table, "HTKSYORI"), _col(table, "HTKSAKI")
        city, name = _col(table, "CITYNAME"), _col(table, "NAME")
        for r in leaves:
            key = town_key(nfkc(r[k]))
            if secrecy_map is not None:
                state, merged = secrecy_map.states.get(key, ("none", None))
            else:
                state, merged = secrecy(r[hk] if hk is not None else "0", r[hs] if hs is not None else None,
                                        own_key=key)
            label = "".join(nfkc(r[i]) for i in (city, name) if i is not None and i < len(r)) or None
            cur = conn.execute("INSERT OR IGNORE INTO regions.town VALUES (?,?,?,?,?,?,?,?,?)",
                               (key, year, version, key[:2], key[:5], label, state, merged, source_id))
            created += cur.rowcount
    known = {r[0] for r in conn.execute("SELECT town_key FROM regions.town WHERE census_year = ? AND boundary_version = ?",
                                        (year, version))}
    load_id = new_load(conn, "census", source_id)
    # 計算に使うだけの列（"store": false。例：15歳未満）は保存せず、計算結果（年齢判明人口）だけを保存する
    computed = _computed(spec)
    inputs = {name for name, fs in spec.get("features", {}).items() if fs.get("store") is False}
    rows, unknown = [], 0
    for r in leaves:
        key = town_key(nfkc(r[k]))
        if key not in known:
            unknown += 1
            continue
        values = {}
        for feature, m in matches.items():
            if m["status"] == "ok":
                values[feature] = Feature(*stat_value(r[m["index"]] if m["index"] < len(r) else None), source_id)
        for feature, expr in computed.items():
            got = _evaluate(expr, values, source_id)
            if got is not None:
                values[feature] = got
        rows += [(key, f, v) for f, v in values.items() if f not in inputs]
    n = put(conn, load_id, year, version, rows)
    return {"towns_created": created, "features_written": n, "load_id": load_id, "skipped": skipped,
            "rows_without_town": unknown, "unmatched_features": {f: m["status"] for f, m in bad.items()}}


def parse_boundary_zip(body: bytes) -> list[dict]:
    with zipfile.ZipFile(io.BytesIO(body)) as zf:
        names = zf.namelist()
        dbfs = [n for n in names if n.lower().endswith(".dbf")]
        if not dbfs:
            raise ValueError("no .dbf inside the boundary ZIP")
        cpg = [n for n in names if n.lower().endswith(".cpg")]
        enc = "cp932"
        if cpg:
            declared = zf.read(cpg[0]).decode("ascii", "replace").strip().lower()
            enc = {"utf-8": "utf-8", "utf8": "utf-8", "shift_jis": "cp932", "sjis": "cp932"}.get(declared, enc)
        records = []
        for d in dbfs:
            records += read_dbf(zf.read(d), enc)
    return records


def _field(records: list[dict], aliases: list[str]) -> str | None:
    if not records:
        return None
    return next((a for a in aliases if a in records[0]), None)


def describe_boundary(records: list[dict], fields: dict) -> dict:
    return {"records": len(records), "fields": sorted(records[0]) if records else [],
            "resolved": {k: _field(records, v) for k, v in fields.items()}}


def import_boundary(conn: sqlite3.Connection, records: list[dict], fields: dict, year: int, version: str,
                    source_id: str) -> dict:
    """境界の属性から面積（AREA, m² → km²）を取り込む。人口は統計表と照合するだけにする。"""
    key_f, area_f = _field(records, fields["key"]), _field(records, fields["area_m2"])
    pop_f = _field(records, fields.get("population", []))
    if key_f is None or area_f is None:
        raise ValueError(f"boundary fields not found (key={key_f}, area={area_f})")
    known = {r[0] for r in conn.execute("SELECT town_key FROM regions.town WHERE census_year = ? AND boundary_version = ?",
                                        (year, version))}
    pops = {r["town_key"]: r["value"] for r in conn.execute(
        """SELECT tf.town_key, tf.value FROM regions.town_feature tf
           WHERE tf.feature = 'pop_total' AND tf.census_year = ? AND tf.boundary_version = ?
             AND tf.load_id = (SELECT MAX(load_id) FROM regions.town_feature t2 WHERE t2.town_key = tf.town_key
                               AND t2.feature = 'pop_total' AND t2.census_year = tf.census_year
                               AND t2.boundary_version = tf.boundary_version)""", (year, version))}
    area: dict[str, float] = {}
    pop: dict[str, float] = {}
    unknown = 0
    for rec in records:
        try:
            key = town_key(nfkc(rec[key_f]))
        except CodeError:
            unknown += 1
            continue
        if key not in known:
            unknown += 1
            continue
        if isinstance(rec.get(area_f), (int, float)):
            area[key] = area.get(key, 0.0) + rec[area_f]  # 1つの町丁目が複数のポリゴンに分かれていれば合計する
        if pop_f and isinstance(rec.get(pop_f), (int, float)):
            pop[key] = pop.get(key, 0.0) + rec[pop_f]
    load_id = new_load(conn, "gis", source_id)
    n = put(conn, load_id, year, version, [(k, "area_km2", Feature.of(v / 1_000_000, source_id)) for k, v in area.items()])
    mismatch = sum(1 for k, v in pop.items() if pops.get(k) is not None and abs(pops[k] - v) > 0.5)
    return {"area_written": n, "records_without_town": unknown, "population_mismatch_towns": mismatch,
            "load_id": load_id}


def register(conn: sqlite3.Connection, source_id: str, title: str, url: str, year: int, license: str,
             sha256: str) -> None:
    add_source(conn, source_id, title, url=url, source_year=year, license=license)
    conn.execute("UPDATE regions.source_document SET sha256 = ? WHERE source_id = ? AND sha256 IS NULL",
                 (sha256, source_id))
