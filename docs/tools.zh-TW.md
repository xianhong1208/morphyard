# 工具與格式

[English](tools.md) · [README](../README.zh-TW.md)

## 工具

| 工具 | 用途 | 回傳 |
|---|---|---|
| `convert_document` | 轉換成目標格式 | 檔案 |
| `extract_text` | 抽取內容為 Markdown 供模型閱讀 | 文字 |
| `inspect_document` | 格式、大小、頁數與可到達的目標格式 | 文字 |
| `list_supported_conversions` | 能力矩陣（只含已安裝的引擎） | 文字 |
| `pdf_extract_pages` | 抽頁或重排頁序 | 檔案 |
| `pdf_split` | 切分成多個檔案 | 多個檔案 |
| `pdf_rotate` | 旋轉指定頁面 | 檔案 |
| `pdf_protect` | AES-256：開啟密碼，以及／或由擁有者密碼保護的權限 | 檔案 |
| `pdf_unlock` | 憑密碼移除密碼與權限限制 | 檔案 |
| `pdf_compress` | 縮小 PDF（`lossless` / `balanced` / `small`） | 檔案 |
| `pdf_metadata` | Info、XMP、版本、頁數、加密、腳本、附件 | 文字 |
| `pdf_strip_metadata` | 移除中繼資料（可選擇一併移除腳本／附件） | 檔案 |

**支援的格式**

| 類別 | 格式 |
|---|---|
| 文件 | pdf, docx, doc, odt, rtf, epub |
| 試算表 | xlsx, xls, ods, csv, tsv |
| 簡報 | pptx, ppt, odp |
| 文字 | md, html, txt, rst, tex, json, xml |
| 圖片 | png, jpg, webp, gif, bmp, tiff |

不支援：音訊、視訊、壓縮檔、CAD、可執行檔。工具描述與 [`config/instructions.md`](../config/instructions.md) 會告訴模型直接說明不支援，而不是反覆重試。

## PDF 壓縮、密碼與中繼資料

- **`pdf_compress`** — `lossless` 只重整檔案結構（object stream、重新壓縮 stream、移除未使用的物件），頁面不變。`balanced`（預設）另把高於 150 DPI 的圖片降解析度並重新編碼成 JPEG 品質 75；`small` 為 96 DPI、品質 50。`max_dpi`（36–600）與 `jpeg_quality`（10–95）可覆寫等級的設定。DPI 依每張圖片在頁面上實際繪製的大小計算，所以縮小顯示的圖片會降得更多。文字、字型與向量圖形一律不降質。若結果沒有小至少 1 %，就回傳原檔並在回覆中說明。掃描檔縮得最多（六頁 300 DPI 掃描：11.0 MB → balanced 1.8 MB、small 0.64 MB）；純文字 PDF 只靠重整就能縮小（實測 28–51 %）。
- **`pdf_protect`** — AES-256。`password` 是開啟密碼；`owner_password` 保護 `allow_print`、`allow_copy`、`allow_modify`、`allow_annotate`、`allow_fill_forms` 這些權限（預設全部允許）。任何限制都需要擁有者密碼，且必須與開啟密碼不同。在 PDF 格式中，註解權限包含填寫表單，所以要禁止填表也必須設 `allow_annotate=false`。權限由一般的閱讀器遵守，並非 DRM。
- **`pdf_unlock`** — 憑密碼回傳未加密的副本。擁有者密碼一定可用；開啟密碼只在沒有任何限制時可用。不提供密碼破解。
- **`pdf_metadata`** — 唯讀：版本、頁數、加密、Info、XMP、註解作者、腳本與動作、內嵌檔案、表單。
- **`pdf_strip_metadata`** — 一律移除 Info 字典、所有 XMP stream 與 PieceInfo，並給檔案新的識別碼；預設也移除註解的作者名稱與日期（不動表單欄位名稱）。可選擇一併移除腳本與動作（`remove_javascript`）及內嵌檔案（`remove_attachments`）。寫在頁面文字裡的名字不會處理。

