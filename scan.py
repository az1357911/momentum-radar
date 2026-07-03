#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
五日動能雷達 — 每日建檔腳本 (server-side, 無 CORS 問題)

流程：
  1. 找出最近一個交易日，抓當日「三大法人買賣超日報 (T86)」
  2. 取「外陸資買賣超」前 100 名 ∩「投信買賣超」前 100 名 的交集
  3. 對交集個股逐檔抓月成交資料，計算 EMA5/10/20，判斷是否多頭排列
  4. 把今日通過名單寫入 data/history/YYYY-MM-DD.json（供跨日累計用）
  5. 讀最近 5 個交易日的歷史，算出「五日累計動能榜」
  6. 全部結果寫入 docs/data.json，給前端 index.html 讀取

只用「官網」端點 (www.twse.com.tw)，因為 openapi.twse.com.tw 只有前一日資料。
所有欄位改用「欄位名稱」對應，不用寫死的 index，避免證交所調整欄位順序就壞掉。
"""

import json
import os
import sys
import time
import datetime as dt
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
HISTORY_DIR = ROOT / "data" / "history"
DOCS_DIR = ROOT / "docs"
HISTORY_DIR.mkdir(parents=True, exist_ok=True)
DOCS_DIR.mkdir(parents=True, exist_ok=True)

# ---- 參數 ----
TOP_N = 100          # 外資 / 投信 各取前幾名
MAX_EMA_CHECK = 30   # 交集後最多檢查幾檔的均線（避免請求過多）
STREAK_DAYS = 5      # 累計動能榜看幾個交易日
REQUEST_PAUSE = 0.4  # 每次請求之間停頓秒數，對證交所友善一點

# 上櫃（TPEx）預設關閉。TWSE 上市部分已完整可用。
# 要開啟上櫃需自行確認 TPEx OpenAPI 端點後補上 fetch_tpex_* ，見 README。
INCLUDE_TPEX = False

session = requests.Session()
session.headers.update({
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/124.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://www.twse.com.tw/",
})


def get_json(url, tries=4):
    """帶重試的 GET JSON。證交所偶爾會擋或逾時，多試幾次。"""
    last_err = None
    for i in range(tries):
        try:
            r = session.get(url, timeout=20)
            r.raise_for_status()
            return r.json()
        except Exception as e:  # noqa
            last_err = e
            time.sleep(1.5 * (i + 1))
    print(f"  ! 取得失敗 {url}\n    {last_err}")
    return None


def to_int(s):
    try:
        return int(str(s).replace(",", "").replace("+", "").strip())
    except Exception:
        return 0


def to_float(s):
    try:
        v = str(s).replace(",", "").strip()
        if v in ("", "--", "---", "N/A"):
            return None
        return float(v)
    except Exception:
        return None


def field_index(fields, *keywords_groups):
    """
    在 fields（欄位名稱陣列）中，找出符合條件的欄位 index。
    keywords_groups: 每組是 (必須包含的關鍵字list, 不可包含的關鍵字list)
    回傳第一個符合的 index，找不到回傳 None。
    """
    for must, forbid in keywords_groups:
        for i, name in enumerate(fields):
            nm = name.replace(" ", "")
            if all(k in nm for k in must) and not any(k in nm for k in forbid):
                return i
    return None


# ---------- 三大法人 (T86) ----------
def fetch_t86_for_date(yyyymmdd):
    """
    抓某一天的三大法人買賣超日報。回傳 (rows, fields) 或 (None, None)。
    官網端點附有 fields 欄位名稱，改用名稱對應抓外資/投信買賣超。
    """
    url = (f"https://www.twse.com.tw/rwd/zh/fund/T86"
           f"?date={yyyymmdd}&selectType=ALL&response=json")
    data = get_json(url)
    if not data:
        return None, None
    # 有些日期沒開盤，stat 會是「查詢日期...無資料」
    if data.get("stat") not in ("OK", None) and not data.get("data"):
        return None, None
    rows = data.get("data") or []
    fields = data.get("fields") or []
    if not rows or not fields:
        return None, None
    return rows, fields


def find_latest_trading_day():
    """
    從今天(台北)往回找，最多找 8 天，回傳 (yyyymmdd, rows, fields)。
    找不到回傳 (None, None, None)。
    """
    today = dt.datetime.utcnow() + dt.timedelta(hours=8)  # 台北時間
    for back in range(0, 8):
        d = today - dt.timedelta(days=back)
        if d.weekday() >= 5:  # 週六(5)、週日(6) 直接跳過
            continue
        ymd = d.strftime("%Y%m%d")
        print(f"  嘗試交易日 {ymd} ...")
        rows, fields = fetch_t86_for_date(ymd)
        if rows:
            print(f"  ✓ 找到交易日 {ymd}，共 {len(rows)} 檔法人資料")
            return ymd, rows, fields
        time.sleep(REQUEST_PAUSE)
    return None, None, None


def parse_institutional(rows, fields):
    """把 T86 rows 解析成 [{code,name,foreign,trust}]。"""
    idx_code = field_index(fields, (["證券代號"], []), (["代號"], []))
    idx_name = field_index(fields, (["證券名稱"], []), (["名稱"], []))
    # 「外陸資買賣超」五字本身就唯一（自營商那欄叫「外資自營商買賣超」，不含「外陸資」）
    idx_foreign = field_index(
        fields,
        (["外陸資買賣超"], []),
        (["外陸資", "買賣超"], []),
        (["外資買賣超"], ["自營"]),
    )
    idx_trust = field_index(fields, (["投信買賣超"], []), (["投信", "買賣超"], []))

    if None in (idx_code, idx_name, idx_foreign, idx_trust):
        print("  ! 欄位名稱對應失敗，請檢查 T86 欄位。fields =")
        print("   ", fields)
        return []

    out = []
    for row in rows:
        code = str(row[idx_code]).strip()
        name = str(row[idx_name]).strip()
        if not code or not code[0].isdigit():
            continue
        out.append({
            "code": code,
            "name": name,
            "foreign": to_int(row[idx_foreign]),
            "trust": to_int(row[idx_trust]),
        })
    return out


# ---------- 個股月成交（算 EMA 用）----------
def fetch_month_closes(code, yyyymm01):
    """抓某月份的每日收盤價（依日期由舊到新）。"""
    url = (f"https://www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY"
           f"?date={yyyymm01}&stockNo={code}&response=json")
    data = get_json(url)
    closes = []
    if not data or not data.get("data") or not data.get("fields"):
        return closes
    fields = data["fields"]
    idx_close = field_index(fields, (["收盤價"], []), (["收盤"], []))
    if idx_close is None:
        idx_close = 6  # STOCK_DAY 標準版面：收盤價在第 6 欄
    for row in data["data"]:
        c = to_float(row[idx_close])
        if c is not None:
            closes.append(c)
    return closes


def get_recent_closes(code, ref_date):
    """取參考日所在月 + 前一個月的收盤價，串成時間序列（舊→新）。"""
    d = dt.datetime.strptime(ref_date, "%Y%m%d")
    cur = d.strftime("%Y%m01")
    prev_month = (d.replace(day=1) - dt.timedelta(days=1))
    prev = prev_month.strftime("%Y%m01")

    closes = []
    for ym in (prev, cur):
        closes.extend(fetch_month_closes(code, ym))
        time.sleep(REQUEST_PAUSE)
    return closes


def ema(values, period):
    """標準 EMA：以前 period 筆的 SMA 當種子，再逐日遞推。"""
    if len(values) < period:
        return None
    sma = sum(values[:period]) / period
    e = sma
    k = 2 / (period + 1)
    for v in values[period:]:
        e = v * k + e * (1 - k)
    return e


def check_bullish(code, ref_date):
    """回傳 (bullish: True/False/None, last_close)。None 代表資料不足。"""
    closes = get_recent_closes(code, ref_date)
    last = closes[-1] if closes else None
    if len(closes) < 20:
        return None, last
    e5, e10, e20 = ema(closes, 5), ema(closes, 10), ema(closes, 20)
    if None in (e5, e10, e20):
        return None, last
    return (e5 > e10 > e20), last


# ---------- 主流程 ----------
def main():
    print("=== 五日動能雷達 建檔開始 ===")
    ref_date, rows, fields = find_latest_trading_day()
    if not rows:
        print("找不到可用交易日資料。")
        print("  近 8 天都抓不到 T86 → 極可能是證交所擋掉此機房 IP（GitHub Actions 常見）。")
        print("  對策：改在自己的 Windows/台灣 IP 電腦定時跑 scan.py 後 push（見 README）。")
        sys.exit(1)  # 讓 Action 明確變紅，不要假裝成功

    all_stocks = parse_institutional(rows, fields)
    if INCLUDE_TPEX:
        try:
            all_stocks += fetch_tpex_institutional(ref_date)  # noqa: F821
        except Exception as e:  # noqa
            print(f"  ! 上櫃資料略過：{e}")

    if not all_stocks:
        print("解析後無資料（欄位對應可能失敗），結束。")
        sys.exit(1)

    # 排名（以股數估算，非金額）
    by_foreign = sorted(all_stocks, key=lambda x: x["foreign"], reverse=True)[:TOP_N]
    by_trust = sorted(all_stocks, key=lambda x: x["trust"], reverse=True)[:TOP_N]
    foreign_rank = {s["code"]: i + 1 for i, s in enumerate(by_foreign)}
    trust_rank = {s["code"]: i + 1 for i, s in enumerate(by_trust)}

    inter = [s for s in all_stocks
             if s["code"] in foreign_rank and s["code"] in trust_rank]
    print(f"外資前{TOP_N} ∩ 投信前{TOP_N} 交集：{len(inter)} 檔")

    inter.sort(key=lambda s: foreign_rank[s["code"]] + trust_rank[s["code"]])
    capped = inter[:MAX_EMA_CHECK]
    if len(inter) > MAX_EMA_CHECK:
        print(f"交集過多，只檢查綜合排名前 {MAX_EMA_CHECK} 檔的均線")

    print("開始檢查 EMA5/10/20 多頭排列 ...")
    passed = []
    for s in capped:
        bullish, close = check_bullish(s["code"], ref_date)
        tag = ("多頭排列成立" if bullish is True
               else "資料不足" if bullish is None else "未成立")
        print(f"  {s['code']} {s['name']} → {tag}")
        if bullish is True:
            passed.append({
                "code": s["code"],
                "name": s["name"],
                "market": "TWSE",
                "foreignRank": foreign_rank[s["code"]],
                "trustRank": trust_rank[s["code"]],
                "close": close,
            })

    iso_date = dt.datetime.strptime(ref_date, "%Y%m%d").strftime("%Y-%m-%d")

    # 寫入當日歷史（供跨日累計）
    (HISTORY_DIR / f"{iso_date}.json").write_text(
        json.dumps(passed, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"已寫入歷史 {iso_date}.json，共 {len(passed)} 檔通過")

    # 讀最近 STREAK_DAYS 個交易日的歷史，做累計榜
    hist_files = sorted(HISTORY_DIR.glob("*.json"))
    recent_dates = [p.stem for p in hist_files][-STREAK_DAYS:]
    tally = {}
    for date in recent_dates:
        day = json.loads((HISTORY_DIR / f"{date}.json").read_text(encoding="utf-8"))
        for r in day:
            t = tally.setdefault(r["code"], {
                "code": r["code"], "name": r["name"],
                "days": [], "lastClose": r.get("close"),
            })
            t["days"].append(date)
            if r.get("close") is not None:
                t["lastClose"] = r["close"]

    streak = sorted(tally.values(), key=lambda t: len(t["days"]), reverse=True)[:15]
    for t in streak:
        t["count"] = len(t["days"])

    # 輸出給前端
    payload = {
        "generatedAt": (dt.datetime.utcnow() + dt.timedelta(hours=8)
                        ).strftime("%Y-%m-%d %H:%M"),
        "refDate": iso_date,
        "windowDates": recent_dates,
        "today": passed,
        "streak": streak,
        "totalRecords": len(hist_files),
    }
    (DOCS_DIR / "data.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print("已更新 docs/data.json")
    print("=== 完成 ===")


if __name__ == "__main__":
    main()
