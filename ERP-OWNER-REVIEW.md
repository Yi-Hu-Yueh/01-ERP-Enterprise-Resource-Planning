# ERP 第二輪：Owner集中審查與決策單

**D01–D16 未明確核准的企業政策細節：仍全部 PENDING。另見下方 Owner 已核准的 LOCAL DEMO 常設授權。** 這是一份讓Owner一次整體審查的業務決策單，不是要求逐表核准，也不是開發完成報告。下列「建議預設」是提供選擇的推薦，未填、未回覆、沒有反對或閱讀過本文，都不等於同意。

配套設計：[ERP-SYSTEM-ANALYSIS.md](ERP-SYSTEM-ANALYSIS.md)。本輪保留六模組30作業；只修正「多輪MRP承諾來源不應被覆蓋」及「盤點凍結不能只寫一句自由備註」，並提出本機Web技術方案。沒有開始寫ERP程式或建資料庫。

## LOCAL DEMO — Owner standing manual inventory-posting authorization (Round16B)

**APPROVED — TRUSTED LOCAL DEMO ONLY.** The Owner expressly grants one-time standing authorization for ordinary, explicitly human-confirmed stock transactions needed by the already-defined30 ERP operations. The genuine decision is preserved in [the original Round16B request](conversation-assets/round16b-2026-11-19T23-09-04-739Z/Pasted%20text.txt). This is separate from the unresolved D01–D16 enterprise-policy proposals; their original decision fields below remain PENDING.

An eligible inventory document may post only through an implemented, specifically registered local-demo action, after the operator reviews its genuine source documents, exact quantities/base unit, work order or other business identity, warehouse/lot/position, movement direction and expected stock effects, then explicitly confirms. The backend and database independently enforce source eligibility, exact arithmetic, versions, aggregate limits, atomicity, idempotency, immutable history and operation-specific tests. Saving, opening, reviewing, Cancel/Escape and F5 do not authorize or perform posting.

The existing dedicated127.0.0.1 local-demo launcher now registers signed manufacturing material ISSUE and RETURN actions (material_issue/material_return). Their runtime/database/request scopes and transaction grants remain operation-specific. Existing purchase receipt and supplier-return manual-posting workflows are already implemented and retain their established source/transaction guards; they were not retrofitted into the new manufacturing HMAC registry in Round16B. Controlled isolated QA authorization remains separate. Ordinary create_app does not enable the signed manufacturing posting capabilities. No nonexistent operation is pre-enabled.

Future ordinary manufacturing, purchasing, sales or inventory operations may reuse this standing trusted-local-demo manual-posting principle without another general Owner approval request for that same mechanism. Each future operation still requires its own authorized development scope, real source rules, correct inventory effects, explicit registration, working implementation and verification before activation. Standing approval is not a wildcard SQL grant and does not authorize operation19 or any other development in this round.

No automatic posting, fabricated documents/stock, arbitrary balance writes, deletion/rewriting of posted history, automatic reversal/correction, source-eligibility override, public-network production deployment, enterprise login/RBAC/electronic signature, accounting, quality, substitution, reservation or other unapproved complex policy is approved here. Genuinely new materially consequential business policy still requires an explicit Owner decision. Do not repeatedly ask for the already-approved general local-demo manual-posting capability.

Round16B specifically activates manually confirmed material RETURN posting. It does not authorize Codex to post the Owner's MRET-TEST-001 or change its0.75 source/quantity/stock2.25. Only the Owner performs the final live confirmation. Automated QA is not Owner functional acceptance; Round16B acceptance remains PENDING. Local-demo operators are not authenticated enterprise employees.

## 先決定哪些，哪些可以稍後決定

