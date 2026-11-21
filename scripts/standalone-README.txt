TIGER-ERP 獨立 Windows 系統
=========================

適用環境：相容的 Windows 10／11 x64 電腦與本機桌面瀏覽器。
本版本使用 Python 3.14 x64 封裝。尚未在所有 Windows 版本或防毒環境驗證。

開始使用
1. 將 TIGER-ERP-Standalone.zip 完整解壓縮到可寫入的資料夾。
2. 開啟 TIGER-ERP 資料夾，雙擊 TIGER-ERP.exe。
3. 等待預設瀏覽器自動開啟 ERP。無須安裝 Python、Node.js、npm 或連線網際網路。
4. 請保留顯示啟動網址的主控台視窗。在該視窗按 Ctrl+C 可安全結束 ERP。
   關閉瀏覽器分頁不會結束服務；可使用主控台上的網址重新開啟。

資料保護與獨立性
此 ZIP 包含確認匯出當時 SQLite 資料庫的全部實際資料，包括客戶、供應商、
採購、製造、庫存與銷售紀錄，也可能包含資料庫內已儲存的敏感值或憑證。
請妥善保管 ZIP、seed、data 與其備份。此匯出未提供企業級加密。

首次啟動會透過 SQLite backup API，將 seed/erp.sqlite3 完整快照初始化為
此安裝資料夾自己的 data/erp.sqlite3。之後每次啟動只使用既有 data/erp.sqlite3，
不會從 seed 覆寫、不會重新植入範例，也不會在錯誤時改用空白資料庫。
若缺少原始快照、資料庫檢查失敗或目的地唯讀，程式會明確顯示啟動錯誤。

目前 ERP 與這個獨立系統各自獨立，後續資料不會自動同步。不同解壓縮資料夾
亦各自持有自己的 data。若要移動或備份本系統，請先按 Ctrl+C 完整結束程式，
再複製整個 TIGER-ERP 資料夾。請保留所有資料庫與可能存在的 WAL/SHM 檔案。
不要只複製執行檔，也不要以舊的 seed 覆蓋目前 data。

服務與安全狀態
前端與後端由同一個本機程序提供，只監聽 127.0.0.1 的可用動態連接埠。
埠號與網址顯示於主控台，執行時亦儲存在 data/startup.json。
既有 30 項業務功能與本機手動逐單確認行為沿用原 ERP。
尚未啟用正式電子簽核與身分權限驗證；D01–D16：PENDING。
SECURITY：PARTIAL — RISK CONTAINED。請勿將此本機服務轉接到公用網路。

再次匯出
可由上方「匯出獨立系統」按鈕確認匯出此獨立安裝當時的完整資料。
匯出使用此資料夾隨附的已驗證 runtime，無須 Python、Node.js 或原始專案。

授權資訊
本系統包含 Python 與第三方套件；隨附的 THIRD-PARTY-LICENSES.txt 保留
執行階段第三方授權文字。PyInstaller 採 GPL 與 bootloader 例外，允許分發
封裝應用；應用及相依套件的原有授權義務仍適用。
