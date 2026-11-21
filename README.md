# TIGER ERP — 文具製造與批發資源管理

以 React / TypeScript、FastAPI、SQLAlchemy、Alembic 與 SQLite 實作的本機 ERP。原始規劃的六大模組、30 項作業之外，另有部門／員工、示範電子簽核、單據內直接送簽、SQLite 維護及 Windows 獨立版匯出。

此公開儲存庫提供原始碼、依賴鎖定檔、遷移與測試程式，**不包含 Owner 業務資料、私人對話、測試證據、SQLite 檔案、EXE 或 ZIP 成品**。測試程式中的合成資料不代表實際客戶或員工。

## 架構

| 層次 | 實作 |
|---|---|
| 前端 | React、TypeScript、Vite、Tailwind；以 hash 路由切換作業 |
| API | Python、FastAPI、Pydantic；既有資料的版本檢查與引用保護 |
| 資料存取 | SQLAlchemy、SQLite 外鍵、WAL、BEGIN IMMEDIATE 寫入交易 |
| 金額／數量 | 後端以十進位文字精確計算；不擅自決定精度、稅務或捨入政策 |
| 遷移 | Alembic；目前原始碼包含 0001 至 0021 |
| 驗證 | pytest、SQLite/API 檢查及 Playwright；測試資料與使用中資料分離 |
| 獨立版 | PyInstaller Windows x64 onedir，內含已編譯前端及 Python 執行環境 |

## 六大模組與 30 項作業

以下依畫面模組分組，不重新定義原作業編號。

| 模組 | 作業 | 項數 |
|---|---|---:|
| 基本資料 | 原物料、成品、客戶、供應商、倉庫、工作中心、供應商供料設定 | 7 |
| 採購管理 | 採購訂單、採購入庫、供應商退貨、未到貨採購查詢 | 4 |
| 製造生產 | 製程作業定義、產品 BOM、製造途程、製造工單、MRP Lite、製造領料、製造退料、完工回報、成品入庫 | 9 |
| 銷售管理 | 銷售訂單、銷售出貨、客戶退貨、銷售未交查詢 | 4 |
| 庫存管理 | 庫存盤點、庫存調整、倉間調撥 | 3 |
| 查詢與報表 | 原料／成品即時庫存、庫存異動與製造批號雙向追溯、採購／製造／銷售統計 | 3 |

過帳、完工確認、實際庫存及電子簽核各有獨立語義；簽核不會自動改變庫存或替既有過帳補上正式授權。未記錄的批號耗用分配不會被推算成精確追溯。

## 示範電子簽核

- 頂部「目前部門／目前員工」只是模擬操作員選擇，沒有登入、密碼或身分驗證。
- 部門階層、員工、主管與多重角色可維護；「建立示範部門與員工」是明確操作，不會在啟動時自動建立示範組織。
- 採購訂單、製造工單與銷售訂單可在已儲存單據的操作區直接「送出簽核」，確認文件、申請人、版本與各關人員；取消不會送件。原有獨立送簽頁及 API 保留。
- 同一引擎執行三種四關流程：PO 為部門主管→採購→財務＋倉庫→總經理；WO 為生產→物料→倉庫＋品管→廠長；SO 為業務→銷售管理→財務＋營運→總經理。第三關兩位不同的必要會簽者必須全部同意。
- 支援退回、申請撤回、安全的本人簽核撤銷、加簽、期間限定的單一任務代理、待辦及歷程。流程版本、文件快照與事件保持不可改寫；來源變更須重新審查。

## 安裝與開發啟動（Windows）

目前開發環境使用 Python 3.14 x64、Node.js 26 與 npm。依賴版本請以 backend/requirements-lock.txt、frontend/package-lock.json 為準。套件安裝需可存取相應套件來源；本機執行不需要外部 LLM 服務。

```powershell
# 在本儲存庫根目錄執行
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\setup.ps1
.\.venv\Scripts\python.exe -m backend.local_demo --database data\erp.sqlite --backend-port 8000 --frontend-port 5173
```

此明確的 local_demo 啟動器提供受限的本機示範過帳、維護與簽核功能，只綁定 127.0.0.1。一般 API 開發也可使用 scripts/dev.py；它不會授予上述本機能力，相關操作會拒絕執行。請勿為了繞過拒絕而停用保護。