| 優先階段 | 決策 | 為什麼此時需要 |
| --- | --- | --- |
| A：基礎實作前需明確接受的方向 | D01、D03、D04、D06、D14 | 品項範圍、識別方式、精確數字契約、歷史保護／責任及平台，會影響基礎資料與交易架構 |
| B：首批訂單／實際庫存異動前 | D02、D05、D08；D16若要帶入期初資料 | 稅價、數量例外、批次證據與期初來源，不能等到收發已發生才補決定 |
| C：各對應模組開始前 | D07、D09、D10、D11、D12、D13、D15 | MRP、退貨承諾、製程、盤點／調撥、客退、WIP及統計，依開發順序集中決定即可 |

D04的表示／取位責任先定，但各幣別與單位的具體粒度仍可在該業務啟用前補齊。D06的不可抹歷史與最小責任人先定，各模組細部授權可於該模組實作前定。D16如本次沒有匯入舊資料，可等到真正匯入或期初過帳前。後段未定事項不阻止已明確批准且不依賴該事項的基礎工作；但本輪沒有自動取得任何基礎實作授權。

## A類：基礎方向

### D01 — 我們要管理哪些品項？

**業務問題：** 本次是不是先管理「買原料、做成品、賣成品」？會不會直接轉售原料，或買入現成成品來賣？

**建議預設：** 原料與成品共用一套料號識別，分兩種維護入口；先依既定流程採購原料、製造成品、出貨成品。分類維護共用，避免相同品項出現兩套庫存。

**相關替代：** 若確有原料轉售或成品外購，一次提出，讓來源／銷售規則一起擴充，而非上線後只改品項類別。

**後果：** 影響採購、BOM、製造、出貨可選品項；同料號被使用後不能隨便改基本單位或改成另一種貨。

**時程：** 基礎品項建模前需確認；額外買賣模式可明確延後為後續範圍。

**Owner決定：PENDING — ______________________________**

### D03 — 單號、料號及供應商報價如何辨認？

**業務問題：** 要用什麼單號查採購、工單和出貨？報價何時生效，能不能同一條件同時有多個價格？

**建議預設：** 系統內部使用不會變的識別；Owner看可讀單號。每種單據內單號唯一，不重用舊號；原料與成品料號共用唯一性。供應商相同原料／單位／幣別的報價依有效日期分版本，避免有效期間重疊。

**相關替代：** 依年度、單別編號；同時有數量階梯價。階梯價需要額外定義，現方案不假稱已支援。

**後果：** 改名稱不改舊單的對象；原單成交價有快照，日後報價改價不回改舊單。單號格式尚待Owner選定，不能靠重編單號掩蓋重複操作。

**時程：** 內部識別及業務代碼方向在基礎前定；單號顯示格式及進階報價可在首張對應單據前定。

**Owner決定：PENDING — ______________________________**

### D04 — 數量和金額怎麼算，哪些可以拆零？

**業務問題：** 紙張、膠、成品各用什麼基本單位？可以有小數嗎？價格、總額、耗損及單位換算要在哪裡取位？

**建議預設：** 數字精確儲存與計算；先逐行算價及依幣別取位，再加成整張金額。單據保存當時換算，不隨後續單位設定回改。各幣別及各單位的具體小數位／最小粒度由Owner按真實交易確認，本輪不擅自指定。

**相關替代：** 最後才對整張金額取位；特定成品只能整件。兩種取位法可能有尾差，不宜混用。

**後果：** 本機SQLite中數量／金額以精確數字文字保存，後端精確計算，不能用近似浮點數。除不盡的BOM或換算需明確取位規則，不能偷偷截斷。退貨、沖回、調整各有方向，不能全部硬設「不准負數」。

**時程：** 精確表示與取位責任是基礎前先決；具體幣別、單位、換算及負數輸入語義，須在相關金額／庫存流程啟用前填妥。

**Owner決定：PENDING — ______________________________**

### D06 — 誰能核准、更正或取消單據？

**業務問題：** 已經領料、收貨或出貨的單，能否直接改掉？誰負責核准及更正？

