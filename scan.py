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

import csv
import io
import json
import sys
import time
import datetime as dt
from pathlib import Path

import requests

TAIPEI = dt.timezone(dt.timedelta(hours=8))  # 台北時區 (UTC+8)

ROOT = Path(__file__).resolve().parent
HISTORY_DIR = ROOT / "data" / "history"
DOCS_DIR = ROOT / "docs"
HISTORY_DIR.mkdir(parents=True, exist_ok=True)
DOCS_DIR.mkdir(parents=True, exist_ok=True)

# ---- 參數 ----
TOP_N = 100          # 外資 / 投信 各取前幾名
MAX_EMA_CHECK = 30   # 交集後最多檢查幾檔的均線（避免請求過多）
STREAK_DAYS = 5      # 累計動能榜看幾個交易日
REQUEST_PAUSE = 0.8  # 每次請求之間停頓秒數。GitHub 機房 IP 打太快會被證交所擋，放慢一點
MIN_DATA_OK_RATIO = 0.7  # 交集個股中，能取得足夠均線資料的比例低於此 → 判定被限流、不覆蓋資料

# 上櫃（TPEx）：已驗證櫃買 OpenAPI 為當日資料，可與上市合併排名。
# 三大法人 tpex_3insti_daily_trading、行情 tpex_mainboard_daily_close_quotes、
# 股利 mopsfin_t187ap39_O、個股歷史 tradingStock，均為當日/可用。
INCLUDE_TPEX = True

session = requests.Session()
session.headers.update({
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/124.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://www.twse.com.tw/",
})


def get_json(url, tries=5):
    """帶重試的 GET JSON。證交所被限流時，用漸進式退避等它解除（3,6,12,20s）。"""
    last_err = None
    for i in range(tries):
        try:
            r = session.get(url, timeout=25)
            r.raise_for_status()
            return r.json()
        except Exception as e:  # noqa
            last_err = e
            if i < tries - 1:
                time.sleep(min(3 * (2 ** i), 20))  # 3,6,12,20,20 — 給限流時間解除
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
    today = dt.datetime.now(TAIPEI)  # 台北時間
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
            "market": "TWSE",
        })
    return out


# ---------- 個股日收盤（算均線用，帶日期以便還原股價）----------
def _roc_iso(s):
    """民國日期 '115/07/01' 或 '115年07月01日' → 西元 '2026-07-01'。"""
    s = str(s).replace("年", "/").replace("月", "/").replace("日", "").strip()
    y, m, d = s.split("/")
    return f"{int(y) + 1911}-{int(m):02d}-{int(d):02d}"


def fetch_month_closes(code, yyyymm01):
    """上市個股某月每日 (iso_date, 收盤價)，由舊到新。"""
    url = (f"https://www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY"
           f"?date={yyyymm01}&stockNo={code}&response=json")
    data = get_json(url)
    out = []
    if not data or not data.get("data") or not data.get("fields"):
        return out
    fields = data["fields"]
    idx_close = field_index(fields, (["收盤價"], []), (["收盤"], []))
    if idx_close is None:
        idx_close = 6  # STOCK_DAY 標準版面：收盤價在第 6 欄
    for row in data["data"]:
        c = to_float(row[idx_close])
        if c is not None:
            try:
                out.append((_roc_iso(row[0]), c))
            except Exception:
                pass
    return out


def fetch_tpex_month_closes(code, ad_year_month):
    """上櫃個股某月每日 (iso_date, 收盤價)（tradingStock，date 用西元 YYYY/MM/01）。"""
    url = ("https://www.tpex.org.tw/www/zh-tw/afterTrading/tradingStock"
           f"?code={code}&date={ad_year_month}&response=json&id=")
    data = get_json(url)
    out = []
    try:
        table = (data.get("tables") or [{}])[0]
    except Exception:
        return out
    fields = table.get("fields") or []
    rows = table.get("data") or []
    idx_close = field_index(fields, (["收盤"], []))
    if idx_close is None:
        idx_close = 6  # 個股日成交：收盤在第 6 欄
    for row in rows:
        if idx_close < len(row):
            c = to_float(row[idx_close])
            if c is not None:
                try:
                    out.append((_roc_iso(row[0]), c))
                except Exception:
                    pass
    return out


