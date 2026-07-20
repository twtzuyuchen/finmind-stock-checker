#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
台股「單一個股」融資維持率估算工具（獨立版）
================================================
這是一個完全獨立的小工具，不依賴任何其他 repo，用來查詢你指定的一檔或幾檔股票，
看「逐日融資成本回推」的完整過程，而不是只給你最終一個數字——方便你自己判斷這個
估算值可不可信、有沒有被種子值假設或某天異常買進影響太大。

背景與重要說明
--------------
台灣證交所 / 櫃買中心只公布「大盤（全市場）融資維持率」，並未公布「個股」融資維持率，
因為維持率本質上是「整戶」（單一投資人帳戶）的概念，且需要知道每一筆融資的實際
「融資成本」（買進成本），這個資料只有券商知道。

因此本程式採用市場上常見的「加權平均成本法」來 **估算** 個股融資維持率，方法與
XQ 全球贏家等看盤軟體公開說明的邏輯相同：

    融資成本(t) = 融資成本(t-1) × (今日餘額(t) - 今日買進(t)) / 今日餘額(t)
                  + 收盤價(t) × 今日買進(t) / 今日餘額(t)

    個股融資維持率(估) = 現在股價 / (融資成本 × 融資成數) × 100%

限制（請務必閱讀，網頁輸出也會顯示）：
 1. 「融資成本」是用最近 N 個交易日（預設 60 日）的資料回推的加權平均值，若融資部位
    是在回推區間「之前」就已經存在，成本的起始值只能用區間第一天的收盤價當種子值
    估計，可能與實際成本有落差（表格會標示哪一列是種子值）。
 2. 融資成數預設為 60%（一般上市櫃普通股），ETF 另外設為 90%；實際成數會因個股是否
    被列為「注意股 / 處置股 / 全額交割股」等而調整，本程式未逐一比對這些名單，
    只會在近期資料出現交易所備註標記時提醒你、並支援手動用 --financing-ratio 覆蓋。
 3. 這是 **估算值**，僅供篩選觀察使用，不是券商實際計算的整戶維持率，不構成投資
    建議，請勿作為單一交易依據。

資料來源：FinMind API（https://finmindtrade.com/）
 - TaiwanStockInfo                    → 股票名稱/產業別
 - TaiwanStockMarginPurchaseShortSale → 個股每日融資融券資料
 - TaiwanStockPrice                   → 個股每日收盤價

用法
----
    # 查一檔
    python scripts/check_stock.py 2330

    # 一次查多檔
    python scripts/check_stock.py 3138 6785 6568

    # 調整回推天數（預設 60 個交易日）
    python scripts/check_stock.py 3138 --lookback-days 90

    # 手動覆蓋融資成數（例如你已在交易所網站查到這檔被調降成 40%）
    python scripts/check_stock.py 6785 --financing-ratio 0.4

    # 把結果另存 CSV/JSON（除了網頁之外）
    python scripts/check_stock.py 2330 3138 --output-dir out/

    # 不想產生網頁的話
    python scripts/check_stock.py 2330 --no-html

環境變數（皆為選填）
--------
 FINMIND_TOKEN            FinMind API token（建議設定，可提高速率限制；單股查詢
                           每檔只要 2 次請求，匿名額度通常也很夠用）
 MARGIN_RATIO_THRESHOLD   顯示「低於門檻」提示用的門檻值，預設 130
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests

# --------------------------------------------------------------------------
# 設定
# --------------------------------------------------------------------------

FINMIND_BASE_URL = "https://api.finmindtrade.com/api/v4/data"
TAIPEI_TZ = timezone(timedelta(hours=8))

FINMIND_TOKEN = os.environ.get("FINMIND_TOKEN", "").strip()
MARGIN_RATIO_THRESHOLD = float(os.environ.get("MARGIN_RATIO_THRESHOLD", "130") or 130)

DEFAULT_FINANCING_RATIO = 0.6   # 一般上市櫃普通股融資成數（自備四成）
ETF_FINANCING_RATIO = 0.9       # ETF 融資成數（多數為九成，僅為概略假設）