**建議預設：** 已被使用／過帳的原始資料保留。還沒執行的餘量可以有原因取消；已發生的異動用有來源的相反事件，再開正確單據，不刪掉歷史。主檔用停用取代刪除。發放後工單不偷偷換BOM／計畫量。

**相關替代：** 允許特定核准的修訂版本，但必須保留原版與責任，不能直接覆寫。

**後果：** 保留供應商、客戶、品項及批次的歷史關係；區分「訂單取消」、「真的退貨」和「輸入錯誤沖回」。取消MRP承諾也保留原事件，後輪來源不能蓋掉前輪。

**時程：** 歷史保護與最小責任辨認在基礎前確認；各模組誰建單、誰核准、誰過帳及是否分工，可在該模組啟用前一起定。

**Owner決定：PENDING — ______________________________**

### D14 — 這套示範ERP先如何運作、誰能操作？

**業務問題：** 是否先在一台本機運作，由瀏覽器操作？是否接受同一時間只有一筆寫入工作，其他短暫等待？

**建議預設：** 使用React＋TypeScript＋Vite、Tailwind CSS；Python＋FastAPI；SQLAlchemy＋Alembic；本機SQLite；後續用pytest與Playwright驗證。延續既有登入身分與操作人員責任記錄；若沒有既有登入方式，需要另確認最小登入方案，不能把不明使用者當已驗證身分。

**相關替代：** 若實測寫入排隊無法達到Owner接受的反應時間，或需要多台寫入主機、資料庫獨立服務與進階備援，改PostgreSQL。多人開瀏覽器使用同一API，不必然需要立即換DB。

**後果：** SQLite沒有逐列鎖；每筆重要操作在取得寫入權後重新確認資料，完整成功才一起存入，失敗全部撤回。會設定有限等待／重試，數字用精確方式，並保護盤點凍結。備份需包含已提交資料，不能運作中只拷貝一個主檔。細節在分析8.1，尚未安裝／實測。

**時程：** 基礎實作前須接受部署、資料庫、精確運算、責任及交易方向；備份頻率、可接受等待、復原時間與細部權限須於相關驗收前確認。

**Owner決定：PENDING — ______________________________**

## B類：首批訂單或實際庫存前

### D02 — 單價是否含稅？是否有折扣、運費及不同幣別？

**業務問題：** 目前交易實際用哪些幣別、稅務／稅籍格式？單價和總額要包含哪些內容？

**建議預設：** 一張單同一幣別，以未稅單價與行金額為第一版；本輪不默認加入折扣、運費分攤、會計應收應付或匯兌結算。統計不同幣別分開列。

**相關替代：** 含稅價、行折扣、整單折扣、運費分攤或本位幣換算；需Owner說明實務。

**後果：** 增加項目就要同步增加欄位與SUM規則；不能把不同幣別直接相加當總營業額，也不能把運費悄悄塞進未說明的單價。

**時程：** 可在共用基礎後定，但第一張有價格的採購／銷售單實作前必須確定。

**Owner決定：PENDING — ______________________________**

### D05 — 可不可以先出貨後補帳，或多收、多領、多做？

**業務問題：** 是否允許負庫存、超收、超出、超領或超產？遇到例外誰核准？

**建議預設：** 不允許無依據的負庫存，也不拿已保留給別工單的材料直接使用。超量需有明確授權；本輪不擅自指定百分比或數字上限。

**相關替代：** 有責任人及依據的例外放行，需定義原因、權限和後續對帳方式。

**後果：** 每次實際收發都重新核對，而非相信畫面先前數量；超交或負數異常直接揭露，不靠把報表數字改成0隱藏。

**時程：** 可等庫存核心前決定，但任何實際收發、沖回或庫存驗收前不能留白。

**Owner決定：PENDING — ______________________________**

### D08 — 批號要追到多精確？材料混批怎麼辦？

**業務問題：** 同一工單用了幾批紙、膠或封面，能否知道各成品批用了多少？出貨選哪一批？

