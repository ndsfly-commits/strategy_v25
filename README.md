# **v2.5 五階段策略每日評分系統 — 完全自動 + 完整覆蓋**

每日抓取 21 個頂部指標 + 14 個底部指標，自動套用 v2.5 規則計算評分，輸出 JSON 數據、CSV 歷史、HTML 視覺化儀表板。

> **v2.5 是 v2.x 系列的最終形態**：100% 自動運作，與 v2.2 同等的完整偵測力（22 個 score 點全部覆蓋），無需任何手動維護。詳見 `五階段市場策略SOP_v2.5_完全自動完整覆蓋版.md`。

---

## 📦 檔案結構

```
your-project/
├── score_v22.py                    # 主腳本（檔名不變，內部已升級到 v2.5）
├── operation_log_template.md       # 操作日誌範本
├── manual_overrides.json           # 爬蟲失敗時的 fallback（首次執行自動建立）
└── output/
    ├── latest_score.json           # 當日完整評分（含所有指標）
    ├── history.csv                 # 每日評分歷史
    ├── dashboard.html              # 視覺化儀表板（瀏覽器開啟）
    └── cache/                      # API 抓取資料快取
        ├── spx_components.json     S&P 500 成分股清單（30 天快取）
        ├── spx_breadth.json        % > 200d MA 計算結果（12 小時）
        ├── margin_debt.json        FINRA 月度資料（72 小時）
        └── ...                     其他指標快取
```

---

## 🚀 快速開始

### 1. 安裝套件

```bash
pip install yfinance pandas requests beautifulsoup4
```

### 2. 第一次執行

```bash
python score_v22.py
```

**首次執行約 60-90 秒**（需下載 500+ 檔 S&P 500 成分股的 1 年歷史）。後續執行 < 5 秒（12 小時快取命中）。

第一次跑會：
- 抓取所有指標（yfinance、FRED、CNN、NAAIM、CBOE、multpl、FINRA、**Wikipedia**）
- 批次下載 503 檔 S&P 500 成分股計算 % > 200d MA
- 在當前目錄建立 `manual_overrides.json` 樣板
- 在 `output/` 產出三個檔案

### 3. 確認結果

用瀏覽器打開 `output/dashboard.html`：
- 頂部與底部評分（大數字 + 顏色）
- 建議動作
- 21 個頂部指標的格狀圖（**含 SPX % > 200d 的 337/503 細節**）
- 14 個底部指標的格狀圖
- 評分歷史趨勢圖

### 4. 如果有指標抓取失敗

只有 4 個指標有手動 fallback（**通常用不到**）：

```json
{
  "cape": 41.06,                  從 multpl.com/shiller-pe 查
  "buffett_indicator": 228.3,     從 currentmarketvaluation.com 查
  "naaim_latest": 94.15,          從 naaim.org 查
  "spx_pct_above_200d": 67.0      從 stockcharts.com $SPXA200R 查
}
```

填完後再跑 `python score_v22.py`。

---

## 🔄 每日執行流程建議

### 自動化（推薦）

#### macOS / Linux：用 `cron`

```bash
crontab -e

# 每天美東時間 18:00 執行（美股收盤後）
0 18 * * 1-5 cd /path/to/project && /usr/bin/python3 score_v22.py >> daily.log 2>&1
```

#### Windows：用工作排程器

新增「基本工作」→ 每日觸發 → 啟動程式 `python.exe`，引數 `score_v22.py`。

### 手動執行

```bash
python score_v22.py             # 完整跑（重新抓資料）
python score_v22.py --no-fetch  # 離線跑（用快取）
python score_v22.py --manual    # 互動模式（提示輸入 fallback）
python score_v22.py --dashboard # 只重新生成儀表板
```

---

## 📊 資料來源詳細說明

### 完全自動化（共 22 個 score 點，21 個頂部指標 + 14 個底部指標）

| 指標 | 來源 | 更新頻率 | 備註 |
|---|---|---|---|
| QQQ, SPY, RSP, XLY, XLP, SMH, ^VIX, ^VVIX | yfinance | 日 | 偶爾 Yahoo 不穩，會跳過 |
| **🆕 SPX % > 200d MA** | **Wikipedia + yfinance（500 檔）** | **日** | **首次 60-90 秒，之後 12h 快取** |
| HY OAS（BAMLH0A0HYM2）| FRED CSV | 日 | 不需 API key |
| 10y-2y（T10Y2Y）| FRED CSV | 日 | |
| Wilshire 5000 + GDP（用於 Buffett）| FRED CSV | 日/季 | |
| F&G | CNN 非官方 API | 日 | 端點偶爾變動，會用快取 |
| Margin Debt YoY（FINRA）| FINRA HTML 爬蟲 | 月 | 3 天快取 |
| NAAIM | naaim.org 爬蟲 | 週四 | 失敗時可手動 fallback |
| CAPE | multpl.com 爬蟲 | 月 | 失敗時可手動 fallback |
| Put/Call | CBOE CSV | 日 | URL 偶爾變動 |
| Distribution Days, FTD, 週 RSI | yfinance 計算 | 日 | |
| RSP/SPY 6M, XLY/XLP, SMH/SPY | yfinance 計算 | 日 | |

---

## 🛠️ 常見問題

### Q：v2.5 的 SPX % > 200d 怎麼計算？