# 單股查詢請求量很小，節流放寬鬆一點即可（有 token 用 500/hr、沒有用 250/hr）
REQUESTS_PER_HOUR = 500 if FINMIND_TOKEN else 250
MIN_SLEEP_SEC = 3600.0 / max(REQUESTS_PER_HOUR, 1)

MANIFEST_FILENAME = "manifest.json"


# --------------------------------------------------------------------------
# 節流 + 重試的 FinMind 呼叫（跟主篩選工具邏輯相同，這裡獨立一份，不依賴其他 repo）
# --------------------------------------------------------------------------

class RateLimiter:
    def __init__(self, min_interval: float):
        self.min_interval = min_interval
        self._last_call = 0.0

    def wait(self):
        now = time.monotonic()
        elapsed = now - self._last_call
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)
        self._last_call = time.monotonic()


_limiter = RateLimiter(MIN_SLEEP_SEC)
_session = requests.Session()


def _seconds_to_next_hour() -> float:
    now = datetime.now(timezone.utc)
    nxt = (now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1))
    return max((nxt - now).total_seconds(), 60.0)


def finmind_get(dataset: str, data_id: str = "", start_date: str = "",
                 end_date: str = "", max_retries: int = 5) -> list:
    """呼叫 FinMind API，內建節流與速率限制的重試/等待機制。"""
    params = {"dataset": dataset}
    if data_id:
        params["data_id"] = data_id
    if start_date:
        params["start_date"] = start_date
    if end_date:
        params["end_date"] = end_date
    if FINMIND_TOKEN:
        params["token"] = FINMIND_TOKEN

    for attempt in range(1, max_retries + 1):
        _limiter.wait()
        try:
            resp = _session.get(FINMIND_BASE_URL, params=params, timeout=30)
        except requests.RequestException as exc:
            print(f"  [warn] request error ({dataset} {data_id}): {exc}, retry {attempt}/{max_retries}")
            time.sleep(min(15 * attempt, 90))
            continue

        if resp.status_code == 200:
            try:
                payload = resp.json()
            except ValueError:
                print(f"  [warn] non-JSON response for {dataset} {data_id}, retry")
                time.sleep(10)
                continue
            msg = str(payload.get("msg", ""))
            if "reach api request limit" in msg.lower() or payload.get("status") == 402:
                wait_s = _seconds_to_next_hour() + 30
                print(f"  [rate-limit] hit FinMind hourly limit, sleeping {wait_s:.0f}s until next window...")
                time.sleep(wait_s)
                continue
            return payload.get("data", [])
        elif resp.status_code in (429, 402):
            wait_s = _seconds_to_next_hour() + 30
            print(f"  [rate-limit] HTTP {resp.status_code}, sleeping {wait_s:.0f}s...")
            time.sleep(wait_s)
            continue
        else:
            print(f"  [warn] HTTP {resp.status_code} for {dataset} {data_id}, retry {attempt}/{max_retries}")
            time.sleep(min(10 * attempt, 60))
            continue

    print(f"  [error] giving up on {dataset} {data_id} after {max_retries} retries")
    return []


def escape(s) -> str:
    if s is None:
        return ""
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


# --------------------------------------------------------------------------
# 核心計算：加權平均成本法回推
# --------------------------------------------------------------------------