除了 `pdf_unlock`（以及會回報加密狀態的 `pdf_metadata`），所有工具都會拒絕已加密的 PDF 並指向 `pdf_unlock`。限制：5,000 頁；超過一億像素的圖片不重新編碼。

## 運作方式

```
client ──(bearer token)──▶ /mcp ──▶ tools ──▶ service.py ──▶ registry.plan_conversion() ──▶ engines
                             │
                             └── JWTVerifier(jwks_uri = MCP_GATE_URL/.well-known/jwks.json)
```

1. **輸入。** 工具接收 `file_content`（base64）、`file_name` 與 `mime_type`。管理上傳檔案的 host 通常會自動注入 base64；參數描述的措辭刻意引導模型只把上傳參照放進 `file_content`，不放在其他地方。
2. **偵測。** `detect_format()` 依序嘗試副檔名、MIME、容器內部結構、檔頭魔數 —— 因為實務上檔名經常缺席。
3. **規劃。** `plan_conversion()` 找出直接引擎或最多三跳的路徑，依 fidelity、跳數與中繼格式偏好排序。直接路徑永遠不會被拒絕。低於 `structural` fidelity 的多跳路徑會照轉並附上警告；`allow_quality_loss=false` 時則改為拒絕。
4. **執行。** 引擎以子程序在暫存工作區中執行並設有逾時；掃描版 PDF 會自動從文字抽取 fallback 到 OCR。
5. **輸出。** 工具回傳 `[摘要文字, File(...)]`；檔案以 MCP `EmbeddedResource` 形式送達。

| 術語 | 意義 |
|---|---|
| **Fidelity** | `high`（保留版面）、`structural`（保留標題／清單／表格）、`lossy`（只有文字）。驅動路徑規劃。 |
| **Hop** | 多步驟路徑中的一次引擎呼叫。 |
| **Audience** | token 中的 resource URI；必須與 MCP Gate 為本伺服器註冊的值相同。 |

引擎表、規劃器的規則與格式偵測的細節在 [docs/architecture.md](architecture.md)。

## 已知限制

- 一次呼叫只處理一個檔案（尚無 `pdf_merge` 工具；合併邏輯已在 `pdfops` 中實作）。
- 單檔 50 MB，每次 OCR 最多 30 頁。
- 同時執行的轉換數有上限（預設為 CPU 數，最多 8）；其餘排隊等待，超過佇列長度或等待時間時，工具會回答「伺服器忙碌」，稍後再呼叫即可。見[同時轉換](configuration.zh-TW.md#同時轉換)。
- PDF → 可編輯格式一定有損；PDF 內的表格以啟發式方式還原。
- 圖片會保留，但附加在抽取出的 Markdown 末尾，而非原本的位置。
- 不支援 `.xlsb`。
- LibreOffice 轉檔同樣不會讀取文件所連結的檔案或網址：以路徑或網址連結的圖片（HTML 的 `<img>`、DOCX/XLSX/PPTX 中標為 external 的 relationship、ODF 的 `xlink:href`、RTF 的 `INCLUDEPICTURE` 欄位）不會出現在輸出中，樣式表也不會被下載。每次轉檔都使用一個會封鎖不受信任文件連結、並把所有 HTTP/HTTPS/FTP 流量導向不存在之 proxy 的 LibreOffice 設定檔；ODF、OOXML 與 RTF 文件還會先移除指向外部的參照（依內容判斷，所以改名成 `.pptx` 的 ODP 仍當作 ODP 處理）。文件內嵌的圖片會保留。無法完整讀取以進行檢查的 zip 類文件會被拒絕，而不是照樣轉檔。
- Pandoc 轉檔不會讀取文件所指向的檔案或網址：LaTeX 的 `\input` / `\lstinputlisting` / `\includegraphics`，以及用路徑或 `http(s)` 網址引用的圖片都會被略過（圖片保留 alt 文字）；文件內嵌或以 `data:` URI 提供的圖片會保留。
- 撤銷不是即時的：啟用 [revocation feed](configuration.zh-TW.md#token-撤銷) 時，被撤銷的 token 會在一個輪詢間隔內失效（預設 30 秒）；未啟用時則要等到過期。