def get_recent_closes(code, ref_date, market="TWSE", months=5):
    """
    取參考日往回 N 個月的 (iso_date, 收盤價)，由舊到新。
    要算 SMA60（季線）需 ≥60 筆；抓 5 個月（約 100 個交易日）留足緩衝，
    同時比 6 個月少一批請求、降低被證交所限流的機會。
    上市走 STOCK_DAY、上櫃走 tradingStock。
    """
    first = dt.datetime.strptime(ref_date, "%Y%m%d").replace(day=1)
    months_list = []
    d = first
    for _ in range(months):
        months_list.append(d)
        d = (d - dt.timedelta(days=1)).replace(day=1)
    months_list.reverse()  # 舊 → 新

    dated = []
    for m in months_list:
        if market == "TPEx":
            dated.extend(fetch_tpex_month_closes(code, m.strftime("%Y/%m/01")))
        else:
            dated.extend(fetch_month_closes(code, m.strftime("%Y%m01")))
        time.sleep(REQUEST_PAUSE)
    return dated


def adjust_closes(dated, events):
    """
    還原股價：events=[(ex_iso, factor)]，factor=除權息參考價/前收盤(<1)。
    某天的價格乘上所有『發生在它之後』的除權息 factor 連乘，把未來的除息缺口
    往回還原，讓均線不被除權息跳空破壞（對齊 CMoney 用還原股價算均線）。
    回傳還原後的收盤序列（舊→新）。
    """
    if not events:
        return [px for _, px in dated]
    out = []
    for date, px in dated:
        f = 1.0
        for ex_iso, factor in events:
            if ex_iso > date:
                f *= factor
        out.append(px * f)
    return out


def sma(values, period):
    """簡單移動平均：最近 period 筆的平均。"""
    if len(values) < period:
        return None
    return sum(values[-period:]) / period


def check_bullish(code, ref_date, market="TWSE", ex_factors=None):
    """
    均線多頭排列（對齊 CMoney）：在『還原股價』下，股價 > 5MA > 20MA > 60MA。
    回傳 (bullish: True/False/None, last_close)。None 代表資料不足。
    """
    dated = get_recent_closes(code, ref_date, market=market)
    last = dated[-1][1] if dated else None
    closes = adjust_closes(dated, (ex_factors or {}).get(code, []))
    if len(closes) < 60:  # 季線(SMA60)需要 60 筆
        return None, last
    s5, s20, s60 = sma(closes, 5), sma(closes, 20), sma(closes, 60)
    if None in (s5, s20, s60):
        return None, last
    return (closes[-1] > s5 > s20 > s60), last


# ---------- 除權除息（還原股價用）----------
TWT49U_URL = "https://www.twse.com.tw/rwd/zh/exRight/TWT49U"


def fetch_twse_ex_factors(ref_date, lookback_days=210):
    """
    上市除權除息計算結果表(TWT49U)近 ~7 個月 → {code: [(ex_iso, factor)]}。
    factor = 除權息參考價 / 除權息前收盤價（<1，含除息與除權）。一次抓一段區間。
    抓不到就回傳 {}，均線改用原始股價（不影響流程）。
    """
    end = ref_date
    start = (dt.datetime.strptime(ref_date, "%Y%m%d")
             - dt.timedelta(days=lookback_days)).strftime("%Y%m%d")
    data = get_json(f"{TWT49U_URL}?startDate={start}&endDate={end}&response=json")
    out = {}
    if not data or not data.get("data") or not data.get("fields"):
        print("  ! 除權除息表(TWT49U)抓取失敗，均線改用原始股價")
        return out
    F = data["fields"]

    def col(name):
        for i, f in enumerate(F):
            if name in f.replace(" ", ""):
                return i
        return None

    ic, idate, ib, ir = (col("股票代號"), col("資料日期"),
                         col("除權息前收盤價"), col("除權息參考價"))
    if None in (ic, idate, ib, ir):
        print("  ! 除權除息表欄位對應失敗，均線改用原始股價")
        return out
    for r in data["data"]:
        try:
            before, ref = to_float(r[ib]), to_float(r[ir])
            if before and ref and before > 0:
                out.setdefault(str(r[ic]).strip(), []).append(
                    (_roc_iso(r[idate]), ref / before))
        except Exception:
            pass
    print(f"  除權除息(近7月)：{sum(len(v) for v in out.values())} 筆 / {len(out)} 檔（供還原股價）")
    return out


# ---------- 全市場成交均價（把買賣超股數換算成金額用）----------
STOCK_DAY_ALL_URL = ("https://www.twse.com.tw/rwd/zh/afterTrading/"
                     "STOCK_DAY_ALL?response=json")