**建議預設：** 原料及成品都用真正批次；先依實際入庫先後選批。量測足夠才標「精確分攤」；只知道同一製造批混用時，標「可能關聯」，召回範圍保守涵蓋，不捏造精確用量。

**相關替代：** 按到期日先出；要求只能在取得精確投料證據後入庫。是否允許先入庫後補領料也需明確決定，現設計不預先放行。

**後果：** 支援一工單多原料批、一原料批多工單、多次成品入庫及多批出貨。精確模式增加現場記錄負擔；保守模式會放大可能召回範圍。單一文字批號不足以達成追溯。

**時程：** 可在一般主檔基礎後定，第一次建立實際批次庫存與製造追溯前須確認。

**Owner決定：PENDING — ______________________________**

### D16 — 期初庫存和舊資料如何帶進來？

**業務問題：** 要匯入哪些既有單據、庫存及批號？原供應商或工單來源是否有證據？

**建議預設：** 以核准的期初／盤盈調整帶入，保留真實批次與來源；不知道的來源明示未知，不造假歷史工單或供應商。

**相關替代：** 另做有原始證據、映射及核對的舊資料遷移專案。

**後果：** 影響追溯完整性與開帳對帳；未知來源不能被報表當成已精確追溯。現有資料不可任意刪掉重建。

**時程：** 本輪不匯入資料，可延後；若下一階段要開期初庫存，必須在該作業前確認。

**Owner決定：PENDING — ______________________________**

## C類：對應模組前決定

### D07 — 缺料時先分給哪個工單？材料怎麼保留？

**業務問題：** 多工單同時要同一原料時，優先順序、未到採購及退料後保留如何處理？

**建議預設：** 第一版單層BOM，按預計開工日、工單單號及用料行序分配合格現有庫存；所有工單已領／已保留都一起扣算。試算不佔料，確認後才承諾；退料不無條件重建保留。

**相關替代：** 優先交期或特定客戶；加入未到PO的時間相依供應；允許部分確認。這些需要另外定義，不把未到貨當現貨。

**後果：** 不同輪MRP可以對同需求／位置追加承諾，每次保留自己的來源事件；前輪不被覆蓋。確認時重驗最新供應，重按確認不會加倍；缺料就重新規劃而非讓兩單用同一批餘量。

**時程：** 共用基礎可先做；MRP／保留模組與領退料保留連動啟用前須決定。

**Owner決定：PENDING — ______________________________**

### D09 — 退貨後，原訂單還要再交一次嗎？

**業務問題：** 退供應商之後是否等補貨？客人退貨後是否要補交？是否只重開其中部分？

**建議預設：** 實物退回不自動增加原訂單待交量。需要補交的部分明確核准記錄「重開量」。

**相關替代：** 全部重開、部分重開，或另外開替換單。

**後果：** 實際庫存與原單履約分開算，避免退貨同時被錯當新收貨／新需求。選項會改變採購未交與銷售未交數字。

**時程：** 基礎後可定，但供應商退貨／客退與未交查詢啟用前須一致決定。

**Owner決定：PENDING — ______________________________**

### D10 — BOM耗損、版本和製程要多複雜？

**業務問題：** 每批要多少原料、損耗怎麼估？工法是否固定順序，有沒有替代料、平行步驟或重工？

**建議預設：** 第一版單層原料BOM；每個產出基準記標準用量及額外耗損量。成品BOM和途程按日期／版本管理；工單發放時凍結快照，依序執行作業。

**相關替代：** 比例耗損、替代料、平行分流、重工或多階BOM。需要擴充後再審查，不是現模型開一個開關就有。

**後果：** 改新BOM不回算既有工單，避免已領料與需求突然漂移。未提供耗損時不憑空假設工廠比例。

**時程：** 可等製造主檔／工單模組前決定。

**Owner決定：PENDING — ______________________________**

### D11 — 盤點時能否暫停整個倉庫收發？調撥是否有在途？

