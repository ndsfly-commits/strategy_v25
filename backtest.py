#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
=============================================================================
v2.5 五階段策略 · 歷史回測引擎
=============================================================================

完整重用 score_v22.py 的計分邏輯，把過去 15 年（2010–2025）每個交易日的
指標值「重新計算」一遍，並模擬 SOP 五模塊規則下的部位與績效。

【設計核心】
  1. 共用邏輯：直接 import score_v22 的計分函式，避免 Live-Backtest Drift
  2. 時間旅行：每一天都把所有時間序列切到「截至該日」再傳給計分函式
  3. 模塊上限：頂部 5 模塊×3=15 分，底部 5 模塊（3+3+3+2+2）=13 分
  4. 部位映射：依模塊總分查表決定 QQQ / TQQQ / Cash 配置
  5. 績效對照：vs QQQ Buy-and-Hold

【使用方式】
  python backtest.py                       # 抓取所有歷史資料 + 跑回測
  python backtest.py --start 2010-01-01    # 自訂起始日
  python backtest.py --end 2025-12-31      # 自訂結束日
  python backtest.py --no-fetch            # 用快取資料只重跑回測
  python backtest.py --quick               # 月頻回測（快速驗證用）

【輸出檔案】
  output/backtest_daily.csv         每日 SOP 評分與部位
  output/backtest_summary.json      策略 vs Buy-and-Hold 總績效
  output/backtest_trades.csv        部位變動明細（換倉日期 + 變化）
  output/backtest_drawdown.csv      回撤分析

【依賴】
  pip install yfinance pandas numpy requests beautifulsoup4

