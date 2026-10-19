"""読み取り専用の検索画面（標準ライブラリの http.server、127.0.0.1 のみで待ち受け）。

ページ: /（検索）、/town?key=、/code?area=&local=、/dashboard、/priority
"""

from __future__ import annotations

import html
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

from ..classify.rules import CODES
from ..db.connection import connect
from ..link import relations
from ..search.query import Filter, search, town_detail

STATUS_JA = {"confirmed": "確定", "candidate": "候補", "not_applicable": "非該当", "on_hold": "判定保留",
             "insufficient_data": "資料不足", "undeterminable": "判定不能"}
LEVEL_JA = {"area": "区画レベル（未確認）", "observed_verified": "観測・確認済み",
            "observed_unverified": "観測・未確認", "no_observation": "観測なし"}

CSS = """
:root{--bg:#fff;--fg:#1f2328;--muted:#59636e;--line:#d1d9e0;--accent:#0b6bcb;--warn:#9a6700}
@media (prefers-color-scheme:dark){:root{--bg:#0d1117;--fg:#e6edf3;--muted:#9198a1;--line:#3d444d;--accent:#4493f8;--warn:#d29922}}
body{background:var(--bg);color:var(--fg);font-family:system-ui,-apple-system,"Hiragino Sans",sans-serif;margin:0 auto;max-width:1100px;padding:16px}
a{color:var(--accent)} table{border-collapse:collapse;width:100%;font-size:14px} th,td{border-bottom:1px solid var(--line);padding:6px;text-align:left;vertical-align:top}
form{display:flex;flex-wrap:wrap;gap:8px;align-items:end;margin:12px 0} label{display:flex;flex-direction:column;font-size:12px;color:var(--muted)}
input,select{padding:4px;font-size:14px} .muted{color:var(--muted)} .hatch{background:repeating-linear-gradient(45deg,transparent 0 4px,var(--line) 4px 6px)}
nav a{margin-right:12px} .note{color:var(--warn);font-size:13px}
"""


def page(title: str, body: str) -> str:
    return (f"<!doctype html><html lang='ja'><head><meta charset='utf-8'><meta name='viewport' "
            f"content='width=device-width,initial-scale=1'><title>{html.escape(title)}</title><style>{CSS}</style>"
            f"</head><body><nav><a href='/'>検索</a><a href='/dashboard'>ダッシュボード</a><a href='/priority'>調査優先順位</a>"
            f"</nav><h1>{html.escape(title)}</h1>{body}</body></html>")


def e(v) -> str:
    return "" if v is None else html.escape(str(v))


def link(url, text: str) -> str:
    """http(s) の出典だけをリンクにする（javascript: などは表示のみ）。"""
    if not url:
        return ""
    if str(url).startswith(("https://", "http://")):
        return f"<a href='{e(url)}' rel='noreferrer'>{e(text)}</a>"
    return f"<span class='muted'>{e(url)}</span>"


def _one(q: dict, k: str) -> str | None:
    v = q.get(k, [""])[0].strip()
    return v or None