**業務問題：** 清點期間，可否整倉暫停收貨、領料、出貨、退料和調撥？兩倉移轉是否立即到倉？

**建議預設：** 第一版整倉盤點。選定倉庫後，系統用明確倉別與凍結狀態保護所有新舊批次；先凍結並取帳面，再初盤／複盤、核准差額，最後一次存入所有盤差並解凍。調撥以同時扣來源、加目的為預設。

**相關替代：** 日後部分位置盤點／不停倉盤點，或在途倉與分兩階段接收；現輪不聲稱已支援。

**後果：** 凍結倉的收發、退貨、品質變更、調整及沖回都被統一擋住；新批號不能繞過。失敗保留凍結，可重試或授權取消；不靠關閉UI按鈕。若不能停倉，需先接受較複雜的範圍及異動重算設計。

**時程：** 基礎可先做；盤點及調撥模組啟用前須確認。凍結屏障的預留設計應在庫存核心實作時一併保留。

**Owner決定：PENDING — ______________________________**

### D12 — 客退要先驗貨嗎？找不到原出貨怎麼辦？

**業務問題：** 回來的成品能否直接再賣？不良品怎麼報廢？沒有原單是否收退？

**建議預設：** 有原出貨才一般收退，先待驗；核准後轉可售，或以有原因調整報廢。批號與原出貨一致，不因退貨變成無來源新貨。

**相關替代：** 直接可售、或另開無原單隔離審查流程；後者需明確擴充來源結構，不能事後虛填原單。

**後果：** 待驗仍是實際庫存，但不是可賣或MRP可用。品質放行要有依據，未知來源需標示追溯缺口。

**時程：** 客退／品質處置模組前確認；一般主檔不必等待。

**Owner決定：PENDING — ______________________________**

### D13 — 沒用完的在製材料、不良品及結案如何處理？

**業務問題：** 工單已領的材料沒有全部變成良品時，要退倉、留在製、報廢還是回收？何時可以結案？

**建議預設：** 已領未退的材料留在工單的耗用／在製範圍，不因關單自動回倉。終端不良不當可用成品。回報良品與實際成品入庫分開，避免同一批加庫存兩次。

**相關替代：** 專用在製／報廢倉或品項、可回收副產品、另計成本，需要增加明確結構。

**後果：** 影響工單結案、MRP需求停止、保留釋放、追溯分攤及損耗；不能把BOM標準量當實際耗用證據。

**時程：** 製造領退料、終端完工及工單結案啟用前確認。

**Owner決定：PENDING — ______________________________**

### D15 — 報表按哪一天？分類和成本要看哪個版本？

**業務問題：** 看業務發生日還是系統入帳日？品項改分類後舊報表跟著改嗎？需要庫存估價與毛利嗎？

**建議預設：** 同時保留業務日與過帳時間，報表清楚標示口徑；分類先依現行主檔。不跨幣直接相加，未核准成本法前不提供假毛利或庫存價值。

**相關替代：** 只按過帳日；儲存歷史分類快照；另外指定成本法。

**後果：** 訂購、實收、退貨、逐站進度、終端完工及出貨分開統計，不以訂量冒充業績或把每站良品累加當產量。歷史分類／成本法可能需要額外快照或模型。

**時程：** 共用基礎可先做；統計／估價模組前確認。若要求歷史分類，須在開始保留相關交易前定，不能事後捏造。

**Owner決定：PENDING — ______________________________**

## 一次整體回覆即可

請以本文件的一組建議整體審查，保留各D編號即可表達「接受建議／採替代／修改條件／延後」。A類若未決，先不要啟動受影響的基礎實作；後段可以明確保留PENDING直到該模組。沒有要求十六次互動審批，沒有要求逐張資料表核准。

所有空白Owner決定欄均為PENDING；本文件不預選、代填或推定Owner接受。第二輪結束後停止，等待你的集中回覆。
