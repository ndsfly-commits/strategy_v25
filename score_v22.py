#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
=============================================================================
v2.5 五階段策略每日評分系統（完全自動 + 完整覆蓋）
=============================================================================

每日自動抓取市場指標，套用 v2.5 規則計算頂部與底部訊號評分，
輸出 JSON、CSV 歷史記錄與 HTML 儀表板。

【v2.4 → v2.5 變動】
  ➕ 加回 SPX % > 200d MA（500 檔成分股自動計算 + Wikipedia 抓清單）
     - 頂部寬度背離：< 60% 且距 52w 高點 < 3% (+1)
     - 底部投降：< 20% (+1)
  📊 新分制：頂部最高 22（v2.4 是 21）、底部最高 15（v2.4 是 14）
  🎯 觸發門檻：頂部 ≥ 7、底部 ≥ 6（不變）
  💪 完整恢復 v2.2 全部 22 個 score 點，且 100% 自動運作
  ⚡ 首次執行約 60-90 秒（500 檔批次下載），後續用 12 小時快取

【使用方式】
  python score_v22.py                # 抓取最新數據並評分（產出所有檔案）
  python score_v22.py --no-fetch     # 只用快取資料重新評分（離線模式）
  python score_v22.py --dashboard    # 只重新生成儀表板，不抓資料
  python score_v22.py --manual       # 提示手動輸入 fallback 指標

【依賴套件】
  pip install yfinance pandas requests beautifulsoup4

【資料來源（全部自動）】
  yfinance:   QQQ, SPY, RSP, XLY, XLP, SMH, ^VIX, ^VVIX
  yfinance:   S&P 500 全部成分股（用於計算 % > 200d MA）
  Wikipedia:  S&P 500 成分股清單（每月更新一次即可）
  FRED CSV:   HY OAS, T10Y2Y, Wilshire 5000, GDP（無需 API key）
  CNN:        F&G 指數（非官方端點）
  FINRA:      Margin Debt 月度資料（HTML 爬蟲）
  NAAIM:      主動經理人持倉指數（手動 fallback）
  multpl:     CAPE 比率（手動 fallback）
  CBOE:       Put/Call 比率

【輸出檔案】
  output/latest_score.json    最新評分（含所有指標明細）
  output/history.csv          每日評分歷史
  output/dashboard.html       視覺化儀表板
  output/cache/*.json         各指標原始數據快取

=============================================================================
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
import warnings
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Dict, List, Any, Tuple
from io import StringIO

warnings.filterwarnings("ignore")

# ─── 第三方套件 ────────────────────────────────────────────────────────────
try:
    import pandas as pd
    import numpy as np
    import requests
    import yfinance as yf
except ImportError as e:
    print(f"❌ 缺少套件：{e}")
    print("請執行：pip install yfinance pandas requests beautifulsoup4")
    sys.exit(1)

try:
    from bs4 import BeautifulSoup
    HAS_BS4 = True
except ImportError:
    HAS_BS4 = False


# ═══════════════════════════════════════════════════════════════════════════
#  路徑與常數設定
# ═══════════════════════════════════════════════════════════════════════════

ROOT = Path(__file__).resolve().parent
OUTPUT_DIR = ROOT / "output"
CACHE_DIR = OUTPUT_DIR / "cache"
OUTPUT_DIR.mkdir(exist_ok=True)
CACHE_DIR.mkdir(exist_ok=True)

LATEST_JSON = OUTPUT_DIR / "latest_score.json"
HISTORY_CSV = OUTPUT_DIR / "history.csv"
DASHBOARD_HTML = OUTPUT_DIR / "dashboard.html"
MANUAL_OVERRIDES = ROOT / "manual_overrides.json"

# v2.2 評分門檻
TOP_TRIGGER = 7
BOTTOM_TRIGGER = 6


# ═══════════════════════════════════════════════════════════════════════════
#  資料結構
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class Indicator:
    """單一指標的觀測結果"""
    key: str                  # 內部 key（用於評分邏輯）
    name: str                 # 顯示名稱
    value: Any                # 觀測值（可能是 float、bool、str）
    threshold: str            # 觸發條件描述
    triggered: bool           # 是否觸發
    points: int               # 給分
    source: str               # 資料來源
    confidence: str = "high"  # high / med / low / manual / failed
    note: str = ""            # 額外說明
    
    def to_dict(self):
        d = asdict(self)
        # JSON 序列化處理：bool 留 bool，但 numpy 類型轉成 Python
        if isinstance(self.value, (np.floating, np.integer)):
            d["value"] = float(self.value) if isinstance(self.value, np.floating) else int(self.value)
        elif isinstance(self.value, np.bool_):
            d["value"] = bool(self.value)
        return d


@dataclass
class ScoreResult:
    """完整的評分結果"""
    timestamp: str
    date: str
    top_score: int
    top_max: int
    top_indicators: List[Indicator]
    bottom_score: int
    bottom_max: int
    bottom_indicators: List[Indicator]
    middle_triggered: bool
    middle_strength: str  # none / light / moderate / severe
    
    @property
    def top_zone(self) -> str:
        s = self.top_score
        if s <= 4:  return "🟢 健康多頭"
        if s <= 6:  return "🟡 高檔警戒"
        if s <= 10: return "🟠 頂部成型"
        if s <= 13: return "🔴 加強防備"
        return "⚫ 極端頂部"
    
    @property
    def action(self) -> str:
        s = self.top_score
        bs = self.bottom_score
        # 底部訊號優先
        if bs >= 10: return "🚀 100% TQQQ（底部極端機會）"
        if bs >= 8:  return "🚀 60% TQQQ + 40% QQQ"
        if bs >= 6:  return "🚀 30% TQQQ + 70% QQQ"
        # 中度訊號
        if self.middle_triggered:
            pct = {"light": 30, "moderate": 50, "severe": 70}.get(self.middle_strength, 30)
            return f"🔄 中度訊號（{self.middle_strength}）：{pct}% QQQ ↔ QQQI 轉換"
        # 頂部訊號階梯
        if s <= 4:  return "🟢 100% QQQ + 允許 TQQQ Runner"
        if s <= 6:  return "🟡 100% QQQ，TQQQ Runner 強制出場"
        if s <= 10: return "🟠 50% QQQI + 50% QQQ"
        if s <= 13: return "🔴 70% QQQI + 30% QQQ"
        return "⚫ 100% QQQI"


# ═══════════════════════════════════════════════════════════════════════════
#  通用工具
# ═══════════════════════════════════════════════════════════════════════════

def log(msg: str, level: str = "INFO"):
    icon = {"INFO": "ℹ️", "OK": "✅", "WARN": "⚠️", "ERR": "❌"}.get(level, "•")
    print(f"{icon} {msg}", flush=True)


def cache_path(key: str) -> Path:
    return CACHE_DIR / f"{key}.json"


def write_cache(key: str, data: Any):
    try:
        with open(cache_path(key), "w", encoding="utf-8") as f:
            json.dump({"timestamp": datetime.now().isoformat(), "data": data}, f,
                      ensure_ascii=False, indent=2, default=str)
    except Exception as e:
        log(f"快取寫入失敗 {key}: {e}", "WARN")


def read_cache(key: str, max_age_hours: float = 24) -> Optional[Any]:
    p = cache_path(key)
    if not p.exists():
        return None
    try:
        with open(p, "r", encoding="utf-8") as f:
            obj = json.load(f)
        ts = datetime.fromisoformat(obj["timestamp"])
        if datetime.now() - ts > timedelta(hours=max_age_hours):
            return None
        return obj["data"]
    except Exception:
        return None


def load_manual_overrides() -> Dict[str, Any]:
    if not MANUAL_OVERRIDES.exists():
        # 第一次執行時建立樣板（v2.5：僅留爬蟲失敗時的 fallback）
        sample = {
            "_comment": "手動覆寫（爬蟲失敗時的 fallback）。空字串或 null 代表使用自動抓取結果。",
            "cape": None,                       # 數值，例如 41.06（multpl 失效時）
            "buffett_indicator": None,          # 數值，例如 228.3（FRED 計算失效時）
            "naaim_latest": None,               # 數值，例如 94.15（naaim.org 解析失效時）
            "spx_pct_above_200d": None,         # 數值，例如 67.0（500 檔批次下載失敗時）
        }
        with open(MANUAL_OVERRIDES, "w", encoding="utf-8") as f:
            json.dump(sample, f, ensure_ascii=False, indent=2)
        return sample
    with open(MANUAL_OVERRIDES, "r", encoding="utf-8") as f:
        return json.load(f)


# ═══════════════════════════════════════════════════════════════════════════
#  資料抓取：yfinance
# ═══════════════════════════════════════════════════════════════════════════

YF_SYMBOLS = ["QQQ", "SPY", "RSP", "XLY", "XLP", "SMH", "^VIX", "^VVIX", "^W5000"]


def fetch_yfinance(use_cache: bool = True) -> Optional[Dict[str, pd.DataFrame]]:
    """抓取所有需要的 ETF/指數歷史資料（最近 2 年）"""
    if use_cache:
        cached = read_cache("yfinance_prices", max_age_hours=12)
        if cached is not None:
            log("使用 yfinance 快取（12 小時內）", "OK")
            return {k: pd.read_json(StringIO(v), orient="split") for k, v in cached.items()}
    
    log("抓取 yfinance 資料中...", "INFO")
    try:
        end = datetime.now()
        start = end - timedelta(days=730)  # 兩年資料
        result = {}
        for sym in YF_SYMBOLS:
            try:
                df = yf.Ticker(sym).history(start=start, end=end, auto_adjust=False)
                if df.empty:
                    log(f"yfinance 無 {sym} 資料", "WARN")
                    continue
                df.index = pd.to_datetime(df.index).tz_localize(None)
                result[sym] = df
            except Exception as e:
                log(f"抓取 {sym} 失敗：{e}", "WARN")
        
        # 寫入快取
        cache_data = {k: v.to_json(orient="split", date_format="iso") for k, v in result.items()}
        write_cache("yfinance_prices", cache_data)
        log(f"yfinance 完成（{len(result)} 個標的）", "OK")
        return result
    except Exception as e:
        log(f"yfinance 整體失敗：{e}", "ERR")
        return None


# ═══════════════════════════════════════════════════════════════════════════
#  HTTP 重試 helper（雲端機房常被擋 / timeout，用真實瀏覽器 headers + 指數退避）
# ═══════════════════════════════════════════════════════════════════════════

BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
              "text/csv,application/json;q=0.8,*/*;q=0.7",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "DNT": "1",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}