A：流程：
1. 從 Wikipedia 抓 S&P 500 成分股清單（503 檔）
2. yfinance 批次下載 1 年歷史（threads=True 平行）
3. 對每檔計算 200 日 SMA
4. 比對最新收盤：`if latest > ma200 → above_count += 1`
5. 結果：`above_count / total_valid × 100`

與 StockCharts $SPXA200R 等價（同樣是 SMA 與 close > MA 的計算方式），實測誤差 < 1%。

### Q：首次執行為什麼要 60-90 秒？

A：要下載 503 個 ticker × 250 個交易日 ≈ 12.6 萬個資料點。yfinance 用 threads=True 平行抓取，約 30-60 秒可完成；加上其他指標約 60-90 秒。

之後每天執行只用 < 5 秒（12 小時快取命中）。

### Q：SPX 成分股下載失敗怎麼辦？

A：系統有三層防線：
1. **Wikipedia** → 失敗則
2. **GitHub datasets repo**（備援）→ 失敗則
3. **手動 fallback**（在 `manual_overrides.json` 填 `spx_pct_above_200d`）

如果三層都失敗，系統跳過這兩個指標（最壞 -2 分），但其他 20 個 score 點仍正常運作。

### Q：FINRA Margin Debt 怎麼運作？

A：抓 [FINRA Margin Statistics 頁面](https://www.finra.org/rules-guidance/key-topics/margin-accounts/margin-statistics)，從表格解析最新 13 個月的 Debit Balance，計算 YoY。資料每月第三週更新，腳本快取 72 小時。

### Q：F&G 抓取失敗怎麼辦？

A：CNN 端點偶爾會擋。檢查 `output/cache/fear_greed.json` 是否在快取期內（6 小時）。系統會跳過該指標，當天評分會少 0-2 分。

### Q：yfinance 突然抓不到資料？

A：Yahoo Finance API 有時會擋頻繁請求。等 30 分鐘再跑，或加 `--no-fetch` 用快取資料先跑。

### Q：怎麼知道哪些指標今天有抓到？

A：執行時會印出每個資料源的狀態（✅ 或 ⚠️）。也可以查 `output/cache/` 下的 JSON 檔，每個檔有 `timestamp` 欄位。

### Q：歷史資料怎麼開始累積？

A：每跑一次 `score_v22.py`，當天評分會 append 到 `output/history.csv`。連續跑 30 天以上就有趨勢圖可看。

### Q：v2.2 vs v2.3 vs v2.4 vs v2.5 該用哪個？

| 你的偏好 | 建議版本 |
|---|---|
| 想最大化偵測準確度（22 點），不介意手動更新 | v2.2（80% 自動）|
| 想 100% 自動，介意失去 4 個指標 | v2.3（19 點）|
| 想 100% 自動 + Margin Debt（19 點）| v2.4 |
| **想 100% 自動 + 完整 22 點覆蓋（推薦）** | **v2.5** |

### Q：腳本檔名為什麼還叫 `score_v22.py`？

A：避免你已經設好的 cron 排程要改路徑。內部版本已升級到 v2.5，所有輸出（儀表板、JSON）都會標 v2.5。

---

## 🎯 進階：訂閱通知

當分數跨過關鍵門檻時用 email/Slack/Telegram 通知自己：

```python
import score_v22
result = score_v22.run()

import pandas as pd
hist = pd.read_csv("output/history.csv")
prev_top = hist.iloc[-2]["top_score"] if len(hist) >= 2 else 0
curr_top = result.top_score

if (prev_top < 7 and curr_top >= 7) or (prev_top < 11 and curr_top >= 11):
    send_alert(f"🚨 v2.5 頂部評分跳到 {curr_top}！動作：{result.action}")

if (prev_top >= 7 and curr_top < 5):
    send_alert(f"✅ 頂部訊號解除！可以恢復多頭曝險")
```

---

## 📝 操作日誌使用指引

每次調倉時：

1. 從儀表板抄下當日評分明細
2. 複製 `operation_log_template.md` 到 `logs/2026-MM-DD_<event>.md`
3. 填入：觸發評分、當下指標數值、為什麼動作、有哪些猶豫、當下情緒、預期成功標準
4. 1 週、1 個月、3 個月後回來填「事後諸葛」欄位

---

## 🔬 v2.5 的已知限制

1. **首次執行較慢**：60-90 秒（500 檔批次下載）。建議在 cron 排程而非手動跑。

2. **F&G 等爬蟲失效時**：CNN 端點變更、NAAIM 改網頁時，系統會跳過該指標。分數會偏低（不會偏高造成虛假訊號）。

3. **2007 型「信貸驅動頂部」**：v2.5 在 2007 估算約 6 分（黃燈高檔警戒）。雖然不會啟動 QQQI 防護（< 7），但會強制 TQQQ 出場，仍有意義的保護。

4. **SPX 成分股清單可能略過時**：Wikipedia 偶爾因 IPO/併購而成分股有變動。系統 30 天才更新一次，最壞情況會用過時的 1-2 個 ticker（不影響 breadth 計算的整體準確度）。

---

## 🚨 投資風險免責聲明

本系統為策略架構與紀律工具，**不構成任何投資建議**。所有投資決策由使用者自行負責。

過往歷史回測結果不代表未來表現。指標可能因市場結構變化而失效。每年至少回測一次系統有效性。

不要在不理解指標含義時盲目執行系統訊號。

---

**v2.5 是 v2.x 系列的最終穩定版本。 祝交易順利。 系統是工具，紀律才是關鍵。**