def fetch_vwap_map():
    """
    抓 STOCK_DAY_ALL（全上市當日成交），回傳 {code: vwap}。
    vwap = 成交金額 / 成交股數（當日成交均價）。用它把『買賣超股數』換算成
    『買賣超金額』，對齊 CMoney 等工具的「外資/投信買超金額前 100」排名
    （實測台光電外資金額用此法算出與 CMoney 完全吻合）。
    這支 rwd 端點回傳 CSV（非 JSON），且有『當日』資料（openapi 只有前一日）。
    欄位一律用「名稱」定位，不寫死 index。抓不到回傳 {}。
    """
    text = None
    for i in range(4):
        try:
            r = session.get(STOCK_DAY_ALL_URL, timeout=30)
            r.raise_for_status()
            text = r.text
            break
        except Exception as e:  # noqa
            time.sleep(1.5 * (i + 1))
    if not text:
        print("  ! STOCK_DAY_ALL 抓取失敗，本次無法算金額")
        return {}
    reader = csv.reader(io.StringIO(text))
    header = next(reader, None)
    if not header:
        return {}
    col = {name.strip().strip('"'): i for i, name in enumerate(header)}
    ic, iv, ivol = col.get("證券代號"), col.get("成交金額"), col.get("成交股數")
    if None in (ic, iv, ivol):
        print("  ! STOCK_DAY_ALL 欄位對應失敗，本次無法算金額")
        return {}
    out = {}
    for row in reader:
        if len(row) <= max(ic, iv, ivol):
            continue
        code = row[ic].strip().strip('"')
        val = to_float(row[iv].strip().strip('"'))
        vol = to_float(row[ivol].strip().strip('"'))
        if code and val and vol and vol > 0:
            out[code] = val / vol
    return out


# ---------- 上櫃（TPEx / 櫃買中心）----------
# 全部走櫃買 OpenAPI（實測為『當日』資料，與上市同一天，可合併排名），
# 個股歷史收盤走 tradingStock。欄位一律用名稱比對。
TPEX_INSTI_URL = "https://www.tpex.org.tw/openapi/v1/tpex_3insti_daily_trading"
TPEX_QUOTES_URL = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes"
TPEX_DIV_URL = "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap39_O"


def _roc_date(yyyymmdd):
    """20260703 -> '1150703'（民國）。"""
    return f"{int(yyyymmdd[:4]) - 1911}{yyyymmdd[4:]}"


def _pick_key(keys, *must, forbid=()):
    """在 keys 裡找第一個（去空白後）含所有 must、不含任何 forbid 的鍵名。"""
    for k in keys:
        kk = k.replace(" ", "")
        if all(m in kk for m in must) and not any(f in kk for f in forbid):
            return k
    return None


def fetch_tpex_institutional(ref_date):
    """上櫃三大法人（openapi，當日）。回傳 [{code,name,foreign,trust,market:'TPEx'}]。"""
    data = get_json(TPEX_INSTI_URL)
    if not isinstance(data, list) or not data:
        print("  ! 上櫃三大法人抓取失敗，略過上櫃")
        return []
    keys = list(data[0].keys())
    kdate = _pick_key(keys, "Date")
    kcode = _pick_key(keys, "SecuritiesCompanyCode") or _pick_key(keys, "Code")
    kname = _pick_key(keys, "CompanyName")
    # 外陸資(不含外資自營商)買賣超：含 Foreign+Mainland+excluded+Difference
    kforeign = _pick_key(keys, "Foreign", "Mainland", "excluded", "Difference")
    ktrust = _pick_key(keys, "InvestmentTrust", "Difference")
    if None in (kcode, kname, kforeign, ktrust):
        print("  ! 上櫃三大法人欄位對應失敗，略過上櫃")
        return []
    roc = _roc_date(ref_date)
    out = []
    for r in data:
        if kdate and str(r.get(kdate)).strip() != roc:
            continue  # 只取與上市同一交易日，避免混到不同日期
        code = str(r.get(kcode, "")).strip()
        if not code or not code[0].isdigit():
            continue
        out.append({
            "code": code,
            "name": str(r.get(kname, "")).strip(),
            "foreign": to_int(r.get(kforeign)),
            "trust": to_int(r.get(ktrust)),
            "market": "TPEx",
        })
    print(f"  上櫃三大法人：{len(out)} 檔"
          if out else f"  ! 上櫃當日({roc})無三大法人資料，略過上櫃")
    return out