def http_get_with_retry(url: str, max_retries: int = 4, timeout: int = 60,
                         extra_headers: Optional[Dict] = None) -> Optional[requests.Response]:
    """帶有指數退避重試的 HTTP GET。為了應付雲端機房被擋/timeout 的情境。"""
    headers = dict(BROWSER_HEADERS)
    if extra_headers:
        headers.update(extra_headers)
    
    last_err = None
    for attempt in range(max_retries):
        try:
            if attempt > 0:
                wait = (2 ** attempt) + random.uniform(0, 1.5)
                time.sleep(wait)
            r = requests.get(url, timeout=timeout, headers=headers)
            r.raise_for_status()
            return r
        except Exception as e:
            last_err = e
            continue
    log(f"HTTP 重試 {max_retries} 次仍失敗 [{url[:80]}]：{last_err}", "WARN")
    return None


# ═══════════════════════════════════════════════════════════════════════════
#  資料抓取：FRED CSV（無需 API key）
# ═══════════════════════════════════════════════════════════════════════════

FRED_SERIES = {
    "hy_oas":     "BAMLH0A0HYM2",   # ICE BofA US HY OAS（日）
    "t10y2y":     "T10Y2Y",         # 10y-2y 殖利率差（日）
    "gdp":        "GDP",            # 名目 GDP（季）
    # 註：Wilshire 5000 已於 2024/6/3 從 FRED 移除（Wilshire 終止授權）
    # 改在 fetch_yfinance 裡抓 ^W5000，於 build_scoring_data 計算 Buffett
}


def fetch_fred(series_id: str, use_cache: bool = True) -> Optional[pd.Series]:
    cache_key = f"fred_{series_id}"
    if use_cache:
        cached = read_cache(cache_key, max_age_hours=24)
        if cached is not None:
            return pd.Series(cached["values"], 
                             index=pd.to_datetime(cached["dates"]))
    
    api_key = os.environ.get("FRED_API_KEY", "").strip()
    
    # 路徑 A：官方 API（有 key）— 雲端機房可用
    if api_key:
        url = (f"https://api.stlouisfed.org/fred/series/observations"
               f"?series_id={series_id}&api_key={api_key}&file_type=json")
        r = http_get_with_retry(url, max_retries=3, timeout=30)
        if r is not None:
            try:
                obs = r.json().get("observations", [])
                rows = [(o["date"], o["value"]) for o in obs if o["value"] != "."]
                if rows:
                    dates = pd.to_datetime([d for d, _ in rows])
                    values = pd.to_numeric([v for _, v in rows], errors="coerce")
                    s = pd.Series(values, index=dates).dropna()
                    write_cache(cache_key, {
                        "dates": [d.isoformat() for d in s.index],
                        "values": s.tolist(),
                    })
                    log(f"FRED {series_id}（API）：最新 {s.iloc[-1]:.4f}", "OK")
                    return s
            except Exception as e:
                log(f"FRED {series_id} API 解析失敗：{e}", "WARN")
    
    # 路徑 B：CSV fallback（無 key 或 API 失敗）— 本機可用、雲端機房常被擋
    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"
    r = http_get_with_retry(url, max_retries=4, timeout=60,
                              extra_headers={"Referer": "https://fred.stlouisfed.org/"})
    if r is None:
        log(f"FRED {series_id} 抓取失敗（請設定 FRED_API_KEY 環境變數）", "WARN")
        return None
    try:
        df = pd.read_csv(StringIO(r.text))
        date_col = df.columns[0]
        val_col = df.columns[1]
        df[date_col] = pd.to_datetime(df[date_col])
        df[val_col] = pd.to_numeric(df[val_col], errors="coerce")
        df = df.dropna()
        s = df.set_index(date_col)[val_col]
        write_cache(cache_key, {
            "dates": [d.isoformat() for d in s.index],
            "values": s.tolist(),
        })
        log(f"FRED {series_id}（CSV）：最新 {s.iloc[-1]:.4f}", "OK")
        return s
    except Exception as e:
        log(f"FRED {series_id} 解析失敗：{e}", "WARN")
        return None


# ═══════════════════════════════════════════════════════════════════════════
#  資料抓取：CNN F&G、NAAIM、multpl CAPE
# ═══════════════════════════════════════════════════════════════════════════

def fetch_fear_greed(use_cache: bool = True) -> Optional[pd.Series]:
    """CNN Fear & Greed Index 歷史值（非官方 API 端點）"""
    cache_key = "fear_greed"
    if use_cache:
        cached = read_cache(cache_key, max_age_hours=6)
        if cached is not None:
            return pd.Series(cached["values"], index=pd.to_datetime(cached["dates"]))
    
    # 起始日設一年半前
    start = (datetime.now() - timedelta(days=550)).strftime("%Y-%m-%d")
    url = f"https://production.dataviz.cnn.io/index/fearandgreed/graphdata/{start}"
    try:
        r = requests.get(url, timeout=60, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            "Accept": "application/json",
        })
        r.raise_for_status()
        data = r.json()
        history = data.get("fear_and_greed_historical", {}).get("data", [])
        if not history:
            log("F&G 歷史資料為空", "WARN")
            return None
        rows = [(pd.to_datetime(d["x"], unit="ms"), d["y"]) for d in history]
        s = pd.Series([r[1] for r in rows], index=[r[0] for r in rows]).sort_index()
        s.index = s.index.tz_localize(None) if s.index.tz is not None else s.index
        
        write_cache(cache_key, {
            "dates": [d.isoformat() for d in s.index],
            "values": s.tolist()
        })
        return s
    except Exception as e:
        log(f"F&G 抓取失敗：{e}", "WARN")
        return None