def compute_est_cost(margin_rows: list, price_by_date: dict, trace: Optional[list] = None) -> tuple:
    """回傳 (最新融資餘額張數, 估計融資成本, 最新收盤價, note)。若無法估計回傳 (0, None, None, '')。

    若傳入 trace（一個 list），會把每一天的中間計算過程 append 進去，方便印出/產生網頁
    給使用者檢查完整回推過程。
    """
    if not margin_rows:
        return 0, None, None, ""

    margin_rows = sorted(margin_rows, key=lambda r: r.get("date", ""))
    cost_prev = None
    last_balance = 0.0
    last_close = None
    note = ""

    for i, row in enumerate(margin_rows):
        d = row.get("date", "")
        close = price_by_date.get(d)
        if close is None:
            continue  # 當日沒有價格資料（例如假日或資料缺漏），跳過
        try:
            balance_today = float(row.get("MarginPurchaseTodayBalance", 0) or 0)
            buy_today = float(row.get("MarginPurchaseBuy", 0) or 0)
        except (TypeError, ValueError):
            continue

        if row.get("Note"):
            note = str(row.get("Note"))

        if balance_today <= 0:
            cost_prev = None
            last_balance = 0.0
            last_close = close
            if trace is not None:
                trace.append({"date": d, "close": close, "buy": buy_today, "balance": balance_today,
                              "est_cost": None, "note": row.get("Note") or "", "seed": False})
            continue

        seed = cost_prev is None or i == 0
        if seed:
            cost_today = close  # 種子值：假設區間第一天的部位是用當天收盤價買的
        elif buy_today >= balance_today:
            cost_today = close
        else:
            carried = balance_today - buy_today
            cost_today = (cost_prev * carried + close * buy_today) / balance_today

        if trace is not None:
            trace.append({"date": d, "close": close, "buy": buy_today, "balance": balance_today,
                          "est_cost": round(cost_today, 2), "note": row.get("Note") or "", "seed": seed})

        cost_prev = cost_today
        last_balance = balance_today
        last_close = close

    return last_balance, cost_prev, last_close, note


def fetch_stock_meta(stock_id: str) -> dict:
    rows = finmind_get("TaiwanStockInfo", data_id=stock_id)
    if not rows:
        return {}
    # 可能有多筆歷史紀錄（改名、轉板等），取最後一筆
    return sorted(rows, key=lambda r: r.get("date", ""))[-1]


def check_one(stock_id: str, lookback_days: int, financing_ratio_override: Optional[float]) -> dict:
    end_date = datetime.now(TAIPEI_TZ).strftime("%Y-%m-%d")
    start_date = (datetime.now(TAIPEI_TZ) - timedelta(days=int(lookback_days * 1.6) + 10)).strftime("%Y-%m-%d")

    print(f"\n{'=' * 70}")
    print(f"股票代號: {stock_id}　查詢區間: {start_date} ~ {end_date}")
    print(f"{'=' * 70}")

    meta = fetch_stock_meta(stock_id)
    stock_name = meta.get("stock_name", "(查無名稱，代號可能有誤)")
    industry = meta.get("industry_category", "")
    market = meta.get("type", "")
    print(f"名稱: {stock_name}　產業別: {industry}")

    margin_rows = finmind_get("TaiwanStockMarginPurchaseShortSale", data_id=stock_id,
                               start_date=start_date, end_date=end_date)
    if not margin_rows:
        print(f"[錯誤] 查不到 {stock_id} 的融資資料（FinMind 回傳空值）。"
              f"請確認代號是否正確、或該股票是否本來就沒有融資交易資格。")
        return {"stock_id": stock_id, "stock_name": stock_name, "market": market, "error": "no_margin_data"}

    price_rows = finmind_get("TaiwanStockPrice", data_id=stock_id,
                              start_date=start_date, end_date=end_date)
    price_by_date = {r.get("date"): r.get("close") for r in price_rows if r.get("close") is not None}
    if not price_by_date:
        print(f"[錯誤] 查不到 {stock_id} 的股價資料。")
        return {"stock_id": stock_id, "stock_name": stock_name, "market": market, "error": "no_price_data"}

    trace: list = []
    balance, cost, close, note = compute_est_cost(margin_rows, price_by_date, trace=trace)

    print(f"\n{'日期':<12}{'收盤價':>10}{'融資買進':>10}{'融資餘額':>10}{'估計成本':>10}  備註")
    print("-" * 70)
    for row in trace[-lookback_days:]:
        seed_mark = " ← 種子值(假設起點)" if row["seed"] else ""
        cost_str = f"{row['est_cost']:.2f}" if row["est_cost"] is not None else "－(無部位)"
        print(f"{row['date']:<12}{row['close']:>10.2f}{row['buy']:>10.0f}"
              f"{row['balance']:>10.0f}{cost_str:>10}  {row['note']}{seed_mark}")

    if not balance or balance <= 0 or not cost or not close:
        print(f"\n[結果] {stock_id} {stock_name} 目前融資餘額為 0（或資料不足），"
              f"沒有維持率可以估算。")
        return {"stock_id": stock_id, "stock_name": stock_name, "market": market, "error": "no_balance"}

    if financing_ratio_override is not None:
        financing_ratio = financing_ratio_override
        ratio_source = "手動指定"
    else:
        financing_ratio = ETF_FINANCING_RATIO if industry == "ETF" else DEFAULT_FINANCING_RATIO
        ratio_source = "程式預設（ETF 90% / 一般 60%）"

    ratio_pct = close / (cost * financing_ratio) * 100.0

    print(f"\n[結果]")
    print(f"  現在股價　　　: {close:,.2f}")
    print(f"  融資餘額(張)　: {balance:,.0f}")
    print(f"  估計融資成本　: {cost:,.2f}")
    print(f"  融資成數　　　: {financing_ratio*100:.0f}%　（{ratio_source}）")
    print(f"  估算融資維持率: {ratio_pct:.1f}%"
          + (f"　← 低於門檻 {MARGIN_RATIO_THRESHOLD:.0f}%" if ratio_pct < MARGIN_RATIO_THRESHOLD else ""))
    if note:
        print(f"  備註（近期曾出現）: {note}　"
              f"⚠️ 這檔股票曾被交易所標記，實際融資成數可能與預設值不同，建議自行查證")

    return {
        "stock_id": stock_id,
        "stock_name": stock_name,
        "market": market,
        "industry": industry,
        "close_price": close,
        "margin_balance_lots": balance,
        "est_cost": round(cost, 2),
        "financing_ratio": financing_ratio,
        "ratio_source": ratio_source,
        "ratio_pct": round(ratio_pct, 2),
        "note": note,
        "daily_trace": trace,
    }