def fetch_tpex_vwap_map(ref_date):
    """上櫃每日行情 → {code: 均價(Average)}。"""
    data = get_json(TPEX_QUOTES_URL)
    if not isinstance(data, list) or not data:
        return {}
    keys = list(data[0].keys())
    kdate = _pick_key(keys, "Date")
    kcode = _pick_key(keys, "SecuritiesCompanyCode") or _pick_key(keys, "Code")
    kavg = _pick_key(keys, "Average")
    if None in (kcode, kavg):
        return {}
    roc = _roc_date(ref_date)
    out = {}
    for r in data:
        if kdate and str(r.get(kdate)).strip() != roc:
            continue
        code = str(r.get(kcode, "")).strip()
        avg = to_float(r.get(kavg))
        if code and avg:
            out[code] = avg
    return out


def fetch_tpex_cash_dividend_map():
    """上櫃股利分派 (mopsfin_t187ap39_O) → {code:{amt,year}}，邏輯同上市。"""
    data = get_json(TPEX_DIV_URL)
    if not isinstance(data, list) or not data:
        return {}
    cash_keys = [k for k in data[0].keys() if "現金" in k and "元/股" in k]
    if not cash_keys:
        return {}
    out = {}
    for r in data:
        code = str(r.get("公司代號", "")).strip()
        if not code:
            continue
        amt = sum((to_float(r.get(k)) or 0) for k in cash_keys)
        cur = out.get(code)
        if cur is None or amt > cur["amt"]:
            try:
                year_ad = int(r.get("股利年度")) + 1911
            except Exception:
                year_ad = None
            out[code] = {"amt": round(amt, 2), "year": year_ad}
    return out


# ---------- 股利（自選按鈕「只看有配息」用）----------
DIVIDEND_URL = "https://openapi.twse.com.tw/v1/opendata/t187ap45_L"