- 前端：http://127.0.0.1:5173
- API 文件：http://127.0.0.1:8000/docs
- 健康檢查：http://127.0.0.1:8000/api/health
- 預設開發資料庫：data/erp.sqlite；程式提供 ERP_DB_PATH 與明確的 --database 設定，避免混淆測試與使用中資料。
- 在啟動視窗按 Ctrl+C 關閉自己啟動的服務。啟動器不會為搶占埠號而終止其他程式。

新安裝會建立結構及技術性示範 Actor，不包含 Owner 商業紀錄或自動植入的採購／銷售文件。請透過畫面自行建立所需主檔；示範部門初始化也需要明確按下按鈕。

## 資料庫遷移與保護

服務啟動會套用 Alembic 遷移；也可明確指定目的地：

```powershell
.\.venv\Scripts\python.exe scripts\migrate.py --database data\erp.sqlite
```

先確認目的資料庫並備份；不要將測試命令指向使用中的資料庫。版本0021新增組織及簽核結構。SQLite 備份／清空／恢復工具要求明確本機能力與確認，並驗證受管備份、結構及實際資料狀態。清空與恢復不是一般測試準備步驟。備份、WAL/SHM、匯出封裝與資料目錄都必須留在 Git 外。

## 原始碼結構

```text
backend/app/                 API、模型、schema、業務與簽核引擎
backend/migrations/         Alembic 版本與遷移環境
backend/tests/              合成資料、API/SQLite 與業務測試
backend/local_demo.py        明確的本機示範啟動器
backend/standalone.py        獨立版啟動器
frontend/src/               React 作業畫面與共用元件
frontend/tests/             Playwright 測試原始碼
scripts/                   安裝、啟動、遷移、打包與驗證工具
docs/                      公開開發與資料保護說明
```

## 輕量驗證

```powershell
New-Item -ItemType Directory -Force artifacts | Out-Null
.\.venv\Scripts\python.exe -m pytest backend/tests/test_foundation.py -q --basetemp artifacts\publication-check
npm.cmd --prefix frontend run build
```

這不是全部歷史回歸或全部 Owner 驗收的替代品。部分歷史瀏覽器／獨立版測試需要先準備合成資料、指定埠號、Edge 路徑或本機證據基準；公開庫不提供私人基準檔，不能直接假定所有這類流程可在空白環境重播。不要把歷史驗證工具指向自己的正式資料。含 Owner 批號 UUID、實際庫存驗收值的歷史測試已排除；同領域的合成後端測試仍提供於 backend/tests。

## Windows 獨立版匯出

```powershell
npm.cmd --prefix frontend run build
.\.venv\Scripts\python.exe -m pip install -r scripts\packaging-requirements.txt
.\.venv\Scripts\python.exe scripts\build_standalone.py
```

建置在 standalone-runtime/TIGER-ERP 產生不可變執行環境，來源指紋檢查會拒絕過期的 runtime。ERP 的「匯出獨立系統」另行取得當下完整 SQLite 線上快照，再封裝 runtime、seed 與說明；這個 ZIP 含私有資料，**不得提交到公開庫**。獨立安裝首次啟動由 seed 建立自己的 data，後續使用既有資料，不覆寫為初始快照；前後端由同一個 loopback 程序提供。

本次原始碼公開不會重建或發布任何 EXE／ZIP，也不代表已修復任何既有 runtime 與來源不同步的狀態。請依實際來源重新建置與驗證後再自行匯出。

## 驗收範圍與安全限制

依 Owner 本次授權說明，原始30項作業及已測試的採購訂單電子簽核流程已獲功能驗收。這不等於其他簽核類型、所有進階簽核動作、獨立版或 SQLite 維護工具均有 Owner 驗收；其未明確接受部分仍為 PENDING。Codex 自動化檢查通過與 Owner 功能驗收分別記錄。

**SECURITY: PARTIAL — RISK CONTAINED. D01–D16: PENDING.** 任何可操作本機介面的人都可能選取其他模擬員工；不提供驗證簽署人、不可否認性、企業存取控制或法律認證的數位簽章。保留 loopback、Origin 與 CSRF 邊界，不應對公開網路部署或轉送。尚未核定的權限、數量精度、捨入與其他政策不可由範例推定。

授權採用既有 [MIT LICENSE](LICENSE)，其文字與原遠端歷史保留。