# --------------------------------------------------------------------------
# 網頁輸出
# --------------------------------------------------------------------------

PAGE_CSS = """
  :root {
    --bg: #0f1420; --panel:#161d2e; --border:#2a3450; --text:#e7ecf7; --muted:#93a0bd;
    --accent:#4f8cff; --high:#ff5470; --mid:#ffb454; --good:#3ddc97;
  }
  * { box-sizing: border-box; }
  body {
    margin:0; font-family: -apple-system, "PingFang TC", "Microsoft JhengHei", Segoe UI, sans-serif;
    background: var(--bg); color: var(--text); padding: 24px 16px 60px;
  }
  .wrap { max-width: 1100px; margin: 0 auto; }
  h1 { font-size: 1.5rem; margin-bottom: 4px; }
  h2 { font-size: 1.15rem; margin: 28px 0 10px; }
  .meta { color: var(--muted); font-size: 0.9rem; margin-bottom: 18px; }
  .disclaimer {
    background: var(--panel); border: 1px solid var(--border); border-radius: 10px;
    padding: 14px 16px; font-size: 0.85rem; color: var(--muted); line-height:1.6; margin-bottom: 22px;
  }
  .disclaimer b { color: var(--mid); }
  .stat-row { display:flex; gap:12px; margin-bottom: 18px; flex-wrap: wrap; }
  .stat {
    background: var(--panel); border:1px solid var(--border); border-radius: 10px;
    padding: 12px 18px; min-width: 140px;
  }
  .stat .n { font-size: 1.6rem; font-weight: 700; }
  .stat .l { font-size: 0.78rem; color: var(--muted); }
  table { width:100%; border-collapse: collapse; background: var(--panel); border-radius: 10px; overflow:hidden; }
  th, td { padding: 9px 10px; border-bottom: 1px solid var(--border); font-size: 0.86rem; text-align:left; }
  th { background:#1c2540; color: var(--muted); font-weight:600; position: sticky; top:0; }
  td.num { text-align:right; font-variant-numeric: tabular-nums; }
  tr.seed-row td { opacity: 0.7; font-style: italic; }
  tr:hover { background: rgba(79,140,255,0.08); }
  .risk-high { color: var(--high); }
  .risk-mid { color: var(--mid); }
  .note { font-size: 0.78rem; color: var(--mid); }
  .empty { padding: 40px; text-align:center; color: var(--muted); }
  footer { margin-top: 24px; color: var(--muted); font-size: 0.78rem; }
  a { color: var(--accent); }
  .table-wrap { overflow-x:auto; }
  .back-link { display:inline-block; margin-bottom: 14px; font-size: 0.85rem; }
  .card-grid { display:grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 12px; }
  .card {
    background: var(--panel); border:1px solid var(--border); border-radius: 10px;
    padding: 14px 16px; display:block; text-decoration:none; color: var(--text);
  }
  .card:hover { border-color: var(--accent); }
  .card .sid { color: var(--muted); font-size: 0.78rem; }
  .card .sname { font-size: 1rem; font-weight:600; margin: 2px 0 8px; }
  .card .sratio { font-size: 1.3rem; font-weight:700; }
"""