def fetch_cash_dividend_map():
    """
    抓上市公司股利分派 (t187ap45_L)，回傳 {code: {"amt": float, "year": 西元int}}。
    amt = 每股現金股利合計（盈餘分配 + 法定盈餘公積 + 資本公積 三欄相加）。
    同一家可能有多筆（不同期別/年度），取現金合計最大的那筆，避免抓到 0 元的預告筆。
    欄位一律用「名稱」比對（含『現金』且『元/股』），不用寫死 index。
    注意：這支官方 OpenAPI 只有『當年度』資料，故僅供「最近年度是否配現金」判斷；
    若要「連續 N 年」需另接歷史來源。抓不到就回傳 {}，不影響主流程（配息只是附加標記）。
    """
    data = get_json(DIVIDEND_URL)
    if not isinstance(data, list) or not data:
        print("  ! 股利資料(t187ap45_L)抓取失敗，本次略過配息標記")
        return {}
    cash_keys = [k for k in data[0].keys() if "現金" in k and "元/股" in k]
    if not cash_keys:
        print("  ! 股利欄位對應失敗，本次略過配息標記")
        return {}
    out = {}
    for r in data:
        code = str(r.get("公司代號", "")).strip()
        if not code:
            continue
        amt = sum((to_float(r.get(k)) or 0) for k in cash_keys)
        cur = out.get(code)
        if cur is None or amt > cur["amt"]:
            try:
                year_ad = int(r.get("股利年度")) + 1911
            except Exception:
                year_ad = None
            out[code] = {"amt": round(amt, 2), "year": year_ad}
    n_paid = sum(1 for v in out.values() if v["amt"] > 0)
    print(f"  股利表：{len(out)} 家上市公司，其中 {n_paid} 家最近年度有配現金")
    return out


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

    # 把『買賣超股數』換算成『買賣超金額』(股數 × 當日成交均價)，對齊 CMoney 的金額排名。
    # 上市走 STOCK_DAY_ALL、上櫃走櫃買行情(Average)，合併成同一張均價表 → 全市場一起排名。
    vwap = fetch_vwap_map()
    if INCLUDE_TPEX:
        vwap.update(fetch_tpex_vwap_map(ref_date))
    if vwap:
        for s in all_stocks:
            p = vwap.get(s["code"])
            s["foreignAmt"] = int(s["foreign"] * p) if p is not None else None
            s["trustAmt"] = int(s["trust"] * p) if p is not None else None
        rankable = [s for s in all_stocks if s.get("foreignAmt") is not None]
        fkey, tkey = (lambda x: x["foreignAmt"]), (lambda x: x["trustAmt"])
        print(f"排名依據：買賣超『金額』(股數×成交均價)，可算金額 {len(rankable)}/{len(all_stocks)} 檔")
    else:
        # 拿不到成交均價時退回股數排名，至少不讓整個流程掛掉
        rankable = all_stocks
        fkey, tkey = (lambda x: x["foreign"]), (lambda x: x["trust"])
        print("! 拿不到成交均價，本次改用『股數』排名（金額欄位留空）")

    by_foreign = sorted(rankable, key=fkey, reverse=True)[:TOP_N]
    by_trust = sorted(rankable, key=tkey, reverse=True)[:TOP_N]
    foreign_rank = {s["code"]: i + 1 for i, s in enumerate(by_foreign)}
    trust_rank = {s["code"]: i + 1 for i, s in enumerate(by_trust)}

    inter = [s for s in all_stocks
             if s["code"] in foreign_rank and s["code"] in trust_rank]
    print(f"外資前{TOP_N} ∩ 投信前{TOP_N} 交集：{len(inter)} 檔")

    inter.sort(key=lambda s: foreign_rank[s["code"]] + trust_rank[s["code"]])
    capped = inter[:MAX_EMA_CHECK]
    if len(inter) > MAX_EMA_CHECK:
        print(f"交集過多，只檢查綜合排名前 {MAX_EMA_CHECK} 檔的均線")

    # 除權除息 factor（上市，供還原股價算均線；上櫃代碼不在表內→用原始股價）
    ex_factors = fetch_twse_ex_factors(ref_date)
    print("開始檢查 均線多頭排列（還原股價：股價>5MA>20MA>60MA）...")
    passed = []
    insufficient = 0
    for s in capped:
        market = s.get("market", "TWSE")
        bullish, close = check_bullish(s["code"], ref_date, market=market,
                                       ex_factors=ex_factors)
        tag = ("多頭排列成立" if bullish is True
               else "資料不足" if bullish is None else "未成立")
        print(f"  [{market}] {s['code']} {s['name']} → {tag}")
        if bullish is None:
            insufficient += 1
        if bullish is True:
            passed.append({
                "code": s["code"],
                "name": s["name"],
                "market": market,
                "foreignRank": foreign_rank[s["code"]],
                "trustRank": trust_rank[s["code"]],
                "foreignAmt": s.get("foreignAmt"),  # 買賣超金額(元)，前端可顯示
                "trustAmt": s.get("trustAmt"),
                "close": close,
            })

    # 限流防護：若太多檔抓不到足夠均線資料（被證交所限流），不要用這份殘缺名單
    # 覆蓋掉上一份好資料 —— 讓 Action 變紅、網站維持原樣，比默默貼出錯名單好。
    checked = len(capped)
    if checked and (checked - insufficient) / checked < MIN_DATA_OK_RATIO:
        print(f"! {insufficient}/{checked} 檔均線資料不足 → 極可能被證交所限流機房 IP。")
        print("  本次『不覆蓋』資料（保留上一份好名單），Action 標記失敗。")
        print("  對策：稍後重跑；或改在台灣 IP 的電腦定時跑 scan.py 後 push。")
        sys.exit(1)

    # 配息標記：最近年度是否配現金股利（前端自選按鈕「只看有配息」用）
    div_map = fetch_cash_dividend_map() if passed else {}
    if passed and INCLUDE_TPEX:
        div_map.update(fetch_tpex_cash_dividend_map())  # 上櫃股利併入（代碼不重疊）
    for s in passed:
        info = div_map.get(s["code"])
        s["cashDiv"] = bool(info and info["amt"] > 0)
        s["cashDivAmt"] = info["amt"] if info else None
        s["divYear"] = info["year"] if info else None
    if passed:
        n_div = sum(1 for s in passed if s["cashDiv"])
        print(f"配息標記：{len(passed)} 檔通過名單中，{n_div} 檔最近年度有配現金")

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
                "market": r.get("market", "TWSE"),
                "days": [], "lastClose": r.get("close"),
                "cashDiv": r.get("cashDiv", False),
                "cashDivAmt": r.get("cashDivAmt"),
            })
            t["days"].append(date)
            if r.get("close") is not None:
                t["lastClose"] = r["close"]
            # dates 由舊到新，讓最近一天的配息資訊覆蓋
            if "cashDiv" in r:
                t["cashDiv"] = r.get("cashDiv", False)
                t["cashDivAmt"] = r.get("cashDivAmt")

    streak = sorted(tally.values(), key=lambda t: len(t["days"]), reverse=True)[:15]
    for t in streak:
        t["count"] = len(t["days"])

    # 輸出給前端
    payload = {
        "generatedAt": dt.datetime.now(TAIPEI).strftime("%Y-%m-%d %H:%M"),
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
