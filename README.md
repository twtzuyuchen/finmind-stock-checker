# 台股個股融資維持率查詢工具（FinMind API + GitHub Actions + GitHub Pages）

輸入你指定的股票代號，用 FinMind API 抓融資與股價資料，以「加權平均成本法」
回推估算個股融資維持率，並把逐日回推過程整理成網頁。這是一個**完全獨立**的
小工具，不依賴任何其他 repo，只要照著下面步驟就能自己架一份。

> **重要：這是估算值，不是官方數字。** 台灣證交所／櫃買中心只公布「大盤」融資
> 維持率，並未公布個股數值，因為維持率牽涉每筆融資的實際成本，這只有券商知道。
> 本專案用「加權平均成本法」回推估算個股維持率，方法與 XQ 等看盤軟體公開說明的
> 邏輯相同，但仍是**估算**，僅供篩選觀察參考，**不構成投資建議**。詳細方法與
> 限制寫在 `scripts/check_stock.py` 檔頭註解，網頁最下方也會顯示同樣的提醒。

---

## 這個專案會做什麼

1. 你輸入一個或多個股票代號（例如 `2330 3138 6785`）。
2. 對每一檔股票，抓最近 60 個交易日（可調整）的融資買賣與收盤價，用加權平均
   成本法回推估計的「融資成本」。
3. 計算：`估算維持率 = 現在股價 ÷ (估算融資成本 × 融資成數) × 100%`
   （融資成數：一般股票 60%、ETF 90%，可用 `--financing-ratio` 手動覆蓋，
   例如你已經在交易所網站查到某檔被調降成 40%）。
4. 印出**逐日回推明細表**（不是只給你一個最終數字），並產生網頁：
   - `docs/<股票代號>.html`：該股的完整回推明細頁面。
   - `docs/index.html`：查詢列表，把你查過的所有股票整理成卡片。
5. 透過 GitHub Actions 手動觸發、GitHub Pages 對外呈現，或直接在自己電腦跑。

因為每檔股票只需要 2 次 FinMind API 請求，查詢通常幾秒到幾十秒就完成，
**不需要長時間排程**，用 `workflow_dispatch` 手動觸發即可。

---

## Step by step：從零開始架設

### 1. 準備 FinMind API Token（建議設定，非必要）

1. 到 [FinMind 官網](https://finmindtrade.com/) 註冊帳號並完成信箱驗證。
2. 登入後在會員頁面複製你的 API Token。
3. 沒有 token 也能跑（匿名額度），單股查詢通常額度綽綽有餘。

### 2. 建立新的 GitHub Repository

1. 登入 [github.com](https://github.com)，右上角 `+` → **New repository**。
2. 隨意取名，例如 `finmind-stock-checker`，Visibility 選 **Public**
   （Public repo 的 GitHub Actions 完全免費、不限分鐘數）。
3. 建立空的 repository。

### 3. 上傳這個專案的檔案

把我準備好的整個資料夾（包含 `.github/workflows/check-stock.yml`、
`scripts/check_stock.py`、`requirements.txt`、`docs/index.html`、
`README.md`）上傳到你剛建立的 repo。兩種方式擇一：

**方式 A：直接在網頁上傳**
1. 進入你的新 repo 頁面 → `Add file` → `Upload files`。
2. 把整個資料夾內的檔案（保留資料夾結構）拖拉上傳 → 送出 commit。

**方式 B：用 git 指令**
```bash
git clone https://github.com/<你的帳號>/<repo名稱>.git
cd <repo名稱>
# 把我提供的檔案複製進來這個資料夾，保留原本的路徑結構
git add .
git commit -m "init: finmind stock checker"
git push
```

### 4. 設定 FinMind Token 為 GitHub Secret（若步驟 1 有申請）

1. 進入 repo → `Settings` → 左側 `Secrets and variables` → `Actions`。
2. `New repository secret` → Name 填 `FINMIND_TOKEN`，Value 貼上 token → `Add secret`。

### 5. 開啟 GitHub Actions 並手動查一次

1. 進入 repo → `Actions` 分頁。若出現提示詢問是否啟用 workflow，按下啟用。
2. 左側會看到「查詢個股融資維持率明細」，點進去。
3. 右上角 `Run workflow` → `stock_ids` 欄位輸入股票代號（空白或逗號分隔都可以，
   例如 `2330 3138 6785`），需要的話調整 `lookback_days` 或 `financing_ratio`
   → 執行。
4. 幾十秒到幾分鐘內就會跑完，可以點進去看即時 log。

### 6. 開啟 GitHub Pages

1. 進入 repo → `Settings` → 左側 `Pages`。
2. `Build and deployment` → `Source` 選 `Deploy from a branch`。
3. `Branch` 選 `main`，資料夾選 `/docs` → `Save`。
4. 存檔後等 1～2 分鐘，網址格式通常是
   `https://<你的帳號>.github.io/<repo名稱>/`，打開即可看到查詢列表。

之後每次手動觸發查詢，跑完都會自動更新 `docs/` 內容並推送，Pages 網址會
自動跟著更新，**之前查過的股票也會保留在列表裡**（同一檔重複查會覆蓋成
最新資料，不會重複累積）。

---

## 本機使用

```bash
pip install -r requirements.txt
export FINMIND_TOKEN=你的token   # 選用但建議設定

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
```

會印出像這樣的逐日明細表：

```
日期            收盤價    融資買進    融資餘額    估計成本  備註
2026-06-01     500.00        200        200    500.00   ← 種子值(假設起點)
2026-06-02     480.00         50        250    496.00
2026-06-03     300.00          0        250    496.00   注意股票

[結果]
  現在股價　　　: 300.00
  融資餘額(張)　: 250
  估計融資成本　: 496.00
  融資成數　　　: 60%　（程式預設（ETF 90% / 一般 60%））
  估算融資維持率: 100.8%　← 低於門檻 130%
```

完成後打開 `docs/index.html` 就能看到網頁。

---

## 檔案結構

```
.
├── .github/workflows/check-stock.yml  # GitHub Actions 手動觸發設定
├── scripts/check_stock.py             # 主程式：抓資料、估算、產生網頁（完全獨立）
├── requirements.txt
├── docs/                          # GitHub Pages 網站根目錄（自動產生/更新）
│   ├── index.html                 # 查詢列表
│   ├── manifest.json              # 查詢紀錄（供 index.html 讀取）
│   └── <股票代號>.html             # 個股逐日回推明細
└── README.md
```

## 這個工具跟「全市場篩選」是什麼關係？

如果你還有一個「全市場融資維持率篩選」的 repo（掃全部上市櫃股票、排程自動跑），
這個工具是完全獨立、可以單獨使用的——不需要那個 repo 也能跑。設計成分開兩個
repo 的原因：全市場批次篩選要跑好幾小時、排程執行；這個工具查幾檔股票只要
幾十秒、手動觸發，兩者的執行模式差很多，分開比較乾淨，也比較不會互相卡到
（例如同時 commit 造成衝突）。用法上可以互相搭配：先在全市場篩選網頁看到
覺得可疑的股票代號，再複製過來這個工具細看逐日回推明細。

## 免責聲明

本專案僅為技術示範與資料觀察工具，融資維持率為模型估算值，可能與券商實際
計算結果有落差，不保證資料即時性、完整性或正確性，使用者需自行承擔依此
資訊做出任何投資決策的風險，本專案與作者不負任何法律或財務責任。