def render_stock_page(result: dict, generated_at: datetime) -> str:
    sid = result.get("stock_id", "")
    name = result.get("stock_name", "")
    error = result.get("error")
    updated_str = generated_at.strftime("%Y-%m-%d %H:%M (台北時間)")

    if error:
        body = f"""
  <div class="disclaimer">
    <b>查詢失敗：</b>{escape(error)}
  </div>"""
    else:
        ratio_pct = result["ratio_pct"]
        risk_class = "risk-high" if ratio_pct < 120 else ("risk-mid" if ratio_pct < 130 else "")
        below = ratio_pct < MARGIN_RATIO_THRESHOLD

        rows_html = []
        for row in result.get("daily_trace", []):
            cost_str = f"{row['est_cost']:.2f}" if row["est_cost"] is not None else "－"
            seed_class = " seed-row" if row["seed"] else ""
            note_html = escape(row["note"]) if row["note"] else ""
            seed_tag = ' <span class="note">← 種子值</span>' if row["seed"] else ""
            rows_html.append(f"""
        <tr class="{seed_class}">
          <td>{escape(row['date'])}</td>
          <td class="num">{row['close']:,.2f}</td>
          <td class="num">{row['buy']:,.0f}</td>
          <td class="num">{row['balance']:,.0f}</td>
          <td class="num">{cost_str}</td>
          <td>{note_html}{seed_tag}</td>
        </tr>""")

        body = f"""
  <div class="disclaimer">
    <b>請注意：</b>這是用<b>加權平均成本法</b>回推的估算值（現價 ÷ (估計融資成本 × 融資成數)），
    不是券商實際計算的整戶維持率，僅供觀察參考，不構成投資建議。表格中「種子值」列代表
    回推區間第一天，程式假設當天部位是用當天收盤價買進，越早的部位、種子值誤差可能越大。
  </div>

  <div class="stat-row">
    <div class="stat"><div class="n">{result['close_price']:,.2f}</div><div class="l">目前股價</div></div>
    <div class="stat"><div class="n">{result['margin_balance_lots']:,.0f}</div><div class="l">融資餘額(張)</div></div>
    <div class="stat"><div class="n">{result['est_cost']:,.2f}</div><div class="l">估計融資成本</div></div>
    <div class="stat"><div class="n">{result['financing_ratio']*100:.0f}%</div><div class="l">融資成數（{escape(result['ratio_source'])}）</div></div>
    <div class="stat"><div class="n {risk_class}">{ratio_pct:.1f}%</div><div class="l">估算融資維持率{'（低於門檻）' if below else ''}</div></div>
  </div>

  {'<div class="disclaimer"><b>⚠️ 備註：</b>近期曾出現交易所標記「' + escape(result.get('note','')) + '」，實際融資成數可能與預設值不同，建議自行查證。</div>' if result.get('note') else ''}

  <h2>逐日融資成本回推明細</h2>
  <div class="table-wrap">
  <table>
    <thead><tr>
      <th>日期</th><th>收盤價</th><th>融資買進</th><th>融資餘額</th><th>估計成本</th><th>備註</th>
    </tr></thead>
    <tbody>{"".join(rows_html)}</tbody>
  </table>
  </div>"""

    return f"""<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{escape(sid)} {escape(name)} 融資維持率估算明細</title>
<style>{PAGE_CSS}</style>
</head>
<body>
<div class="wrap">
  <a class="back-link" href="index.html">← 回查詢列表</a>
  <h1>{escape(sid)} {escape(name)}　融資維持率估算明細</h1>
  <div class="meta">更新時間：{updated_str}　｜　資料來源：
    <a href="https://finmindtrade.com/" target="_blank" rel="noopener">FinMind API</a></div>
  {body}
  <footer>本頁由 scripts/check_stock.py 產生，計算方法與限制請見 repo README。</footer>
</div>
</body>
</html>
"""