def render_search(conn, q: dict) -> str:
    mode = _one(q, "mode") or "residential"
    f = Filter(prefecture=_one(q, "pref"), municipality=_one(q, "muni"), town=_one(q, "town"),
               classes=[c for c in q.get("class", []) if c in CODES], all_classes=_one(q, "all") == "1",
               axis=_one(q, "axis"), mode=None if mode == "all" else mode, include_r6=_one(q, "r6") == "1",
               area_code=_one(q, "area"), local_code=_one(q, "local"), number_area_code=_one(q, "na"),
               carrier=_one(q, "carrier"), as_of_date=_one(q, "asof"), usage_status=_one(q, "usage"),
               relation_level=_one(q, "level"))
    if _one(q, "status"):
        f.statuses = (_one(q, "status"),)
    boxes = "".join(f"<label>{c}<input type='checkbox' name='class' value='{c}'"
                    f"{' checked' if c in f.classes else ''}></label>" for c in CODES)
    form = (f"<form method='get'>"
            f"<label>都道府県<input name='pref' size='3' value='{e(f.prefecture)}'></label>"
            f"<label>市区町村<input name='muni' size='6' value='{e(f.municipality)}'></label>"
            f"<label>町丁目<input name='town' value='{e(f.town)}'></label>{boxes}"
            f"<label>すべて該当<input type='checkbox' name='all' value='1'{' checked' if f.all_classes else ''}></label>"
            f"<label>市外局番<input name='area' size='5' value='{e(f.area_code)}'></label>"
            f"<label>市内局番<input name='local' size='5' value='{e(f.local_code)}'></label>"
            f"<label>番号区画<input name='na' size='6' value='{e(f.number_area_code)}'></label>"
            f"<label>事業者<input name='carrier' size='8' value='{e(f.carrier)}'></label>"
            f"<label>基準日<input name='asof' size='10' value='{e(f.as_of_date)}'></label>"
            f"<label>使用状況<input name='usage' size='6' value='{e(f.usage_status)}'></label>"
            "<label>根拠<select name='level'><option value=''>すべて</option>"
            + "".join(f"<option value='{k}'{' selected' if f.relation_level == k else ''}>{v}</option>"
                      for k, v in LEVEL_JA.items() if k != "no_observation") + "</select></label>"
            "<label>判定状態<select name='status'><option value=''>確定＋候補</option>"
            + "".join(f"<option value='{k}'{' selected' if f.statuses == (k,) else ''}>{v}</option>"
                      for k, v in STATUS_JA.items()) + "</select></label>"
            f"<label>検索の種類<select name='mode'><option value='residential'{' selected' if mode == 'residential' else ''}>"
            f"住宅系</option><option value='all'{' selected' if mode == 'all' else ''}>すべて</option></select></label>"
            f"<label>大阪府R6を表示<input type='checkbox' name='r6' value='1'{' checked' if f.include_r6 else ''}></label>"
            f"<button>検索</button></form>")
    note = ("<p class='note'>住宅系検索では、大阪府の R6（住宅・商業混在地区）を初期設定で非表示にしています。"
            "分類結果は保持されています。</p>" if f.mode == "residential" and not f.include_r6 else "")
    rows = search(conn, f)[:500]
    trs = "".join(
        f"<tr><td><a href='/town?{urlencode({'key': r['town_key']})}'>{e(r['town_key'])}</a></td><td>{e(r['town_name'])}</td>"
        f"<td>{e(r['class_code'])}{'（主）' if r['primary_flag'] else ''}</td><td>{STATUS_JA[r['label_status']]}</td>"
        f"<td>{e(r['evidence_grade'])}</td><td>{e(r['score'])}</td><td class='muted'>{e(r['reason'])}</td>"
        f"<td>{link(r['source_url'], '出典')}</td></tr>" for r in rows)
    table = ("<table><tr><th>町丁目キー</th><th>名称</th><th>分類</th><th>状態</th><th>等級</th><th>スコア</th>"
             f"<th>理由</th><th>統計</th></tr>{trs}</table>" if rows else "<p class='muted'>該当なし</p>")
    return page("全国固定電話番号調査：12分類検索", form + note + f"<p class='muted'>{len(rows)} 行</p>" + table)


def render_town(conn, key: str) -> str:
    d = town_detail(conn, key)
    if d is None:
        return page("町丁目が見つかりません", "")
    t = d["town"]
    cls = "".join(f"<tr class='{'hatch' if c['label_status'] in ('insufficient_data', 'on_hold') else ''}'>"
                  f"<td>{e(c['class_code'])}</td><td>{STATUS_JA[c['label_status']]}</td><td>{e(c['evidence_grade'])}</td>"
                  f"<td>{e(c['score'])}</td><td>{'主' if c['primary_flag'] else ('副' if c['secondary_flag'] else '')}"
                  f"{' 重複' if c['overlap_flag'] else ''}</td><td class='muted'>{e(c['reason'])}</td></tr>"
                  for c in d["classes"])
    axes = "".join(f"<li>{e(a['axis'])}軸：{e(a['primary_class'] or '—')}（{e(a['state'])}）</li>" for a in d["axes"])
    rels = "".join(
        f"<tr><td>{LEVEL_JA[r['relation_level']]}</td><td>"
        + (f"<a href='/code?{urlencode({'area': r['area_code'], 'local': r['local_code']})}'>"
           f"{e(r['area_code'])}-{e(r['local_code'])}</a>" if r.get("area_code") else "")
        + f"</td><td class='muted'>{e(r.get('note') or r.get('source_urls') or '')}</td></tr>" for r in d["relations"])
    return page(f"{t['town_name'] or key}（{key}）",
                f"<p class='muted'>{e(t['census_year'])}年 境界 {e(t['boundary_version'])}／秘匿 {e(t['secrecy'])}</p>"
                f"<ul>{axes}</ul><h2>12分類</h2><table><tr><th>分類</th><th>状態</th><th>等級</th><th>スコア</th>"
                f"<th>主副</th><th>理由</th></tr>{cls}</table><h2>局番との関連</h2><table>{rels}</table>")