def fetch_naaim(use_cache: bool = True) -> Optional[float]:
    """NAAIM Exposure Index 最新值（每週四公布）"""
    cache_key = "naaim"
    if use_cache:
        cached = read_cache(cache_key, max_age_hours=72)
        if cached is not None:
            return cached.get("latest")
    
    try:
        url = "https://www.naaim.org/programs/naaim-exposure-index/"
        r = requests.get(url, timeout=60, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        if HAS_BS4:
            soup = BeautifulSoup(r.text, "html.parser")
            # 找含數字的表格
            tables = soup.find_all("table")
            for t in tables:
                rows = t.find_all("tr")
                for row in rows[1:5]:
                    cells = [c.get_text(strip=True) for c in row.find_all("td")]
                    if len(cells) >= 2:
                        try:
                            val = float(cells[1])
                            if -200 <= val <= 200:
                                write_cache(cache_key, {"latest": val})
                                return val
                        except ValueError:
                            continue
        log("NAAIM 解析失敗（請手動輸入）", "WARN")
        return None
    except Exception as e:
        log(f"NAAIM 抓取失敗：{e}", "WARN")
        return None


def fetch_cape(use_cache: bool = True) -> Optional[float]:
    """從 multpl.com 抓 CAPE 比率"""
    cache_key = "cape"
    if use_cache:
        cached = read_cache(cache_key, max_age_hours=24)
        if cached is not None:
            return cached.get("value")
    
    try:
        r = requests.get("https://www.multpl.com/shiller-pe", timeout=60,
                         headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        if HAS_BS4:
            soup = BeautifulSoup(r.text, "html.parser")
            current = soup.find(id="current")
            if current:
                txt = current.get_text(strip=True)
                # 取得文字中的數字
                import re
                m = re.search(r"(\d+\.\d+)", txt)
                if m:
                    val = float(m.group(1))
                    write_cache(cache_key, {"value": val})
                    return val
        log("CAPE 解析失敗（請手動輸入）", "WARN")
        return None
    except Exception as e:
        log(f"CAPE 抓取失敗：{e}", "WARN")
        return None


def fetch_putcall(use_cache: bool = True) -> Optional[pd.Series]:
    """CBOE Total Put/Call Ratio 歷史"""
    cache_key = "putcall"
    if use_cache:
        cached = read_cache(cache_key, max_age_hours=12)
        if cached is not None:
            return pd.Series(cached["values"], index=pd.to_datetime(cached["dates"]))
    
    # 路徑 A：CBOE 官方 CSV（cdn.cboe.com 對雲端機房 IP block）
    url = "https://cdn.cboe.com/api/global/us_indices/daily_prices/total_pc_history.csv"
    r = http_get_with_retry(url, max_retries=3, timeout=60,
                              extra_headers={"Referer": "https://www.cboe.com/"})
    if r is not None:
        try:
            df = pd.read_csv(StringIO(r.text))
            date_col = None
            ratio_col = None
            for c in df.columns:
                cl = c.lower()
                if "date" in cl: date_col = c
                elif "p/c" in cl or "put/call" in cl or "ratio" in cl: ratio_col = c
            if date_col and ratio_col:
                df[date_col] = pd.to_datetime(df[date_col])
                df[ratio_col] = pd.to_numeric(df[ratio_col], errors="coerce")
                df = df.dropna()
                s = df.set_index(date_col)[ratio_col].sort_index()
                if len(s) > 0:
                    write_cache(cache_key, {
                        "dates": [d.isoformat() for d in s.index],
                        "values": s.tolist(),
                    })
                    log(f"P/C（CBOE）：最新 {s.iloc[-1]:.3f}", "OK")
                    return s
        except Exception as e:
            log(f"P/C CBOE 解析失敗，改用 yfinance：{e}", "INFO")
    
    # 路徑 B：yfinance ^CPC（CBOE Equity Put/Call Ratio）— 雲端機房可用
    try:
        ticker = yf.Ticker("^CPC")
        end = datetime.now()
        start = end - timedelta(days=400)
        df = ticker.history(start=start, end=end, auto_adjust=False)
        if not df.empty and "Close" in df.columns:
            s = df["Close"].copy()
            s.index = pd.to_datetime(s.index).tz_localize(None)
            s = s.dropna().sort_index()
            if len(s) > 0:
                write_cache(cache_key, {
                    "dates": [d.isoformat() for d in s.index],
                    "values": s.tolist(),
                })
                log(f"P/C（yfinance ^CPC）：最新 {s.iloc[-1]:.3f}", "OK")
                return s
    except Exception as e:
        log(f"P/C yfinance fallback 失敗：{e}", "INFO")
    
    # 路徑 C：stooq.com CSV（^cpc，最後 fallback）
    try:
        d2 = datetime.now().strftime("%Y%m%d")
        d1 = (datetime.now() - timedelta(days=400)).strftime("%Y%m%d")
        url = f"https://stooq.com/q/d/l/?s=^cpc&d1={d1}&d2={d2}&i=d"
        r = http_get_with_retry(url, max_retries=3, timeout=30)
        if r is not None and r.text and "Date" in r.text:
            df = pd.read_csv(StringIO(r.text))
            if "Date" in df.columns and "Close" in df.columns:
                df["Date"] = pd.to_datetime(df["Date"])
                df["Close"] = pd.to_numeric(df["Close"], errors="coerce")
                df = df.dropna()
                s = df.set_index("Date")["Close"].sort_index()
                if len(s) > 0:
                    write_cache(cache_key, {
                        "dates": [d.isoformat() for d in s.index],
                        "values": s.tolist(),
                    })
                    log(f"P/C（stooq）：最新 {s.iloc[-1]:.3f}", "OK")
                    return s
    except Exception as e:
        log(f"P/C stooq fallback 失敗：{e}", "WARN")
    
    log("P/C 三個來源都失敗", "WARN")
    return None


def fetch_margin_debt(use_cache: bool = True) -> Optional[Dict[str, Any]]:
    """
    從 FINRA 抓取 Margin Debt 月度統計，計算 YoY %
    
    資料來源：https://www.finra.org/rules-guidance/key-topics/margin-accounts/margin-statistics
    更新頻率：每月第三週公布上月資料（例：4 月中旬公布 3 月資料）
    
    回傳結構：
      {
        "latest_month": "Mar-26",
        "latest_value_millions": 1220922.0,
        "year_ago_month": "Mar-25",
        "year_ago_value_millions": 880316.0,
        "yoy_pct": 38.69,
        "history": [...]  # 13 個月歷史
      }
    """
    cache_key = "margin_debt"
    if use_cache:
        cached = read_cache(cache_key, max_age_hours=72)  # 月資料，3 天快取夠用
        if cached is not None:
            return cached
    
    if not HAS_BS4:
        log("Margin Debt 解析需要 beautifulsoup4", "WARN")
        return None
    
    url = "https://www.finra.org/rules-guidance/key-topics/margin-accounts/margin-statistics"
    try:
        r = requests.get(url, timeout=60, headers={
            "User-Agent": "Mozilla/5.0 (compatible; ScoreSystem/1.0)",
            "Accept": "text/html,application/xhtml+xml",
        })
        r.raise_for_status()
        
        soup = BeautifulSoup(r.text, "html.parser")
        
        # 找到含 "Debit Balances" 標頭的表格
        target_table = None
        for table in soup.find_all("table"):
            headers = " ".join(th.get_text(strip=True) for th in table.find_all("th"))
            if "Debit Balances" in headers and "Month/Year" in headers:
                target_table = table
                break
        
        if target_table is None:
            log("FINRA 頁面結構變化，找不到 Margin Debt 表", "WARN")
            return None
        
        # 解析資料列
        rows = []
        for tr in target_table.find_all("tr"):
            cells = [td.get_text(strip=True) for td in tr.find_all("td")]
            if len(cells) < 2:
                continue
            month_str = cells[0].strip()
            if not month_str or "Month" in month_str:
                continue
            debit_str = cells[1].replace(",", "").replace("$", "").strip()
            try:
                debit = float(debit_str)
                rows.append((month_str, debit))
            except ValueError:
                continue
        
        if len(rows) < 13:
            log(f"FINRA Margin Debt 資料不足（{len(rows)} 行，需 ≥ 13）", "WARN")
            return None
        
        # FINRA 表格慣例：最新月在最上方
        latest_month, latest_value = rows[0]
        year_ago_month, year_ago_value = rows[12]
        yoy_pct = round((latest_value - year_ago_value) / year_ago_value * 100, 2)
        
        result = {
            "latest_month": latest_month,
            "latest_value_millions": latest_value,
            "year_ago_month": year_ago_month,
            "year_ago_value_millions": year_ago_value,
            "yoy_pct": yoy_pct,
            "history": [{"month": m, "value": v} for m, v in rows],
        }
        write_cache(cache_key, result)
        log(f"Margin Debt: {latest_month}=${latest_value/1000:,.0f}B, YoY={yoy_pct:+.2f}%", "OK")
        return result
        
    except Exception as e:
        log(f"Margin Debt 抓取失敗：{e}", "WARN")
        return None


# ═══════════════════════════════════════════════════════════════════════════
#  資料抓取：S&P 500 成分股清單 + Breadth（% > 200日線）
# ═══════════════════════════════════════════════════════════════════════════

SPX_WIKI_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
SPX_GITHUB_URL = "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/main/data/constituents.csv"


def fetch_spx_components(use_cache: bool = True) -> Optional[List[str]]:
    """
    抓 S&P 500 成分股清單。
    多源容錯：先試 Wikipedia，失敗則試 GitHub datasets repo。
    成分股清單變動緩慢，快取 30 天。
    """
    cache_key = "spx_components"
    if use_cache:
        cached = read_cache(cache_key, max_age_hours=24 * 30)  # 30 天快取
        if cached is not None:
            tickers = cached.get("tickers", [])
            if len(tickers) >= 400:
                return tickers
    
    # 來源 1：Wikipedia（首選）
    if HAS_BS4:
        try:
            r = requests.get(SPX_WIKI_URL, timeout=60, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "en-US,en;q=0.9",
            })
            r.raise_for_status()
            soup = BeautifulSoup(r.text, "html.parser")
            
            table = soup.find("table", {"id": "constituents"})
            if table is None:
                tables = soup.find_all("table", class_="wikitable")
                table = tables[0] if tables else None
            
            if table is not None:
                tickers = []
                for tr in table.find_all("tr")[1:]:  # 跳過表頭
                    cells = tr.find_all(["td", "th"])
                    if not cells:
                        continue
                    ticker = cells[0].get_text(strip=True)
                    # Wikipedia 用「.」(BRK.B)，yfinance 用「-」(BRK-B)
                    ticker = ticker.replace(".", "-").strip()
                    if ticker and ticker.isascii() and 1 <= len(ticker) <= 6:
                        tickers.append(ticker)
                
                if len(tickers) >= 480:
                    write_cache(cache_key, {"tickers": tickers, "source": "wikipedia",
                                            "count": len(tickers)})
                    log(f"S&P 500 成分股：{len(tickers)} 個（Wikipedia）", "OK")
                    return tickers
                else:
                    log(f"Wikipedia 解析數量過少：{len(tickers)}", "WARN")
        except Exception as e:
            log(f"Wikipedia 抓取失敗：{e}", "WARN")
    
    # 來源 2：GitHub datasets repo（備援）
    try:
        r = requests.get(SPX_GITHUB_URL, timeout=60, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        df = pd.read_csv(StringIO(r.text))
        symbol_col = next((c for c in df.columns if 
                          "symbol" in c.lower() or "ticker" in c.lower()), df.columns[0])
        tickers = [str(t).replace(".", "-").strip() for t in df[symbol_col].tolist()]
        tickers = [t for t in tickers if t and t.isascii() and 1 <= len(t) <= 6]
        
        if len(tickers) >= 480:
            write_cache(cache_key, {"tickers": tickers, "source": "github",
                                    "count": len(tickers)})
            log(f"S&P 500 成分股：{len(tickers)} 個（GitHub）", "OK")
            return tickers
    except Exception as e:
        log(f"GitHub fallback 失敗：{e}", "WARN")
    
    log("成分股抓取全部失敗，將跳過 SPX breadth", "WARN")
    return None


def fetch_spx_breadth(use_cache: bool = True) -> Optional[Dict[str, Any]]:
    """
    計算 S&P 500 中當日收盤 > 200 日 SMA 的個股比例（StockCharts $SPXA200R 等價）。
    
    流程：
      1. 取得當前成分股清單（500+ 個）
      2. yfinance 批次下載 1 年歷史（threads=True 平行加速）
      3. 對每檔計算 200 日 SMA，比對最新收盤
      4. 計算百分比 = 在 200d 上方的個股 / 有效資料的個股
    
    備註：
      - 首次執行約 30-60 秒（取決於網路）
      - 快取 12 小時，與其他日線資料一致
      - 必須 ≥ 400 檔下載成功才回傳結果
    """
    cache_key = "spx_breadth"
    if use_cache:
        cached = read_cache(cache_key, max_age_hours=12)
        if cached is not None:
            return cached
    
    components = fetch_spx_components(use_cache=use_cache)
    if components is None or len(components) < 400:
        log("S&P 500 成分股不足 400 個，跳過 breadth 計算", "WARN")
        return None
    
    log(f"批次下載 {len(components)} 個 S&P 500 成分股的 1 年歷史（約 30-60 秒）...", "INFO")
    try:
        end = datetime.now()
        start = end - timedelta(days=400)  # 400 日曆日 ≈ 280 交易日，足夠 200d SMA
        
        ticker_str = " ".join(components)
        data = yf.download(
            tickers=ticker_str,
            start=start, end=end,
            interval="1d",
            group_by="ticker",
            auto_adjust=True,
            progress=False,
            threads=True,
            ignore_tz=True,
        )
        
        if data is None or data.empty:
            log("S&P 500 批次下載結果為空", "WARN")
            return None
        
        # 計算每檔在不在 200d 上方
        above_count = 0
        total_valid = 0
        below_count = 0
        for ticker in components:
            try:
                # yfinance 多 ticker 用 MultiIndex columns: (ticker, field)
                if isinstance(data.columns, pd.MultiIndex):
                    if ticker not in data.columns.get_level_values(0):
                        continue
                    close = data[ticker]["Close"].dropna()
                else:
                    # 單一 ticker fallback（不應發生）
                    close = data["Close"].dropna()
                
                if len(close) < 200:
                    continue
                
                ma200 = close.tail(200).mean()
                latest = float(close.iloc[-1])
                if latest > ma200:
                    above_count += 1
                else:
                    below_count += 1
                total_valid += 1
            except (KeyError, AttributeError, ValueError, IndexError):
                continue
        
        if total_valid < 400:
            log(f"成分股下載成功率過低（{total_valid}/{len(components)}）", "WARN")
            return None
        
        breadth_pct = round(above_count / total_valid * 100, 2)
        result = {
            "pct_above_200d": breadth_pct,
            "above_count": above_count,
            "below_count": below_count,
            "total_valid": total_valid,
            "total_components": len(components),
            "calculated_at": datetime.now().isoformat(),
        }
        write_cache(cache_key, result)
        log(f"SPX % > 200d：{breadth_pct}%（{above_count}/{total_valid} 個成分股）", "OK")
        return result
        
    except Exception as e:
        log(f"SPX breadth 計算失敗：{e}", "WARN")
        return None


# ═══════════════════════════════════════════════════════════════════════════
#  指標計算函式
# ═══════════════════════════════════════════════════════════════════════════

def calc_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """Wilder's RSI（純 pandas 實作）"""
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1/period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/period, adjust=False).mean()
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    return rsi


def weekly_rsi(daily_close: pd.Series) -> float:
    """日線降採樣為週線後計算 RSI(14)"""
    weekly = daily_close.resample("W-FRI").last().dropna()
    rsi_series = calc_rsi(weekly, 14)
    return float(rsi_series.iloc[-1]) if len(rsi_series) > 0 else None


def distribution_days_4w(qqq_df: pd.DataFrame) -> int:
    """過去 20 個交易日中：跌幅 ≥ 0.2% 且當日量 > 前一日量"""
    df = qqq_df.tail(21).copy()
    if len(df) < 2:
        return 0
    df["pct_change"] = df["Close"].pct_change()
    df["vol_higher"] = df["Volume"] > df["Volume"].shift(1)
    df["is_dd"] = (df["pct_change"] <= -0.002) & df["vol_higher"]
    return int(df["is_dd"].tail(20).sum())


def follow_through_day_in_10d(qqq_df: pd.DataFrame) -> bool:
    """過去 10 個交易日內是否出現 Follow-Through Day（漲 ≥ 1.5% 且量增）"""
    df = qqq_df.tail(11).copy()
    if len(df) < 2:
        return False
    df["pct_change"] = df["Close"].pct_change()
    df["vol_higher"] = df["Volume"] > df["Volume"].shift(1)
    df["is_ftd"] = (df["pct_change"] >= 0.015) & df["vol_higher"]
    return bool(df["is_ftd"].tail(10).any())


def below_50d_persistent(ratio: pd.Series, days: int = 3) -> bool:
    """比值是否連續 N 日跌破自身 50 日 SMA"""
    if len(ratio) < 60:
        return False
    sma50 = ratio.rolling(50).mean()
    below = ratio < sma50
    return bool(below.tail(days).all())


def yield_curve_inverted_3m(t10y2y: pd.Series, lookback_days: int = 252) -> bool:
    """過去 252 個交易日中，T10Y2Y < 0 累計 ≥ 63 天（約 3 個月）"""
    recent = t10y2y.tail(lookback_days)
    return int((recent < 0).sum()) >= 63


def buffett_indicator(wilshire: pd.Series, gdp: pd.Series) -> Optional[float]:
    """
    Buffett 指標 = Wilshire 5000 / GDP × 100
    GDP 是季資料，需要找最近一季的值
    Wilshire 是日資料，取最新值
    """
    if wilshire is None or gdp is None or wilshire.empty or gdp.empty:
        return None
    latest_wilshire = float(wilshire.iloc[-1])
    latest_gdp = float(gdp.iloc[-1])  # 季 GDP，已年化
    # WILL5000PR 是「Full Cap Price Index」，1 點對應約 $1B 市值（FRED 文件）
    # Wilshire 5000 全市值（億）≈ WILL5000PR × 1.05
    market_cap = latest_wilshire * 1.05  # 億美元
    return market_cap / latest_gdp * 100


# ═══════════════════════════════════════════════════════════════════════════
#  評分主邏輯
# ═══════════════════════════════════════════════════════════════════════════

def score_top_signal(data: Dict, manual: Dict) -> Tuple[List[Indicator], int]:
    """v2.2 頂部訊號評分（21 分制）"""
    inds: List[Indicator] = []
    
    # ─ 估值 ──────────────────────────────────────────────────────────
    cape = manual.get("cape") if manual.get("cape") else data.get("cape")
    inds.append(Indicator(
        key="cape_30", name="CAPE ≥ 30", value=cape,
        threshold="≥ 30", triggered=bool(cape and cape >= 30),
        points=1 if (cape and cape >= 30) else 0,
        source="manual" if manual.get("cape") else "multpl.com",
        confidence="manual" if manual.get("cape") else ("high" if cape else "failed"),
    ))
    inds.append(Indicator(
        key="cape_35", name="CAPE ≥ 35", value=cape,
        threshold="≥ 35", triggered=bool(cape and cape >= 35),
        points=1 if (cape and cape >= 35) else 0,
        source="manual" if manual.get("cape") else "multpl.com",
        confidence="manual" if manual.get("cape") else ("high" if cape else "failed"),
    ))
    
    # ─ 情緒（F&G）─────────────────────────────────────────────────────
    fg_10ma = data.get("fg_10ma")
    inds.append(Indicator(
        key="fg_75", name="F&G 10MA ≥ 75", value=fg_10ma,
        threshold="≥ 75", triggered=bool(fg_10ma and fg_10ma >= 75),
        points=1 if (fg_10ma and fg_10ma >= 75) else 0,
        source="CNN", confidence="high" if fg_10ma else "failed",
    ))
    inds.append(Indicator(
        key="fg_85", name="F&G 10MA ≥ 85", value=fg_10ma,
        threshold="≥ 85", triggered=bool(fg_10ma and fg_10ma >= 85),
        points=1 if (fg_10ma and fg_10ma >= 85) else 0,
        source="CNN", confidence="high" if fg_10ma else "failed",
    ))
    
    # ─ 波動 ──────────────────────────────────────────────────────────
    vix_21ma = data.get("vix_21ma")
    inds.append(Indicator(
        key="vix_21ma_low", name="VIX 21MA < 13", value=vix_21ma,
        threshold="< 13", triggered=bool(vix_21ma and vix_21ma < 13),
        points=1 if (vix_21ma and vix_21ma < 13) else 0,
        source="Yahoo Finance", confidence="high" if vix_21ma else "failed",
    ))
    
    # ─ 寬度（RSP/SPY）─────────────────────────────────────────────────
    rsp_spy_6m = data.get("rsp_spy_6m_pct")
    inds.append(Indicator(
        key="rsp_spy_breadth", name="RSP/SPY 6M 跌幅", value=rsp_spy_6m,
        threshold="< -5%", triggered=bool(rsp_spy_6m is not None and rsp_spy_6m < -5),
        points=2 if (rsp_spy_6m is not None and rsp_spy_6m < -5) else 0,
        source="yfinance 計算", confidence="high" if rsp_spy_6m is not None else "failed",
    ))
    
    # ─ 動能（QQQ 週 RSI）─────────────────────────────────────────────
    qqq_rsi = data.get("qqq_weekly_rsi")
    inds.append(Indicator(
        key="rsi_75", name="QQQ 週 RSI > 75", value=qqq_rsi,
        threshold="> 75", triggered=bool(qqq_rsi and qqq_rsi > 75),
        points=1 if (qqq_rsi and qqq_rsi > 75) else 0,
        source="yfinance 計算", confidence="high" if qqq_rsi else "failed",
    ))
    inds.append(Indicator(
        key="rsi_80", name="QQQ 週 RSI > 80", value=qqq_rsi,
        threshold="> 80", triggered=bool(qqq_rsi and qqq_rsi > 80),
        points=1 if (qqq_rsi and qqq_rsi > 80) else 0,
        source="yfinance 計算", confidence="high" if qqq_rsi else "failed",
    ))
    
    # ─ 寬度（SPX % > 200d MA）─────────────────────────────────────────
    spx_200d = data.get("spx_pct_above_200d")
    if spx_200d is None:
        spx_200d = manual.get("spx_pct_above_200d")  # fallback
    spx_above_cnt = data.get("spx_breadth_above_count")
    spx_total = data.get("spx_breadth_total")
    spx_near_high = data.get("spx_within_3pct_of_high", False)
    breadth_trigger = (spx_200d is not None and spx_200d < 60 and spx_near_high)
    breadth_value_str = (
        f"{spx_200d:.1f}% ({spx_above_cnt}/{spx_total})"
        if spx_above_cnt is not None and spx_total is not None
        else (f"{spx_200d:.1f}%" if spx_200d is not None else None)
    )
    inds.append(Indicator(
        key="breadth_200d", name="SPX %>200d <60% 且距高點<3%",
        value=breadth_value_str,
        threshold="< 60% 且 距高點 < 3%", triggered=breadth_trigger,
        points=1 if breadth_trigger else 0,
        source="yfinance 計算（500 檔成分股）",
        confidence="high" if data.get("spx_pct_above_200d") is not None
                   else ("manual" if manual.get("spx_pct_above_200d") else "failed"),
        note=(f"距高點: {data.get('spx_distance_to_high_pct', 0):+.1f}%" 
              if spx_200d is not None else ""),
    ))
    
    # ─ 機構（派發日）─────────────────────────────────────────────────
    dd = data.get("distribution_days_4w")
    inds.append(Indicator(
        key="distribution_days", name="QQQ 派發日 (4w)", value=dd,
        threshold="≥ 4", triggered=bool(dd is not None and dd >= 4),
        points=1 if (dd is not None and dd >= 4) else 0,
        source="yfinance 計算", confidence="high" if dd is not None else "failed",
    ))
    
    # ─ 輪動（XLY/XLP）────────────────────────────────────────────────
    xly_xlp_below = data.get("xly_xlp_below_50d", None)
    inds.append(Indicator(
        key="xly_xlp", name="XLY/XLP 跌破 50dMA", value=xly_xlp_below,
        threshold="True", triggered=bool(xly_xlp_below),
        points=1 if xly_xlp_below else 0,
        source="yfinance 計算", confidence="high" if xly_xlp_below is not None else "failed",
    ))
    
    # ─ 領導（SMH/SPY）────────────────────────────────────────────────
    smh_spy_below = data.get("smh_spy_below_50d_3d", None)
    inds.append(Indicator(
        key="smh_spy", name="SMH/SPY 跌破 50dMA 持續 3 日", value=smh_spy_below,
        threshold="持續 3 日", triggered=bool(smh_spy_below),
        points=1 if smh_spy_below else 0,
        source="yfinance 計算", confidence="high" if smh_spy_below is not None else "failed",
    ))
    
    # ─ 籌碼（Put/Call 21MA）─────────────────────────────────────────
    pc_21ma = data.get("putcall_21ma")
    inds.append(Indicator(
        key="putcall_21ma", name="Put/Call 21MA < 0.7", value=pc_21ma,
        threshold="< 0.7", triggered=bool(pc_21ma and pc_21ma < 0.7),
        points=1 if (pc_21ma and pc_21ma < 0.7) else 0,
        source="CBOE", confidence="high" if pc_21ma else "failed",
    ))
    
    # ─ 槓桿（Margin Debt YoY，FINRA 自動爬蟲）──────────────────────────
    md_yoy = data.get("margin_debt_yoy_pct")
    md_month = data.get("margin_debt_latest_month", "")
    md_value_b = data.get("margin_debt_latest_value_b")
    md_display = (f"{md_yoy:+.1f}% ({md_month}: ${md_value_b:.0f}B)"
                  if md_yoy is not None and md_value_b is not None else md_yoy)
    inds.append(Indicator(
        key="margin_30", name="Margin Debt YoY > 30%", value=md_display,
        threshold="> 30%", triggered=bool(md_yoy is not None and md_yoy > 30),
        points=1 if (md_yoy is not None and md_yoy > 30) else 0,
        source="FINRA",
        confidence="high" if md_yoy is not None else "failed",
        note=f"FINRA 月度資料（{md_month}）" if md_month else "",
    ))
    inds.append(Indicator(
        key="margin_50", name="Margin Debt YoY > 50%", value=md_display,
        threshold="> 50%", triggered=bool(md_yoy is not None and md_yoy > 50),
        points=1 if (md_yoy is not None and md_yoy > 50) else 0,
        source="FINRA",
        confidence="high" if md_yoy is not None else "failed",
    ))
    
    # ─ 信用（HY OAS）─────────────────────────────────────────────────
    oas_now = data.get("hy_oas_current")
    inds.append(Indicator(
        key="hy_oas_low", name="HY OAS < 3.25%", value=oas_now,
        threshold="< 3.25%", triggered=bool(oas_now and oas_now < 3.25),
        points=1 if (oas_now and oas_now < 3.25) else 0,
        source="FRED BAMLH0A0HYM2", confidence="high" if oas_now else "failed",
    ))
    
    # ─ 波動結構（VVIX 背離）─────────────────────────────────────────
    vvix_21ma = data.get("vvix_21ma")
    vix_today = data.get("vix_today")
    vvix_div = bool(vvix_21ma and vix_today and vvix_21ma > 110 and vix_today < 15)
    inds.append(Indicator(
        key="vvix_divergence", name="VVIX 21MA > 110 且 VIX < 15",
        value=f"VVIX 21MA={vvix_21ma:.1f}, VIX={vix_today:.1f}" if (vvix_21ma and vix_today) else None,
        threshold="VVIX>110 且 VIX<15", triggered=vvix_div,
        points=1 if vvix_div else 0,
        source="yfinance",
        confidence="high" if (vvix_21ma and vix_today) else "failed",
    ))
    
    # ─ 專業持倉（NAAIM）─────────────────────────────────────────────
    naaim = manual.get("naaim_latest") if manual.get("naaim_latest") else data.get("naaim")
    inds.append(Indicator(
        key="naaim_high", name="NAAIM > 100", value=naaim,
        threshold="> 100", triggered=bool(naaim and naaim > 100),
        points=1 if (naaim and naaim > 100) else 0,
        source="manual" if manual.get("naaim_latest") else "naaim.org",
        confidence="manual" if manual.get("naaim_latest") else ("high" if naaim else "failed"),
    ))
    
    # ─ 信用擴大（v2.2 新）────────────────────────────────────────────
    oas_expansion = data.get("hy_oas_expansion_bps")
    inds.append(Indicator(
        key="hy_oas_expansion", name="🆕 HY OAS 擴大 ≥ 75 bps（從 12m 低）",
        value=f"{oas_expansion:.0f} bps" if oas_expansion is not None else None,
        threshold="≥ 75 bps", triggered=bool(oas_expansion and oas_expansion >= 75),
        points=1 if (oas_expansion and oas_expansion >= 75) else 0,
        source="FRED BAMLH0A0HYM2 計算",
        confidence="high" if oas_expansion is not None else "failed",
    ))
    
    # ─ 殖利率倒掛（v2.2 新）────────────────────────────────────────
    yc_inverted = data.get("yield_curve_3m_inverted")
    inds.append(Indicator(
        key="yield_inversion", name="🆕 10y-2y 倒掛 ≥ 3 個月（過去 12m 內）",
        value=yc_inverted,
        threshold="累計 ≥ 63 個交易日", triggered=bool(yc_inverted),
        points=1 if yc_inverted else 0,
        source="FRED T10Y2Y 計算",
        confidence="high" if yc_inverted is not None else "failed",
    ))
    
    # ─ Buffett（v2.2 新）─────────────────────────────────────────────
    buffett = manual.get("buffett_indicator") if manual.get("buffett_indicator") else data.get("buffett")
    inds.append(Indicator(
        key="buffett_200", name="🆕 Buffett 指標 > 200%", value=buffett,
        threshold="> 200%", triggered=bool(buffett and buffett > 200),
        points=1 if (buffett and buffett > 200) else 0,
        source="manual" if manual.get("buffett_indicator") else "FRED 計算",
        confidence="manual" if manual.get("buffett_indicator") else ("high" if buffett else "failed"),
    ))
    
    total = sum(i.points for i in inds)
    return inds, total


def score_bottom_signal(data: Dict) -> Tuple[List[Indicator], int]:
    """底部訊號評分（15 分制，保留 v2.0）"""
    inds: List[Indicator] = []
    
    vix = data.get("vix_today")
    inds.append(Indicator(
        key="vix_40", name="VIX ≥ 40", value=vix,
        threshold="≥ 40", triggered=bool(vix and vix >= 40),
        points=2 if (vix and vix >= 40) else 0,
        source="Yahoo Finance", confidence="high" if vix else "failed",
    ))
    inds.append(Indicator(
        key="vix_50", name="VIX ≥ 50", value=vix,
        threshold="≥ 50", triggered=bool(vix and vix >= 50),
        points=1 if (vix and vix >= 50) else 0,
        source="Yahoo Finance", confidence="high" if vix else "failed",
    ))
    
    fg = data.get("fg_today")
    inds.append(Indicator(
        key="fg_25", name="F&G ≤ 25", value=fg,
        threshold="≤ 25", triggered=bool(fg is not None and fg <= 25),
        points=1 if (fg is not None and fg <= 25) else 0,
        source="CNN", confidence="high" if fg is not None else "failed",
    ))
    inds.append(Indicator(
        key="fg_15", name="F&G ≤ 15", value=fg,
        threshold="≤ 15", triggered=bool(fg is not None and fg <= 15),
        points=1 if (fg is not None and fg <= 15) else 0,
        source="CNN", confidence="high" if fg is not None else "failed",
    ))
    
    qqq_dd = data.get("qqq_drawdown_pct")
    inds.append(Indicator(
        key="dd_15", name="QQQ 回撤 ≤ -15%", value=qqq_dd,
        threshold="≤ -15%", triggered=bool(qqq_dd is not None and qqq_dd <= -15),
        points=1 if (qqq_dd is not None and qqq_dd <= -15) else 0,
        source="yfinance 計算", confidence="high" if qqq_dd is not None else "failed",
    ))
    inds.append(Indicator(
        key="dd_20", name="QQQ 回撤 ≤ -20%", value=qqq_dd,
        threshold="≤ -20%", triggered=bool(qqq_dd is not None and qqq_dd <= -20),
        points=1 if (qqq_dd is not None and qqq_dd <= -20) else 0,
        source="yfinance 計算", confidence="high" if qqq_dd is not None else "failed",
    ))
    inds.append(Indicator(
        key="dd_30", name="QQQ 回撤 ≤ -30%", value=qqq_dd,
        threshold="≤ -30%", triggered=bool(qqq_dd is not None and qqq_dd <= -30),
        points=1 if (qqq_dd is not None and qqq_dd <= -30) else 0,
        source="yfinance 計算", confidence="high" if qqq_dd is not None else "failed",
    ))
    
    qqq_rsi = data.get("qqq_weekly_rsi")
    inds.append(Indicator(
        key="rsi_low", name="QQQ 週 RSI < 35", value=qqq_rsi,
        threshold="< 35", triggered=bool(qqq_rsi and qqq_rsi < 35),
        points=1 if (qqq_rsi and qqq_rsi < 35) else 0,
        source="yfinance 計算", confidence="high" if qqq_rsi else "failed",
    ))
    
    # ─ 寬度投降（SPX % > 200d MA < 20%）─────────────────────────────
    spx_200d_bot = data.get("spx_pct_above_200d")
    spx_above_cnt_bot = data.get("spx_breadth_above_count")
    spx_total_bot = data.get("spx_breadth_total")
    spx_breadth_value_bot = (
        f"{spx_200d_bot:.1f}% ({spx_above_cnt_bot}/{spx_total_bot})"
        if spx_above_cnt_bot is not None and spx_total_bot is not None
        else (f"{spx_200d_bot:.1f}%" if spx_200d_bot is not None else None)
    )
    inds.append(Indicator(
        key="breadth_capitulation", name="SPX % > 200d < 20%",
        value=spx_breadth_value_bot,
        threshold="< 20%",
        triggered=bool(spx_200d_bot is not None and spx_200d_bot < 20),
        points=1 if (spx_200d_bot is not None and spx_200d_bot < 20) else 0,
        source="yfinance 計算（500 檔成分股）",
        confidence="high" if spx_200d_bot is not None else "failed",
    ))
    
    pc_5ma = data.get("putcall_5ma")
    inds.append(Indicator(
        key="putcall_panic", name="P/C 5MA > 1.2", value=pc_5ma,
        threshold="> 1.2", triggered=bool(pc_5ma and pc_5ma > 1.2),
        points=1 if (pc_5ma and pc_5ma > 1.2) else 0,
        source="CBOE", confidence="high" if pc_5ma else "failed",
    ))
    
    ftd = data.get("ftd_in_10d")
    inds.append(Indicator(
        key="ftd", name="過去 10 日內出現 FTD", value=ftd,
        threshold="True", triggered=bool(ftd),
        points=1 if ftd else 0,
        source="yfinance 計算", confidence="high",
    ))
    
    oas = data.get("hy_oas_current")
    inds.append(Indicator(
        key="oas_stress", name="HY OAS > 6%", value=oas,
        threshold="> 6%", triggered=bool(oas and oas > 6),
        points=1 if (oas and oas > 6) else 0,
        source="FRED", confidence="high" if oas else "failed",
    ))
    inds.append(Indicator(
        key="oas_recession", name="HY OAS > 8%", value=oas,
        threshold="> 8%", triggered=bool(oas and oas > 8),
        points=1 if (oas and oas > 8) else 0,
        source="FRED", confidence="high" if oas else "failed",
    ))
    
    naaim = data.get("naaim")
    inds.append(Indicator(
        key="naaim_capitulation", name="NAAIM < 30", value=naaim,
        threshold="< 30", triggered=bool(naaim is not None and naaim < 30),
        points=1 if (naaim is not None and naaim < 30) else 0,
        source="naaim.org", confidence="high" if naaim is not None else "failed",
    ))
    
    total = sum(i.points for i in inds)
    return inds, total


def check_middle_signal(data: Dict) -> Tuple[bool, str]:
    """中度訊號（QQQ ↔ QQQI 微調）"""
    qqq_dd = data.get("qqq_drawdown_pct")
    fg = data.get("fg_today")
    vix = data.get("vix_today")
    rsi_w = data.get("qqq_weekly_rsi")
    
    if qqq_dd is None or qqq_dd > -10:
        return False, "none"
    
    # B 條件三選一
    cond_b = (
        (fg is not None and fg <= 30) or
        (vix is not None and vix > 25) or
        (rsi_w is not None and rsi_w < 45)
    )
    if not cond_b:
        return False, "none"
    
    # 強度分級
    if qqq_dd <= -20 and fg is not None and fg <= 15:
        return True, "severe"
    if qqq_dd <= -15 and fg is not None and fg <= 20:
        return True, "moderate"
    return True, "light"


# ═══════════════════════════════════════════════════════════════════════════
#  資料整合：把所有原始資料轉成評分輸入
# ═══════════════════════════════════════════════════════════════════════════

def build_scoring_data(yf_data: Dict, fred_data: Dict, fg: pd.Series,
                        naaim: float, cape: float, putcall: pd.Series,
                        margin_debt: Optional[Dict] = None,
                        spx_breadth: Optional[Dict] = None) -> Dict:
    """從原始抓取資料計算所有衍生指標"""
    d: Dict[str, Any] = {}
    
    # Margin Debt（早期填入，避免 yfinance 失敗時就丟掉）
    if margin_debt and isinstance(margin_debt, dict):
        d["margin_debt_yoy_pct"] = margin_debt.get("yoy_pct")
        d["margin_debt_latest_month"] = margin_debt.get("latest_month")
        d["margin_debt_latest_value_b"] = margin_debt.get("latest_value_millions", 0) / 1000
    
    # SPX Breadth（% > 200d MA）
    if spx_breadth and isinstance(spx_breadth, dict):
        d["spx_pct_above_200d"] = spx_breadth.get("pct_above_200d")
        d["spx_breadth_above_count"] = spx_breadth.get("above_count")
        d["spx_breadth_total"] = spx_breadth.get("total_valid")
    
    if yf_data is None:
        log("yfinance 資料缺失", "WARN")
        return d
    
    qqq = yf_data.get("QQQ")
    spy = yf_data.get("SPY")
    rsp = yf_data.get("RSP")
    xly = yf_data.get("XLY")
    xlp = yf_data.get("XLP")
    smh = yf_data.get("SMH")
    vix = yf_data.get("^VIX")
    vvix = yf_data.get("^VVIX")
    
    # VIX
    if vix is not None and not vix.empty:
        d["vix_today"] = float(vix["Close"].iloc[-1])
        d["vix_21ma"] = float(vix["Close"].tail(21).mean())
    
    # VVIX
    if vvix is not None and not vvix.empty:
        d["vvix_today"] = float(vvix["Close"].iloc[-1])
        d["vvix_21ma"] = float(vvix["Close"].tail(21).mean())
    
    # QQQ：回撤、週 RSI、派發日、FTD
    if qqq is not None and not qqq.empty:
        close = qqq["Close"]
        high_52w = close.tail(252).max()
        d["qqq_close"] = float(close.iloc[-1])
        d["qqq_drawdown_pct"] = (float(close.iloc[-1]) - float(high_52w)) / float(high_52w) * 100
        d["qqq_weekly_rsi"] = weekly_rsi(close)
        d["distribution_days_4w"] = distribution_days_4w(qqq)
        d["ftd_in_10d"] = follow_through_day_in_10d(qqq)
    
    # SPY：是否在距高點 3% 以內
    if spy is not None and not spy.empty:
        c = spy["Close"]
        h52 = c.tail(252).max()
        dist = (float(c.iloc[-1]) - float(h52)) / float(h52) * 100
        d["spx_within_3pct_of_high"] = dist >= -3
        d["spx_distance_to_high_pct"] = dist
    
    # RSP/SPY 6 個月變化
    if rsp is not None and spy is not None and not rsp.empty and not spy.empty:
        ratio = (rsp["Close"] / spy["Close"]).dropna()
        if len(ratio) >= 126:
            chg = (ratio.iloc[-1] - ratio.iloc[-126]) / ratio.iloc[-126] * 100
            d["rsp_spy_6m_pct"] = float(chg)
    
    # XLY/XLP 50 日線
    if xly is not None and xlp is not None and not xly.empty and not xlp.empty:
        ratio = (xly["Close"] / xlp["Close"]).dropna()
        if len(ratio) >= 50:
            sma50 = ratio.rolling(50).mean()
            d["xly_xlp_below_50d"] = bool(ratio.iloc[-1] < sma50.iloc[-1])
    
    # SMH/SPY 50 日線（持續 3 日）
    if smh is not None and spy is not None and not smh.empty and not spy.empty:
        ratio = (smh["Close"] / spy["Close"]).dropna()
        d["smh_spy_below_50d_3d"] = below_50d_persistent(ratio, 3)
    
    # FRED：HY OAS
    oas_series = fred_data.get("hy_oas")
    if oas_series is not None and not oas_series.empty:
        d["hy_oas_current"] = float(oas_series.iloc[-1])
        # 過去 12 個月（252 個交易日）最低值
        oas_min_12m = float(oas_series.tail(252).min())
        d["hy_oas_12m_min"] = oas_min_12m
        d["hy_oas_expansion_bps"] = (d["hy_oas_current"] - oas_min_12m) * 100
    
    # FRED：殖利率倒掛
    t10y2y = fred_data.get("t10y2y")
    if t10y2y is not None and not t10y2y.empty:
        d["yield_curve_today"] = float(t10y2y.iloc[-1])
        d["yield_curve_3m_inverted"] = yield_curve_inverted_3m(t10y2y)
    
    # Buffett 指標：Wilshire 5000（從 yfinance ^W5000）÷ GDP × 100
    # FRED 已於 2024/6/3 移除 Wilshire 系列，改從 yfinance 抓
    try:
        gdp = fred_data.get("gdp") if fred_data else None
        w5000 = yf_data.get("^W5000") if yf_data else None
        if (w5000 is not None and hasattr(w5000, "empty") and not w5000.empty
                and gdp is not None and hasattr(gdp, "empty") and not gdp.empty):
            close_series = w5000["Close"].dropna()
            if len(close_series) > 0 and len(gdp) > 0:
                latest_w5000 = float(close_series.iloc[-1])
                latest_gdp = float(gdp.iloc[-1])
                # ^W5000 點數 ≈ 美股全市值（單位：十億美元），1:1 對應
                # GDP 也是十億美元，相除即得百分比
                d["buffett"] = latest_w5000 / latest_gdp * 100
                log(f"Buffett 計算：W5000={latest_w5000:,.0f}, "
                    f"GDP={latest_gdp:,.0f}, 比值={d['buffett']:.1f}%", "OK")
    except Exception as e:
        log(f"Buffett 計算失敗：{type(e).__name__}: {e}", "WARN")
    
    # F&G
    if fg is not None and not fg.empty:
        d["fg_today"] = float(fg.iloc[-1])
        d["fg_10ma"] = float(fg.tail(10).mean())
    
    # NAAIM
    if naaim is not None:
        d["naaim"] = float(naaim)
    
    # CAPE
    if cape is not None:
        d["cape"] = float(cape)
    
    # Put/Call
    if putcall is not None and not putcall.empty:
        d["putcall_today"] = float(putcall.iloc[-1])
        d["putcall_21ma"] = float(putcall.tail(21).mean())
        d["putcall_5ma"] = float(putcall.tail(5).mean())
    
    return d


# ═══════════════════════════════════════════════════════════════════════════
#  HTML 儀表板生成
# ═══════════════════════════════════════════════════════════════════════════

def render_dashboard(result: ScoreResult, history: pd.DataFrame = None) -> str:
    """產生靜態 HTML 儀表板"""
    
    def cell_class(ind: Indicator) -> str:
        if ind.confidence == "failed":
            return "ind ind-na"
        if ind.triggered:
            return "ind ind-trig"
        return "ind ind-clear"
    
    def fmt_val(v: Any) -> str:
        if v is None:
            return "N/A"
        if isinstance(v, bool):
            return "✓" if v else "✗"
        if isinstance(v, (int, float)):
            return f"{v:.2f}" if isinstance(v, float) else str(v)
        return str(v)
    
    def render_indicator_grid(inds: List[Indicator]) -> str:
        rows = []
        for ind in inds:
            cls = cell_class(ind)
            value = fmt_val(ind.value)
            pts = ind.points
            rows.append(f'''
              <div class="{cls}">
                <div class="ind-name">{ind.name}</div>
                <div class="ind-value">{value}</div>
                <div class="ind-meta">
                  <span class="ind-thresh">{ind.threshold}</span>
                  <span class="ind-points">{('+' + str(pts)) if pts else '·'}</span>
                </div>
              </div>''')
        return "\n".join(rows)
    
    top_grid = render_indicator_grid(result.top_indicators)
    bot_grid = render_indicator_grid(result.bottom_indicators)
    
    # 評分顏色
    score_color_map = [
        (4, "#22c55e"),    # 綠
        (6, "#eab308"),    # 黃
        (10, "#f97316"),   # 橘
        (13, "#ef4444"),   # 紅
        (99, "#1f2937"),   # 黑
    ]
    score_color = next(c for limit, c in score_color_map if result.top_score <= limit)
    
    # 歷史趨勢圖資料
    history_json = "[]"
    if history is not None and not history.empty:
        history_json = history.tail(180).to_json(orient="records", date_format="iso")
    
    html = f"""<!DOCTYPE html>
<html lang="zh-TW">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>v2.5 五階段策略儀表板 — {result.date}</title>
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
  <style>
    *{{box-sizing:border-box;margin:0;padding:0}}
    body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','PingFang TC',Roboto,sans-serif;
         background:#0f172a;color:#e2e8f0;padding:24px;line-height:1.5}}
    .container{{max-width:1400px;margin:0 auto}}
    h1{{font-size:24px;margin-bottom:4px;color:#f1f5f9}}
    .subtitle{{color:#94a3b8;font-size:14px;margin-bottom:24px}}
    
    .score-grid{{display:grid;grid-template-columns:1fr 1fr;gap:20px;margin-bottom:24px}}
    .score-card{{background:#1e293b;border-radius:12px;padding:24px;border:1px solid #334155}}
    .score-label{{color:#94a3b8;font-size:13px;text-transform:uppercase;letter-spacing:1px;margin-bottom:8px}}
    .score-value{{font-size:64px;font-weight:700;line-height:1}}
    .score-max{{font-size:24px;color:#64748b;font-weight:400}}
    .score-zone{{margin-top:8px;font-size:18px;font-weight:500}}
    .score-action{{margin-top:16px;padding:12px;background:#0f172a;border-radius:8px;
                   border-left:4px solid {score_color};font-size:14px}}
    
    h2{{font-size:18px;margin:32px 0 12px;color:#f1f5f9;display:flex;align-items:center;gap:8px}}
    .ind-grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:8px}}
    .ind{{background:#1e293b;border:1px solid #334155;border-radius:8px;padding:12px;font-size:13px}}
    .ind-trig{{background:#7f1d1d;border-color:#ef4444}}
    .ind-clear{{background:#1e293b;border-color:#334155}}
    .ind-na{{background:#1e293b;border:1px dashed #475569;opacity:0.6}}
    .ind-name{{font-weight:600;color:#f1f5f9;margin-bottom:6px;font-size:13px}}
    .ind-value{{font-size:18px;font-weight:700;color:#cbd5e1;margin-bottom:6px;
                font-family:'SF Mono','Consolas',monospace}}
    .ind-meta{{display:flex;justify-content:space-between;align-items:center;
              font-size:11px;color:#94a3b8}}
    .ind-points{{background:#0f172a;padding:2px 8px;border-radius:4px;font-weight:700}}
    .ind-trig .ind-points{{background:#dc2626;color:#fff}}
    .ind-trig .ind-name,.ind-trig .ind-value{{color:#fff}}
    .ind-trig .ind-meta{{color:#fecaca}}
    
    /* Bottom signal triggered uses green */
    .ind-grid.bottom .ind-trig{{background:#14532d;border-color:#22c55e}}
    .ind-grid.bottom .ind-trig .ind-points{{background:#16a34a}}
    
    .chart-card{{background:#1e293b;border-radius:12px;padding:24px;margin:24px 0;
                 border:1px solid #334155;height:300px}}
    .footer{{text-align:center;color:#64748b;font-size:12px;margin-top:48px;padding-top:24px;
            border-top:1px solid #334155}}
    .legend{{display:inline-flex;gap:16px;font-size:12px;color:#94a3b8;margin-bottom:8px}}
    .legend span{{display:inline-flex;align-items:center;gap:4px}}
    .swatch{{width:12px;height:12px;border-radius:2px;display:inline-block}}
  </style>
</head>
<body>
  <div class="container">
    <h1>v2.5 五階段策略儀表板（完全自動 + 完整覆蓋）</h1>
    <div class="subtitle">最後更新：{result.timestamp} ｜ 觸發門檻：頂部 ≥ {TOP_TRIGGER}、底部 ≥ {BOTTOM_TRIGGER}</div>
    
    <div class="score-grid">
      <div class="score-card">
        <div class="score-label">頂部訊號（避險）</div>
        <div class="score-value" style="color:{score_color}">
          {result.top_score}<span class="score-max"> / {result.top_max}</span>
        </div>
        <div class="score-zone">{result.top_zone}</div>
      </div>
      
      <div class="score-card">
        <div class="score-label">底部訊號（抄底）</div>
        <div class="score-value" style="color:{'#22c55e' if result.bottom_score >= 6 else '#94a3b8'}">
          {result.bottom_score}<span class="score-max"> / {result.bottom_max}</span>
        </div>
        <div class="score-zone">{'🚀 觸發中' if result.bottom_score >= 6 else '靜止'}</div>
      </div>
    </div>
    
    <div class="score-card">
      <div class="score-label">建議動作</div>
      <div class="score-action">{result.action}</div>
    </div>
    
    <h2>🔴 頂部訊號指標明細（最高 22 分，21 個指標）</h2>
    <div class="legend">
      <span><span class="swatch" style="background:#7f1d1d"></span>已觸發</span>
      <span><span class="swatch" style="background:#1e293b;border:1px solid #334155"></span>未觸發</span>
      <span><span class="swatch" style="background:#1e293b;border:1px dashed #475569"></span>資料缺失</span>
    </div>
    <div class="ind-grid top">{top_grid}</div>
    
    <h2>🟢 底部訊號指標明細（最高 15 分，14 個指標）</h2>
    <div class="ind-grid bottom">{bot_grid}</div>
    
    <h2>📈 評分歷史趨勢（最近 180 天）</h2>
    <div class="chart-card"><canvas id="historyChart"></canvas></div>
    
    <div class="footer">
      v2.5 五階段策略系統（完全自動 + 完整覆蓋）｜ 本評分為策略架構演練，非投資建議
    </div>
  </div>

  <script>
    const data = {history_json};
    if (data && data.length > 0) {{
      const labels = data.map(d => d.date);
      const topScores = data.map(d => d.top_score);
      const bottomScores = data.map(d => d.bottom_score);
      
      const ctx = document.getElementById('historyChart').getContext('2d');
      new Chart(ctx, {{
        type: 'line',
        data: {{
          labels: labels,
          datasets: [
            {{label: '頂部評分', data: topScores, borderColor: '#ef4444',
              backgroundColor: 'rgba(239,68,68,0.1)', tension: 0.2, fill: true}},
            {{label: '底部評分', data: bottomScores, borderColor: '#22c55e',
              backgroundColor: 'rgba(34,197,94,0.1)', tension: 0.2, fill: true}}
          ]
        }},
        options: {{
          responsive: true, maintainAspectRatio: false,
          plugins: {{ legend: {{ labels: {{ color: '#cbd5e1' }} }} }},
          scales: {{
            x: {{ ticks: {{ color: '#94a3b8' }}, grid: {{ color: '#334155' }} }},
            y: {{ ticks: {{ color: '#94a3b8' }}, grid: {{ color: '#334155' }},
                  beginAtZero: true, max: 22 }}
          }}
        }}
      }});
    }} else {{
      document.querySelector('.chart-card').innerHTML = 
        '<div style="text-align:center;color:#64748b;padding:80px">尚無歷史資料（執行多日後將累積）</div>';
    }}
  </script>
</body>
</html>"""
    return html


# ═══════════════════════════════════════════════════════════════════════════
#  歷史記錄
# ═══════════════════════════════════════════════════════════════════════════

def append_history(result: ScoreResult):
    row = {
        "date": result.date,
        "timestamp": result.timestamp,
        "top_score": result.top_score,
        "bottom_score": result.bottom_score,
        "middle_triggered": result.middle_triggered,
        "middle_strength": result.middle_strength,
    }
    # 加入每個指標的觸發狀態與分數
    for ind in result.top_indicators:
        row[f"top_{ind.key}"] = ind.points
    for ind in result.bottom_indicators:
        row[f"bot_{ind.key}"] = ind.points
    
    df = pd.DataFrame([row])
    if HISTORY_CSV.exists():
        old = pd.read_csv(HISTORY_CSV)
        old = old[old["date"] != result.date]  # 移除同日舊記錄
        df = pd.concat([old, df], ignore_index=True)
    df.to_csv(HISTORY_CSV, index=False)


# ═══════════════════════════════════════════════════════════════════════════
#  Main
# ═══════════════════════════════════════════════════════════════════════════

def run(use_cache: bool = True, manual_prompt: bool = False) -> ScoreResult:
    log("=" * 70)
    log("v2.5 五階段策略每日評分（完全自動 + 完整覆蓋）")
    log("=" * 70)
    
    # 1. 抓資料
    yf_data = fetch_yfinance(use_cache=use_cache)
    fred_data = {k: fetch_fred(s, use_cache=use_cache) for k, s in FRED_SERIES.items()}
    fg_series = fetch_fear_greed(use_cache=use_cache)
    naaim = fetch_naaim(use_cache=use_cache)
    cape = fetch_cape(use_cache=use_cache)
    putcall = fetch_putcall(use_cache=use_cache)
    margin_debt = fetch_margin_debt(use_cache=use_cache)
    spx_breadth = fetch_spx_breadth(use_cache=use_cache)
    
    # 2. 載入手動覆寫
    manual = load_manual_overrides()
    
    # 3. 互動模式
    if manual_prompt:
        for k in ("cape", "buffett_indicator", "naaim_latest", "spx_pct_above_200d"):
            cur = manual.get(k)
            new = input(f"請輸入 {k}（當前：{cur}，按 Enter 跳過）：").strip()
            if new:
                try:
                    manual[k] = float(new)
                except ValueError:
                    pass
        with open(MANUAL_OVERRIDES, "w", encoding="utf-8") as f:
            json.dump(manual, f, ensure_ascii=False, indent=2)
    
    # 4. 計算指標
    data = build_scoring_data(yf_data, fred_data, fg_series, naaim, cape, putcall,
                              margin_debt=margin_debt, spx_breadth=spx_breadth)
    
    # 5. 評分
    top_inds, top_score = score_top_signal(data, manual)
    bot_inds, bot_score = score_bottom_signal(data)
    mid_trig, mid_strength = check_middle_signal(data)
    
    # 6. 組裝結果
    now = datetime.now()
    result = ScoreResult(
        timestamp=now.strftime("%Y-%m-%d %H:%M:%S"),
        date=now.strftime("%Y-%m-%d"),
        top_score=top_score,
        top_max=sum(2 if i.key in ("rsp_spy_breadth",) else 1 for i in top_inds),
        top_indicators=top_inds,
        bottom_score=bot_score,
        bottom_max=sum(2 if i.key == "vix_40" else 1 for i in bot_inds),
        bottom_indicators=bot_inds,
        middle_triggered=mid_trig,
        middle_strength=mid_strength,
    )
    
    # 7. 輸出
    json_out = {
        "timestamp": result.timestamp,
        "date": result.date,
        "top_score": result.top_score,
        "top_max": result.top_max,
        "top_zone": result.top_zone,
        "bottom_score": result.bottom_score,
        "bottom_max": result.bottom_max,
        "middle_triggered": result.middle_triggered,
        "middle_strength": result.middle_strength,
        "action": result.action,
        "top_indicators": [i.to_dict() for i in result.top_indicators],
        "bottom_indicators": [i.to_dict() for i in result.bottom_indicators],
        "raw_data": {k: (float(v) if isinstance(v, (np.floating, np.integer)) else
                         bool(v) if isinstance(v, np.bool_) else v)
                     for k, v in data.items()},
    }
    with open(LATEST_JSON, "w", encoding="utf-8") as f:
        json.dump(json_out, f, ensure_ascii=False, indent=2, default=str)
    
    append_history(result)
    
    history_df = pd.read_csv(HISTORY_CSV) if HISTORY_CSV.exists() else None
    html = render_dashboard(result, history_df)
    with open(DASHBOARD_HTML, "w", encoding="utf-8") as f:
        f.write(html)
    
    # 8. 印出摘要
    print()
    log(f"📊 頂部評分：{result.top_score} / {result.top_max} 分（{result.top_zone}）", "OK")
    log(f"📊 底部評分：{result.bottom_score} / {result.bottom_max} 分", "OK")
    log(f"📋 建議動作：{result.action}", "OK")
    print()
    log(f"檔案輸出：")
    log(f"  • {LATEST_JSON}")
    log(f"  • {HISTORY_CSV}")
    log(f"  • {DASHBOARD_HTML}（可直接用瀏覽器開啟）")
    
    return result


def main():
    parser = argparse.ArgumentParser(description="v2.5 五階段策略每日評分（完全自動 + 完整覆蓋）")
    parser.add_argument("--no-fetch", action="store_true",
                        help="不重抓資料，使用快取")
    parser.add_argument("--manual", action="store_true",
                        help="互動模式輸入手動指標")
    parser.add_argument("--dashboard", action="store_true",
                        help="只重新生成 HTML 儀表板")
    args = parser.parse_args()
    
    try:
        if args.dashboard and LATEST_JSON.exists():
            with open(LATEST_JSON, "r", encoding="utf-8") as f:
                j = json.load(f)
            log("從 latest_score.json 重新生成儀表板...", "INFO")
            run(use_cache=True)
            return
        
        run(use_cache=not args.manual and not args.dashboard, manual_prompt=args.manual)
    except Exception as e:
        # 雲端 workflow 即使某環節掛掉也不要讓整體失敗
        # 至少把錯誤訊息印出來，方便事後 debug
        log(f"主流程例外：{type(e).__name__}: {e}", "ERR")
        import traceback
        traceback.print_exc()
        # 不 sys.exit(1)，讓 workflow 接著做 commit/push（前次的 dashboard 仍可保留）
        return


if __name__ == "__main__":
    main()