def load_manifest(html_dir: str) -> list:
    path = os.path.join(html_dir, MANIFEST_FILENAME)
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return []
    return []


def save_manifest(html_dir: str, manifest: list):
    path = os.path.join(html_dir, MANIFEST_FILENAME)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)


def upsert_manifest(manifest: list, entry: dict) -> list:
    manifest = [m for m in manifest if m.get("stock_id") != entry["stock_id"]]
    manifest.append(entry)
    return manifest


def render_index_page(manifest: list) -> str:
    manifest = sorted(manifest, key=lambda m: (m.get("ratio_pct") is None, m.get("ratio_pct", 0)))
    cards = []
    for m in manifest:
        if m.get("error"):
            cards.append(f"""
        <a class="card" href="{escape(m['stock_id'])}.html">
          <div class="sid">{escape(m['stock_id'])}</div>
          <div class="sname">{escape(m.get('stock_name') or '')}</div>
          <div class="sratio">查詢失敗</div>
        </a>""")
            continue
        ratio_pct = m.get("ratio_pct")
        risk_class = "risk-high" if ratio_pct is not None and ratio_pct < 120 else \
                     ("risk-mid" if ratio_pct is not None and ratio_pct < 130 else "")
        cards.append(f"""
        <a class="card" href="{escape(m['stock_id'])}.html">
          <div class="sid">{escape(m['stock_id'])}　{escape(m.get('market','').upper())}</div>
          <div class="sname">{escape(m.get('stock_name') or '')}</div>
          <div class="sratio {risk_class}">{ratio_pct:.1f}%</div>
        </a>""")

    empty_html = '<div class="empty">目前還沒有查過任何個股，執行 check_stock.py 後這裡會列出來。</div>'

    return f"""<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>個股融資維持率查詢列表</title>
<style>{PAGE_CSS}</style>
</head>
<body>
<div class="wrap">
  <h1>個股融資維持率查詢列表（估算值）</h1>
  <div class="meta">點進任一檔可以看逐日融資成本回推明細　｜　共 {len(manifest)} 檔查詢紀錄　｜
    資料來源：<a href="https://finmindtrade.com/" target="_blank" rel="noopener">FinMind API</a></div>

  <div class="disclaimer">
    <b>請注意：</b>台灣證交所／櫃買中心只公布「大盤」融資維持率，並未公布個股數值。
    本頁的「個股融資維持率」是用近期融資買進與收盤價，以<b>加權平均成本法</b>回推估算，
    <b>非券商實際計算的整戶維持率</b>，僅供觀察篩選參考，不構成投資建議，請自行查證並謹慎判斷。
  </div>

  {empty_html if not manifest else '<div class="card-grid">' + "".join(cards) + '</div>'}
  <footer>本頁由 scripts/check_stock.py 產生與更新，每次查詢新的股票代號都會加進這個列表；
    在 GitHub Actions 頁面手動輸入代號觸發，或在自己電腦執行
    <code>python scripts/check_stock.py 股票代號</code> 都可以更新這裡。</footer>
</div>
</body>
</html>
"""


