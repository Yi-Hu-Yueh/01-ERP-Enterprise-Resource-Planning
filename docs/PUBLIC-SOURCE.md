# 公開原始碼與資料邊界

公開庫提供應用程式、遷移、建置工具、依賴鎖定檔與合成測試程式。Owner 於2026-11-22另明確指定公開10份文件：COMPLETE／COMPACT對話文字快照、AGENTS.md、直接送簽與電子簽核的檔案清單及實作報告、Owner決策審閱及兩版系統分析。這是針對指定檔名的授權，不表示所有私人檔案皆可公開。

指定文字快照可能包含專案需求、歷史驗收、檔案路徑及業務案例。此次未上傳其圖片／附件、SQLite資料庫、備份、Office文件或EXE／ZIP。Markdown中指向本機或conversation-assets的連結，可能無法在GitHub開啟。對話快照以提交時內容為準；後續本機紀錄不會自動同步。

.gitignore保留data、artifacts、backups、conversation-assets、runtime、node_modules、虛擬環境、實際.env、資料庫側車、私鑰及成品封裝的排除，並只對指定10份文件新增精準放行。不得依此推定其他Owner業務資料也獲准上傳。

測試中的技術性Actor識別碼與DEMO角色是程式設計內容，不是驗證過的帳號。部分歷史驗證工具需要未公開的本機基準；不要為重播測試而重建或修改Owner資料。