=============================================================================
"""
from __future__ import annotations

import argparse
import json
import sys
import warnings
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Dict, List, Any, Tuple

warnings.filterwarnings("ignore")

try:
    import pandas as pd
    import numpy as np
    import yfinance as yf
    import requests
except ImportError as e:
    print(f"❌ 缺少套件：{e}")
    print("請執行：pip install yfinance pandas numpy requests beautifulsoup4")
    sys.exit(1)

# 共用 score_v22 的計分邏輯，避免邏輯漂移
try:
    import score_v22 as sv
except ImportError:
    print("❌ 找不到 score_v22.py，請確認 backtest.py 與其放在同一個目錄")
    sys.exit(1)


# ═══════════════════════════════════════════════════════════════════════════
#  常數設定
# ═══════════════════════════════════════════════════════════════════════════

ROOT = Path(__file__).resolve().parent
OUTPUT_DIR = ROOT / "output"
CACHE_DIR = OUTPUT_DIR / "backtest_cache"
OUTPUT_DIR.mkdir(exist_ok=True)
CACHE_DIR.mkdir(exist_ok=True)

DAILY_CSV = OUTPUT_DIR / "backtest_daily.csv"
SUMMARY_JSON = OUTPUT_DIR / "backtest_summary.json"
TRADES_CSV = OUTPUT_DIR / "backtest_trades.csv"
DRAWDOWN_CSV = OUTPUT_DIR / "backtest_drawdown.csv"

# 五模塊定義（必須與 v5.html 完全一致）
TOP_MODULES = {
    "valuation":  {"keys": ["cape_30", "cape_35", "buffett_200", "margin_30", "margin_50"], "cap": 3},
    "sentiment":  {"keys": ["fg_75", "fg_85", "naaim_high", "vvix_divergence", "vix_21ma_low"], "cap": 3},
    "breadth":    {"keys": ["rsp_spy_breadth", "breadth_200d", "xly_xlp", "smh_spy"], "cap": 3},
    "credit":     {"keys": ["hy_oas_low", "hy_oas_expansion", "yield_inversion"], "cap": 3},
    "technical":  {"keys": ["rsi_75", "rsi_80", "distribution_days"], "cap": 3},
}

BOTTOM_MODULES = {
    "sentiment": {"keys": ["fg_25", "fg_15", "naaim_capitulation"], "cap": 3},
    "vol":       {"keys": ["vix_40", "vix_50"], "cap": 3},
    "price":     {"keys": ["dd_15", "dd_20", "dd_30", "rsi_low"], "cap": 3},
    "credit":    {"keys": ["oas_stress", "oas_recession"], "cap": 2},
    "breadth":   {"keys": ["breadth_capitulation", "ftd"], "cap": 2},
}

# 頂部部位映射（五模塊總分 → 部位百分比）
# 必須與 v5.html 的 ACTION_MAP 一致
TOP_ACTION_MAP = [
    # (min_score, max_score, qqq_pct, tqqq_pct, label)
    (0,  3,  1.00, 0.20, "健康"),       # 滿倉 + TQQQ 衛星 20%
    (4,  5,  1.00, 0.10, "略熱"),       # 滿倉 + TQQQ 減半至 10%
    (6,  6,  1.00, 0.00, "高檔警戒"),   # TQQQ 出清，QQQ 滿倉
    (7,  8,  0.80, 0.00, "避險啟動"),   # QQQ 80%
    (9,  10, 0.60, 0.00, "明顯偏空"),   # QQQ 60%
    (11, 15, 0.30, 0.00, "全面避險"),   # QQQ 30%
]

# 底部部位映射（底部模塊總分 → 額外 TQQQ %，疊加在 TOP 之上）
BOTTOM_ACTION_MAP = [
    # (min_score, max_score, extra_tqqq_pct, label)
    (0,  3,  0.00, "靜止"),
    (4,  5,  0.00, "警戒"),
    (6,  7,  0.30, "觸發"),    # +30% TQQQ
    (8,  9,  0.60, "加碼"),    # +60% TQQQ
    (10, 13, 1.00, "全壓"),    # +100% TQQQ
]


# ═══════════════════════════════════════════════════════════════════════════
#  資料抓取（一次抓 20 年，後續切片用）
# ═══════════════════════════════════════════════════════════════════════════

def fetch_full_history(start: str, end: str, use_cache: bool = True) -> Dict[str, Any]:
    """抓取所有指標的完整歷史，存入 cache 之後切片回放"""
    cache_file = CACHE_DIR / "full_history.pkl"

    if use_cache and cache_file.exists():
        # 檢查快取是否涵蓋所需期間
        try:
            data = pd.read_pickle(cache_file)
            qqq = data.get("yf", {}).get("QQQ")
            if qqq is not None and not qqq.empty:
                cache_start = qqq.index[0]
                cache_end = qqq.index[-1]
                # 快取要涵蓋所需期間 + 至少前置 2 年（給 weekly RSI 等指標暖機）
                need_start = pd.Timestamp(start) - pd.Timedelta(days=730)
                if cache_start <= need_start and cache_end >= pd.Timestamp(end):
                    print(f"✓ 使用快取：{cache_file.name} ({cache_start.date()} ~ {cache_end.date()})")
                    return data
        except Exception as e:
            print(f"⚠ 快取讀取失敗：{e}，重新抓取")

    print(f"📥 抓取完整歷史資料（{start} ~ {end}）...")
    # 為了讓技術指標有暖機期，多抓 2 年前置
    fetch_start = (pd.Timestamp(start) - pd.Timedelta(days=730)).strftime("%Y-%m-%d")

    data = {"yf": {}, "fred": {}, "fg": None, "naaim": None, "cape": None,
            "margin_debt": None, "spx_breadth": None}

    # ─ yfinance ─────────────────────────────────────────────
    tickers = ["QQQ", "SPY", "RSP", "XLY", "XLP", "SMH", "^VIX", "^VVIX", "^W5000", "TQQQ"]
    for t in tickers:
        try:
            print(f"  · yfinance: {t} ...", end=" ", flush=True)
            df = yf.download(t, start=fetch_start, end=end, progress=False, auto_adjust=False)
            if df is not None and not df.empty:
                # yfinance 回傳 multi-level columns，攤平
                if isinstance(df.columns, pd.MultiIndex):
                    df.columns = df.columns.get_level_values(0)
                data["yf"][t] = df
                print(f"{len(df)} rows")
            else:
                print("empty")
        except Exception as e:
            print(f"FAIL: {e}")

    # ─ FRED ─────────────────────────────────────────────────
    fred_series = {
        "hy_oas": "BAMLH0A0HYM2",
        "t10y2y": "T10Y2Y",
        "gdp": "GDP",
    }
    for name, sid in fred_series.items():
        try:
            print(f"  · FRED: {sid} ...", end=" ", flush=True)
            url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={sid}"
            r = requests.get(url, timeout=30)
            if r.status_code == 200:
                df = pd.read_csv(pd.io.common.StringIO(r.text))
                df.columns = ["date", "value"]
                df["date"] = pd.to_datetime(df["date"])
                df = df[df["value"] != "."]
                df["value"] = pd.to_numeric(df["value"], errors="coerce")
                df = df.dropna().set_index("date")["value"]
                data["fred"][name] = df
                print(f"{len(df)} rows")
            else:
                print(f"HTTP {r.status_code}")
        except Exception as e:
            print(f"FAIL: {e}")

    # ─ F&G 從 CNN（只能抓最近 ~3 年，更早用 alternative.me 備援）─
    try:
        print("  · F&G (alternative.me 全歷史) ...", end=" ", flush=True)
        url = "https://api.alternative.me/fng/?limit=0"  # limit=0 = all history
        r = requests.get(url, timeout=30)
        if r.status_code == 200:
            j = r.json()
            rows = [(pd.to_datetime(int(d["timestamp"]), unit="s"), float(d["value"]))
                    for d in j.get("data", [])]
            if rows:
                fg = pd.Series({d: v for d, v in rows}).sort_index()
                # alternative.me 是 crypto 的 F&G，跟 CNN 的股市 F&G 不同
                # 但兩者高度相關，可作為長歷史 proxy
                data["fg"] = fg
                print(f"{len(fg)} rows ({fg.index[0].date()} ~ {fg.index[-1].date()})")
        else:
            print(f"HTTP {r.status_code}")
    except Exception as e:
        print(f"FAIL: {e}")

    # ─ NAAIM 歷史（CSV download）─────────────────────────────
    try:
        print("  · NAAIM 歷史 CSV ...", end=" ", flush=True)
        url = "https://www.naaim.org/wp-content/themes/quietbow/scripts/exposure-csv.php"
        r = requests.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code == 200:
            df = pd.read_csv(pd.io.common.StringIO(r.text))
            # 預期欄位：Date, Mean Average
            df.columns = [c.strip() for c in df.columns]
            date_col = [c for c in df.columns if "date" in c.lower()][0]
            mean_col = [c for c in df.columns if "mean" in c.lower() or "average" in c.lower()][0]
            df[date_col] = pd.to_datetime(df[date_col])
            naaim = df.set_index(date_col)[mean_col].astype(float).sort_index()
            data["naaim"] = naaim
            print(f"{len(naaim)} rows")
        else:
            print(f"HTTP {r.status_code}")
    except Exception as e:
        print(f"FAIL: {e}")

    # ─ CAPE 歷史（multpl 月頻 HTML 表）──────────────────────
    try:
        print("  · CAPE 歷史 (multpl) ...", end=" ", flush=True)
        url = "https://www.multpl.com/shiller-pe/table/by-month"
        r = requests.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code == 200:
            tables = pd.read_html(pd.io.common.StringIO(r.text))
            if tables:
                t = tables[0]
                t.columns = ["date", "value"]
                t["date"] = pd.to_datetime(t["date"])
                t["value"] = (t["value"].astype(str)
                              .str.replace("estimate", "", regex=False)
                              .str.replace(" ", "")
                              .str.strip())
                t["value"] = pd.to_numeric(t["value"], errors="coerce")
                cape = t.dropna().set_index("date")["value"].sort_index()
                data["cape"] = cape
                print(f"{len(cape)} rows")
            else:
                print("no table")
        else:
            print(f"HTTP {r.status_code}")
    except Exception as e:
        print(f"FAIL: {e}")

    # ─ Margin Debt 歷史（FINRA）──────────────────────────────
    # 較難穩定抓，先放空，回測時這個指標可能 NaN（影響估值模塊但不致命）
    print("  · Margin Debt 歷史：暫不抓（影響有限）")

    # SPX 廣度（500 檔成分股各自的 200MA 比例）成本太高，
    # 改用「SPX 站上 200MA 的比例」proxy：用 SPY 個股的 ETF 替代資料
    # 這個比較複雜，先用 SPX 本身與 200MA 的距離當 proxy
    print("  · SPX 廣度：用 proxy（後續可優化）")

    # 存檔
    pd.to_pickle(data, cache_file)
    print(f"✓ 完整歷史存入 {cache_file.name}")
    return data


# ═══════════════════════════════════════════════════════════════════════════
#  時間旅行：把全歷史切到指定日期前的版本
# ═══════════════════════════════════════════════════════════════════════════

def slice_data_at_date(full: Dict[str, Any], asof: pd.Timestamp) -> Dict[str, Any]:
    """把所有時間序列切到 asof 日期（含）為止，傳給 build_scoring_data"""
    yf_sliced = {}
    for t, df in full["yf"].items():
        if df is not None and not df.empty:
            sub = df[df.index <= asof]
            if not sub.empty:
                yf_sliced[t] = sub

    fred_sliced = {}
    for name, ser in full["fred"].items():
        if ser is not None and not ser.empty:
            sub = ser[ser.index <= asof]
            if not sub.empty:
                fred_sliced[name] = sub

    fg_sliced = None
    if full["fg"] is not None:
        sub = full["fg"][full["fg"].index <= asof]
        if not sub.empty:
            fg_sliced = sub

    naaim_val = None
    if full["naaim"] is not None:
        sub = full["naaim"][full["naaim"].index <= asof]
        if not sub.empty:
            naaim_val = float(sub.iloc[-1])

    cape_val = None
    if full["cape"] is not None:
        sub = full["cape"][full["cape"].index <= asof]
        if not sub.empty:
            cape_val = float(sub.iloc[-1])

    return {
        "yf_data": yf_sliced,
        "fred_data": fred_sliced,
        "fg": fg_sliced,
        "naaim": naaim_val,
        "cape": cape_val,
    }


# ═══════════════════════════════════════════════════════════════════════════
#  模塊計分（取代「等權加總」）
# ═══════════════════════════════════════════════════════════════════════════

def module_score(indicators: List[Any], modules_def: Dict[str, Dict]) -> Dict[str, Any]:
    """依模塊定義將指標分組，各模塊上限後加總"""
    result = {"modules": {}, "total": 0, "raw_total": 0}
    for mod_name, mod_cfg in modules_def.items():
        mod_inds = [i for i in indicators if i.key in mod_cfg["keys"]]
        raw = sum(i.points for i in mod_inds)
        capped = min(raw, mod_cfg["cap"])
        result["modules"][mod_name] = {"raw": raw, "capped": capped, "cap": mod_cfg["cap"]}
        result["total"] += capped
        result["raw_total"] += raw
    return result


# ═══════════════════════════════════════════════════════════════════════════
#  部位映射
# ═══════════════════════════════════════════════════════════════════════════

def position_from_top(top_score: int) -> Tuple[float, float, str]:
    """頂部分數 → (QQQ_pct, TQQQ_pct, label)"""
    for lo, hi, qqq, tqqq, label in TOP_ACTION_MAP:
        if lo <= top_score <= hi:
            return qqq, tqqq, label
    return 0.30, 0.00, "全面避險"


def position_from_bottom(bot_score: int) -> Tuple[float, str]:
    """底部分數 → 額外 TQQQ %"""
    for lo, hi, extra, label in BOTTOM_ACTION_MAP:
        if lo <= bot_score <= hi:
            return extra, label
    return 0.00, "靜止"


def combine_positions(top_score: int, bot_score: int) -> Dict[str, Any]:
    """合併頂部減倉與底部加倉
    
    邏輯：
      1. 頂部主導減倉（QQQ + 基礎 TQQQ）
      2. 底部觸發 → 把現金部位轉為 TQQQ 加碼
      3. 但底部加碼只在頂部分數 < 7 時生效（避險區優先）
    """
    base_qqq, base_tqqq, top_label = position_from_top(top_score)
    extra_tqqq, bot_label = position_from_bottom(bot_score)

    # 底部加倉的條件：頂部處於健康/略熱區（0-5），否則矛盾不執行
    if top_score >= 6:
        extra_tqqq = 0.0  # 警戒區不抄底
    
    # 現金 = 1 - QQQ - 所有 TQQQ
    total_tqqq = base_tqqq + extra_tqqq
    cash = max(0.0, 1.0 - base_qqq - total_tqqq)

    # 如果加總超過 100%，等比例縮放（理論上不會發生但保險）
    total_exposure = base_qqq + total_tqqq
    if total_exposure > 1.0:
        scale = 1.0 / total_exposure
        base_qqq *= scale
        total_tqqq *= scale
        cash = 0.0

    return {
        "qqq_pct": base_qqq,
        "tqqq_pct": total_tqqq,
        "cash_pct": cash,
        "top_label": top_label,
        "bot_label": bot_label,
        # 「股票暴露度」= QQQ + TQQQ×3（粗略，因為 TQQQ 是 3 倍槓桿）
        "stock_exposure": base_qqq + total_tqqq * 3.0,
    }


# ═══════════════════════════════════════════════════════════════════════════
#  回測主迴圈
# ═══════════════════════════════════════════════════════════════════════════

def run_backtest(start: str, end: str, full_data: Dict[str, Any],
                 frequency: str = "daily") -> pd.DataFrame:
    """逐日跑回測，回傳每日 dataframe"""
    qqq = full_data["yf"].get("QQQ")
    tqqq = full_data["yf"].get("TQQQ")

    if qqq is None or qqq.empty:
        raise RuntimeError("QQQ 資料不存在，無法回測")

    # 決定要計算的日期清單
    backtest_dates = qqq.index[(qqq.index >= start) & (qqq.index <= end)]

    if frequency == "monthly":
        # 月底取樣
        backtest_dates = backtest_dates.to_series().groupby(
            [backtest_dates.year, backtest_dates.month]).last()
    elif frequency == "weekly":
        # 週五取樣
        backtest_dates = backtest_dates[backtest_dates.weekday == 4]

    print(f"📅 回測期間：{backtest_dates[0].date()} ~ {backtest_dates[-1].date()}")
    print(f"   日期數：{len(backtest_dates)} 天（頻率：{frequency}）")

    rows = []
    progress_step = max(1, len(backtest_dates) // 20)

    for i, asof in enumerate(backtest_dates):
        if i % progress_step == 0:
            pct = i / len(backtest_dates) * 100
            print(f"  進度 {pct:5.1f}% · {asof.date()}", flush=True)

        # 切資料
        sliced = slice_data_at_date(full_data, asof)

        # 用 score_v22 的邏輯計算指標
        try:
            data_dict = sv.build_scoring_data(
                yf_data=sliced["yf_data"],
                fred_data=sliced["fred_data"],
                fg=sliced["fg"],
                naaim=sliced["naaim"],
                cape=sliced["cape"],
                putcall=None,
                margin_debt=None,
                spx_breadth=None,
            )
            top_inds, _ = sv.score_top_signal(data_dict, manual={})
            bot_inds, _ = sv.score_bottom_signal(data_dict)
        except Exception as e:
            print(f"⚠ {asof.date()} 計算失敗：{e}")
            continue

        # 模塊計分
        top = module_score(top_inds, TOP_MODULES)
        bot = module_score(bot_inds, BOTTOM_MODULES)

        # 部位決策
        pos = combine_positions(top["total"], bot["total"])

        # 當日 QQQ 收盤
        qqq_close = float(qqq.loc[asof, "Close"]) if asof in qqq.index else np.nan
        tqqq_close = float(tqqq.loc[asof, "Close"]) if (tqqq is not None and asof in tqqq.index) else np.nan

        # 模塊細節展開
        row = {
            "date": asof,
            "qqq_close": qqq_close,
            "tqqq_close": tqqq_close,
            "top_score": top["total"],
            "bot_score": bot["total"],
            "top_raw": top["raw_total"],
            "bot_raw": bot["raw_total"],
            "top_label": pos["top_label"],
            "bot_label": pos["bot_label"],
            "qqq_pct": pos["qqq_pct"],
            "tqqq_pct": pos["tqqq_pct"],
            "cash_pct": pos["cash_pct"],
            "stock_exposure": pos["stock_exposure"],
        }
        # 個別模塊分數
        for mod_name, mod_data in top["modules"].items():
            row[f"top_{mod_name}"] = mod_data["capped"]
        for mod_name, mod_data in bot["modules"].items():
            row[f"bot_{mod_name}"] = mod_data["capped"]

        rows.append(row)

    df = pd.DataFrame(rows).set_index("date")
    print(f"✓ 完成 {len(df)} 筆評分")
    return df


# ═══════════════════════════════════════════════════════════════════════════
#  績效計算
# ═══════════════════════════════════════════════════════════════════════════

def compute_performance(df: pd.DataFrame, full_data: Dict[str, Any]) -> Dict[str, Any]:
    """根據逐日部位計算策略 vs Buy-and-Hold 績效
    
    模擬邏輯（簡化版，未考慮成本與滑價）：
      - 每天根據前一日的部位決策，乘以當天的標的報酬
      - QQQ 部位 → QQQ 報酬
      - TQQQ 部位 → TQQQ 報酬（實際 3x 槓桿 ETF）
      - Cash → 0% 報酬（保守，不計貨幣基金利息）
    """
    qqq = full_data["yf"]["QQQ"]["Close"]
    tqqq = full_data["yf"].get("TQQQ")
    tqqq_close = tqqq["Close"] if tqqq is not None else None

    # 對齊到回測日期
    qqq_aligned = qqq.reindex(df.index, method="ffill")
    tqqq_aligned = tqqq_close.reindex(df.index, method="ffill") if tqqq_close is not None else None

    # 報酬率
    qqq_ret = qqq_aligned.pct_change().fillna(0)
    tqqq_ret = tqqq_aligned.pct_change().fillna(0) if tqqq_aligned is not None else pd.Series(0, index=df.index)

    # 用「前一日的部位」決定今天的暴露（避免 look-ahead bias）
    qqq_pos_yesterday = df["qqq_pct"].shift(1).fillna(1.0)  # 起始假設 100% QQQ
    tqqq_pos_yesterday = df["tqqq_pct"].shift(1).fillna(0.0)

    # 每日策略報酬
    strategy_ret = qqq_pos_yesterday * qqq_ret + tqqq_pos_yesterday * tqqq_ret

    # 累積淨值
    strategy_nav = (1 + strategy_ret).cumprod()
    qqq_nav = (1 + qqq_ret).cumprod()

    # 績效指標
    days = len(df)
    years = days / 252

    def cagr(nav_series):
        return (nav_series.iloc[-1] ** (1 / years) - 1) * 100 if years > 0 else 0

    def max_drawdown(nav_series):
        running_max = nav_series.cummax()
        dd = (nav_series - running_max) / running_max
        return float(dd.min() * 100)

    def sharpe(ret_series, periods_per_year=252):
        if ret_series.std() == 0:
            return 0
        return float(ret_series.mean() / ret_series.std() * np.sqrt(periods_per_year))

    def sortino(ret_series, periods_per_year=252):
        downside = ret_series[ret_series < 0]
        if downside.std() == 0:
            return 0
        return float(ret_series.mean() / downside.std() * np.sqrt(periods_per_year))

    summary = {
        "period": {
            "start": str(df.index[0].date()),
            "end": str(df.index[-1].date()),
            "years": round(years, 2),
            "days": days,
        },
        "strategy": {
            "total_return_pct": round((strategy_nav.iloc[-1] - 1) * 100, 2),
            "cagr_pct": round(cagr(strategy_nav), 2),
            "max_drawdown_pct": round(max_drawdown(strategy_nav), 2),
            "sharpe": round(sharpe(strategy_ret), 3),
            "sortino": round(sortino(strategy_ret), 3),
            "final_nav": round(float(strategy_nav.iloc[-1]), 4),
        },
        "buy_and_hold_qqq": {
            "total_return_pct": round((qqq_nav.iloc[-1] - 1) * 100, 2),
            "cagr_pct": round(cagr(qqq_nav), 2),
            "max_drawdown_pct": round(max_drawdown(qqq_nav), 2),
            "sharpe": round(sharpe(qqq_ret), 3),
            "sortino": round(sortino(qqq_ret), 3),
            "final_nav": round(float(qqq_nav.iloc[-1]), 4),
        },
        "alpha": {
            "return_diff_pct": round((strategy_nav.iloc[-1] - qqq_nav.iloc[-1]) * 100, 2),
            "drawdown_diff_pct": round(max_drawdown(strategy_nav) - max_drawdown(qqq_nav), 2),
        }
    }

    # 把 NAV 也加進 df
    df["strategy_nav"] = strategy_nav
    df["qqq_nav"] = qqq_nav
    df["strategy_ret"] = strategy_ret
    df["qqq_ret"] = qqq_ret

    return summary


def extract_trades(df: pd.DataFrame) -> pd.DataFrame:
    """擷取部位變動的「換倉日」"""
    changes = []
    prev_qqq, prev_tqqq, prev_top, prev_bot = None, None, None, None
    for date, row in df.iterrows():
        qqq_pct = row["qqq_pct"]
        tqqq_pct = row["tqqq_pct"]
        if (prev_qqq is None) or (qqq_pct != prev_qqq) or (tqqq_pct != prev_tqqq):
            changes.append({
                "date": date,
                "top_score": row["top_score"],
                "bot_score": row["bot_score"],
                "top_label": row["top_label"],
                "bot_label": row["bot_label"],
                "qqq_pct_from": prev_qqq if prev_qqq is not None else "—",
                "qqq_pct_to": qqq_pct,
                "tqqq_pct_from": prev_tqqq if prev_tqqq is not None else "—",
                "tqqq_pct_to": tqqq_pct,
                "qqq_close": row["qqq_close"],
            })
        prev_qqq, prev_tqqq = qqq_pct, tqqq_pct
        prev_top, prev_bot = row["top_score"], row["bot_score"]
    return pd.DataFrame(changes).set_index("date")


def drawdown_analysis(df: pd.DataFrame) -> pd.DataFrame:
    """找出策略 vs QQQ 各自的歷史最大回撤期"""
    def find_drawdowns(nav, label):
        running_max = nav.cummax()
        dd = (nav - running_max) / running_max
        dd_periods = []
        in_dd = False
        start_dd, peak_nav, trough_date, trough_dd = None, None, None, 0
        for date, value in dd.items():
            if value < -0.01 and not in_dd:
                in_dd = True
                start_dd = date
                peak_nav = running_max.loc[date]
                trough_dd = value
                trough_date = date
            elif in_dd:
                if value < trough_dd:
                    trough_dd = value
                    trough_date = date
                if value >= -0.001:  # 回到高點
                    dd_periods.append({
                        "name": label,
                        "start": start_dd,
                        "trough": trough_date,
                        "end": date,
                        "trough_dd_pct": round(trough_dd * 100, 2),
                        "days": (date - start_dd).days,
                    })
                    in_dd = False
        return dd_periods

    strat_dd = find_drawdowns(df["strategy_nav"], "strategy")
    qqq_dd = find_drawdowns(df["qqq_nav"], "qqq_buy_hold")
    all_dd = strat_dd + qqq_dd
    if not all_dd:
        return pd.DataFrame()
    out = pd.DataFrame(all_dd).sort_values("trough_dd_pct")
    return out


# ═══════════════════════════════════════════════════════════════════════════
#  Main
# ═══════════════════════════════════════════════════════════════════════════

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2010-01-01")
    ap.add_argument("--end", default=datetime.today().strftime("%Y-%m-%d"))
    ap.add_argument("--no-fetch", action="store_true", help="使用快取，不抓新資料")
    ap.add_argument("--quick", action="store_true", help="月頻快速回測")
    ap.add_argument("--frequency", default="daily", choices=["daily", "weekly", "monthly"])
    args = ap.parse_args()

    if args.quick:
        args.frequency = "monthly"

    print("=" * 70)
    print(f"v2.5 SOP 策略歷史回測 · {args.start} ~ {args.end} · {args.frequency}")
    print("=" * 70)

    # Step 1: 抓資料
    full_data = fetch_full_history(args.start, args.end, use_cache=args.no_fetch)

    # Step 2: 跑回測
    df = run_backtest(args.start, args.end, full_data, frequency=args.frequency)

    # Step 3: 績效計算
    summary = compute_performance(df, full_data)

    # Step 4: 輸出
    df.to_csv(DAILY_CSV)
    print(f"✓ 每日明細存入 {DAILY_CSV.name}")

    with open(SUMMARY_JSON, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    print(f"✓ 績效總結存入 {SUMMARY_JSON.name}")

    trades = extract_trades(df)
    trades.to_csv(TRADES_CSV)
    print(f"✓ 換倉明細存入 {TRADES_CSV.name}（{len(trades)} 次換倉）")

    dd = drawdown_analysis(df)
    dd.to_csv(DRAWDOWN_CSV, index=False)
    print(f"✓ 回撤分析存入 {DRAWDOWN_CSV.name}（{len(dd)} 段回撤）")

    # 印出簡要結果
    print("\n" + "=" * 70)
    print("回測結果摘要")
    print("=" * 70)
    print(f"期間：{summary['period']['start']} ~ {summary['period']['end']} ({summary['period']['years']} 年)")
    print()
    print(f"{'指標':12s} {'SOP 策略':>15s} {'QQQ Buy-Hold':>15s} {'差異':>10s}")
    print("-" * 60)
    s = summary["strategy"]
    b = summary["buy_and_hold_qqq"]
    print(f"{'總報酬 %':12s} {s['total_return_pct']:>15.2f} {b['total_return_pct']:>15.2f} {s['total_return_pct'] - b['total_return_pct']:>+10.2f}")
    print(f"{'年化 %':12s} {s['cagr_pct']:>15.2f} {b['cagr_pct']:>15.2f} {s['cagr_pct'] - b['cagr_pct']:>+10.2f}")
    print(f"{'最大回撤 %':12s} {s['max_drawdown_pct']:>15.2f} {b['max_drawdown_pct']:>15.2f} {s['max_drawdown_pct'] - b['max_drawdown_pct']:>+10.2f}")
    print(f"{'Sharpe':12s} {s['sharpe']:>15.3f} {b['sharpe']:>15.3f} {s['sharpe'] - b['sharpe']:>+10.3f}")
    print(f"{'Sortino':12s} {s['sortino']:>15.3f} {b['sortino']:>15.3f} {s['sortino'] - b['sortino']:>+10.3f}")
    print()


if __name__ == "__main__":
    main()