def write_html_outputs(results: list, html_dir: str, generated_at: datetime):
    os.makedirs(html_dir, exist_ok=True)
    manifest = load_manifest(html_dir)

    for r in results:
        sid = r.get("stock_id")
        if not sid:
            continue
        page = render_stock_page(r, generated_at)
        with open(os.path.join(html_dir, f"{sid}.html"), "w", encoding="utf-8") as f:
            f.write(page)

        entry = {
            "stock_id": sid,
            "stock_name": r.get("stock_name", ""),
            "market": r.get("market", ""),
            "ratio_pct": r.get("ratio_pct"),
            "error": r.get("error"),
            "updated_at": generated_at.isoformat(),
        }
        manifest = upsert_manifest(manifest, entry)

    save_manifest(html_dir, manifest)
    with open(os.path.join(html_dir, "index.html"), "w", encoding="utf-8") as f:
        f.write(render_index_page(manifest))

    print(f"\n已產生網頁：{html_dir}/index.html（查詢列表）"
          + "".join(f"、{html_dir}/{r.get('stock_id')}.html" for r in results if r.get("stock_id")))


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="台股個股融資維持率估算（獨立版，逐日回推 + 產生網頁）")
    parser.add_argument("stock_ids", nargs="+", help="股票代號，可一次輸入多個，用空白分隔")
    parser.add_argument("--lookback-days", type=int, default=60, help="回推估算用的交易日數（預設 60）")
    parser.add_argument("--financing-ratio", type=float, default=None,
                         help="手動指定融資成數（例如 0.4），不指定則用程式預設值")
    parser.add_argument("--output-dir", default=None, help="若指定，會把結果另存 CSV/JSON 到這個資料夾")
    parser.add_argument("--html-dir", default="docs",
                         help="產生網頁的輸出資料夾（預設 docs，搭配 GitHub Pages 可直接看）")
    parser.add_argument("--no-html", action="store_true", help="不要產生網頁，只印在畫面上/存 CSV/JSON")
    args = parser.parse_args()

    print(f"FinMind token: {'已設定' if FINMIND_TOKEN else '未設定（匿名額度，單股查詢通常足夠）'}")

    results = []
    for sid in args.stock_ids:
        sid = sid.strip()
        if not sid:
            continue
        try:
            res = check_one(sid, args.lookback_days, args.financing_ratio)
        except Exception as exc:  # noqa: BLE001
            print(f"[錯誤] 查詢 {sid} 時發生例外: {exc}")
            res = {"stock_id": sid, "error": str(exc)}
        results.append(res)

    if args.output_dir:
        os.makedirs(args.output_dir, exist_ok=True)
        json_path = os.path.join(args.output_dir, "check_stock_result.json")
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=2)

        csv_path = os.path.join(args.output_dir, "check_stock_result.csv")
        with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f)
            writer.writerow(["股票代號", "名稱", "產業別", "收盤價", "融資餘額(張)",
                              "估計成本", "融資成數", "成數來源", "估算維持率(%)", "備註", "錯誤"])
            for r in results:
                writer.writerow([
                    r.get("stock_id", ""), r.get("stock_name", ""), r.get("industry", ""),
                    r.get("close_price", ""), r.get("margin_balance_lots", ""),
                    r.get("est_cost", ""), r.get("financing_ratio", ""), r.get("ratio_source", ""),
                    r.get("ratio_pct", ""), r.get("note", ""), r.get("error", ""),
                ])
        print(f"\n已將結果存到 {json_path} 與 {csv_path}")

    if not args.no_html:
        generated_at = datetime.now(TAIPEI_TZ)
        write_html_outputs(results, args.html_dir, generated_at)

    print(f"\n{'=' * 70}")
    print("提醒：以上都是加權平均成本法回推的估算值，不是券商實際計算的整戶維持率，"
          "僅供觀察參考，不構成投資建議。")


if __name__ == "__main__":
    main()
