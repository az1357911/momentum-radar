# 五日動能雷達

外資買超前 100 ∩ 投信買超前 100 ∩ EMA5>10>20 多頭排列 的每日自動選股。
由 GitHub Action 每個交易日收盤後自動抓證交所資料、算好結果，放到一個網頁上，手機（Android）跟電腦（Windows）用瀏覽器開同一個網址就能看。

## 它怎麼運作（為什麼這樣設計）

- **資料在伺服器端抓**：`scan.py` 在 GitHub 的機器上執行，用 Python 直接打證交所官網 API。瀏覽器直接抓證交所會被 CORS 擋掉，但伺服器對伺服器沒有這個限制，所以資料改由 Action 抓好。
- **前端只讀結果**：`docs/index.html` 只讀取同一個資料夾裡的 `data.json`（同源，不會有 CORS 問題），不在瀏覽器直接連證交所。
- **歷史存在 repo 裡**：每個交易日的通過名單存成 `data/history/YYYY-MM-DD.json`，「五日累計」就是讀最近 5 個檔案算出來的。取代了原本只能在特定環境用的 `window.storage`。

---

## 第一次設定（大約 10 分鐘，只需做一次）

1. **登入 GitHub**（沒有帳號就先到 github.com 免費註冊）。

2. **建立一個新的儲存庫（repository）**
   右上角 `+` → `New repository` → 取個名字（例如 `five-day-radar`）→ 選 **Public**（公開；私人庫要付費才能用 Pages）→ `Create repository`。

3. **上傳這個資料夾的所有檔案**
   在新 repo 頁面點 `Add file` → `Upload files`，把本資料夾內容整包拖進去（**要保留資料夾結構**：`scan.py`、`requirements.txt`、`README.md`、`docs/`、`data/`、`.github/`）→ `Commit changes`。

4. **開啟網頁（GitHub Pages）**
   `Settings` → 左側 `Pages` → Source 選 **Deploy from a branch** → Branch 選 `main`、資料夾選 **`/docs`** → `Save`。
   稍等一下，這頁上方會出現你的網址，格式是：
   `https://<你的帳號>.github.io/<repo名>/`

5. **給自動流程寫入權限**（很重要，否則它無法把結果存回來）
   `Settings` → `Actions` → `General` → 拉到 **Workflow permissions** → 選 **Read and write permissions** → `Save`。

6. **手動先跑一次，把第一天資料建起來**
   `Actions` 分頁 → 左側點 `daily-scan` → 右邊 `Run workflow` → 綠色 `Run workflow`。
   等 1～2 分鐘跑完（變綠勾）。

7. **打開網址**
   回到第 4 步那個 Pages 網址，手機、電腦都可以開。加到主畫面 / 我的最愛即可。

之後 **每個工作日台北時間約 15:30** 會自動更新，你什麼都不用做。累積幾天後「五日累計動能榜」就會出現。

---

## 常見問題

- **想改自動更新的時間？**
  編輯 `.github/workflows/daily.yml` 裡的 `cron`。它用 UTC，台北時間要減 8 小時。例如台北 15:30 → UTC 07:30 → `'30 7 * * 1-5'`。

- **手動那次跑失敗、log 顯示抓不到證交所？**
  極少數情況證交所會限制 GitHub 機房的 IP。可改成在自己的 Windows 電腦定時跑（見下方「在自己電腦跑」），再手動把結果推回 GitHub。

- **網頁顯示「無法讀取資料」？**
  代表 Action 還沒成功跑過。先確認第 5、6 步都完成，且 `daily-scan` 有跑出綠勾。

- **想同時看上櫃（TPEx）個股？**
  目前預設只做上市（穩定、保證可用）。上櫃要在 `scan.py` 把 `INCLUDE_TPEX` 設成 `True`，並自行補上 `fetch_tpex_institutional()`（櫃買已改用新的 OpenAPI，端點在 `https://www.tpex.org.tw/openapi/`，需先確認實際路徑再接）。原作者本來也註明上櫃資料穩定度較低，先跑上市即可。

---

## 在自己電腦跑（Windows，選用）

1. 安裝 Python 3（python.org，安裝時勾 “Add to PATH”）。
2. 在這個資料夾開啟命令提示字元，執行：
   ```
   pip install -r requirements.txt
   python scan.py
   ```
3. 它會更新 `docs\data.json`。要在本機看網頁，最穩的方式是在 `docs` 資料夾起一個小伺服器：
   ```
   cd docs
   python -m http.server 8000
   ```
   然後瀏覽器開 `http://localhost:8000`。
   （直接用檔案總管點 `index.html` 有時瀏覽器會擋 `file://` 的 fetch，用上面的方式最保險。）

---

## 這套修好了原本程式的哪些問題

| 原本的問題 | 這裡怎麼解 |
|---|---|
| 瀏覽器直接抓證交所被 CORS 擋 | 改由 GitHub Action 在伺服器端抓，前端只讀同源 `data.json` |
| `window.storage` 只在特定環境存在，離開就壞 | 歷史改存成 repo 裡的 JSON 檔，跨日累計靠 git |
| 上櫃舊的 `.php` 端點已停用 | 預設關閉上櫃、清楚標示；上市改用現行官網端點 |
| 投信買賣超抓錯欄位（index 9） | 改用「欄位名稱」對應（抓「投信買賣超」那欄），順序變動也不會壞 |
| 手機看不到 | GitHub Pages 給一個網址，手機電腦都能開 |