def render_code(conn, area: str, local: str) -> str:
    d = relations.for_code(conn, area, local)
    hist = "".join(f"<tr><td>{e(h['as_of_date'])}</td><td>{e(h['carrier'])}</td><td>{e(h['usage_status'])}</td>"
                   f"<td>{e(h['number_area_code'])}</td><td>{link(h['source_url'], '原本')}</td></tr>"
                   for h in d["designation_history"])
    ch = "".join(f"<li>{e(c['new_date'])}：{e(c['change_kind'])} {e(c['old_value'])} → {e(c['new_value'])}</li>"
                 for c in d["changes"])
    obs = "".join(f"<li><a href='/town?{urlencode({'key': o['town_key']})}'>{e(o['town_key'])}</a>"
                  f"（{LEVEL_JA[o['relation_level']]}、{e(o['observation_count'])}件）</li>" for o in d["observed_towns"])
    munis = "、".join(f"{m['municipality_code']}（{m['coverage']}）" for m in d["municipalities"])
    return page(f"局番 {area}-{local}",
                f"<h2>指定の履歴</h2><table><tr><th>基準日</th><th>事業者</th><th>使用状況</th><th>番号区画</th>"
                f"<th>出典</th></tr>{hist}</table><h2>変更</h2><ul>{ch}</ul>"
                f"<h2>番号区画の市区町村</h2><p>{e(munis) or '未登録'}</p>"
                f"<h2>公開施設で観測された町丁目</h2><ul>{obs or '<li>観測なし（利用されていないことを意味しない）</li>'}</ul>")


def render_dashboard(conn) -> str:
    rows = conn.execute(
        """SELECT prefecture_code, COUNT(DISTINCT town_key) AS towns,
                  COUNT(DISTINCT CASE WHEN label_status IN ('confirmed','candidate') THEN town_key END) AS uniq,
                  SUM(label_status IN ('confirmed','candidate')) AS gross,
                  COUNT(DISTINCT CASE WHEN label_status = 'insufficient_data' THEN town_key END) AS insufficient
           FROM regions.current_classification GROUP BY prefecture_code ORDER BY prefecture_code""").fetchall()
    trs = "".join(f"<tr><td>{e(r[0])}</td><td>{r[1]}</td><td>{r[2]}</td><td>{r[3]}</td><td>{r[4]}</td></tr>" for r in rows)
    return page("都道府県別ダッシュボード",
                "<table><tr><th>都道府県</th><th>町丁目数</th><th>一意該当地域数</th><th>延べ該当件数</th>"
                f"<th>資料不足を含む町丁目</th></tr>{trs}</table>")


def render_priority(conn) -> str:
    rows = conn.execute("SELECT * FROM priority_item WHERE priority_run_id = (SELECT MAX(priority_run_id)"
                        " FROM priority_run) ORDER BY rank LIMIT 300").fetchall()
    trs = "".join(f"<tr><td>{r['rank']}</td><td><a href='/code?{urlencode({'area': r['area_code'], 'local': r['local_code']})}'>"
                  f"{e(r['area_code'])}-{e(r['local_code'])}</a></td><td>{r['score']}</td><td>{LEVEL_JA.get(r['evidence_level'], e(r['evidence_level']))}"
                  f"</td><td class='muted'>{e(r['reasons'])}</td></tr>" for r in rows)
    return page("調査優先順位（指定済み局番のみ）",
                "<p class='muted'>番号の生成・総当たりは行いません。公開施設で確認する対象の優先順位です。</p>"
                f"<table><tr><th>順位</th><th>局番</th><th>スコア</th><th>根拠</th><th>理由</th></tr>{trs}</table>")


def make_handler(db_dir: Path):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            url = urlsplit(self.path)
            q = parse_qs(url.query)
            conn = connect(db_dir, read_only=True)
            try:
                if url.path == "/":
                    body = render_search(conn, q)
                elif url.path == "/town":
                    body = render_town(conn, _one(q, "key") or "")
                elif url.path == "/code":
                    body = render_code(conn, _one(q, "area") or "", _one(q, "local") or "")
                elif url.path == "/dashboard":
                    body = render_dashboard(conn)
                elif url.path == "/priority":
                    body = render_priority(conn)
                else:
                    self.send_error(404)
                    return
            finally:
                conn.close()
            data = body.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    return Handler


def serve(db_dir: Path, port: int = 8765) -> ThreadingHTTPServer:
    return ThreadingHTTPServer(("127.0.0.1", port), make_handler(db_dir))
