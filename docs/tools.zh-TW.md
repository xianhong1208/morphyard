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
| `pdf_merge` | 依序合併 2–20 個 PDF，每個檔案一個書籤 | 檔案 |
| `pdf_delete_pages` | 刪除指定頁面 | 檔案 |
| `pdf_reorder_pages` | 把所有頁面排成新順序（`3,1,2,4-10`、`10-1`、`reverse`） | 檔案 |
| `pdf_watermark` | 文字或圖片（PNG/JPEG）浮水印：置中、對角線或平鋪；透明度、顏色、角度；全部或指定頁面 | 檔案 |
| `pdf_page_numbers` | 頁碼（`{page}`、`{total}`）、頁首與頁尾文字；位置、起始號碼、跳過第一頁、字級、邊距 | 檔案 |
| `pdf_to_images` | 把頁面轉成 PNG、JPEG 或 WebP，每頁一張圖 | 檔案（單頁）或 ZIP |
| `images_to_pdf` | 一張圖片、多頁 TIFF 或圖片 ZIP → 一個 PDF，每張圖一頁 | 檔案 |
| `job_status` | 取得工具回覆時仍在執行的轉換結果（最多等 20 秒） | 與原工具相同 |
| `job_cancel` | 停止排隊中或執行中的轉換 | 文字 |
| `job_list` | 你的轉換：排隊中、執行中與最近完成的 | 文字 |

**較久的轉換。** 每個會轉換的工具都在 20 秒內回覆：若到時還沒完成，回覆會給一個 `job_id`，轉換繼續進行；結果好了之後用 `job_status` 取得。見[工作](configuration.zh-TW.md#工作)。

**PDF 頁面轉圖片。** `pdf_to_images` 以 Poppler 的 `pdftoppm` 繪製：`image_format` 為 `png`（預設）、`jpg` 或 `webp`；`dpi` 36–300（預設 150）；`pages` 如 `1-3,7`（空白 = 全部，每次最多 50 頁）；`quality` 1–95，用於 JPEG / WebP。某頁在指定 DPI 下會超過 2500 萬像素時，改用較低 DPI 繪製，並在回覆中說明。單頁回傳圖片本身；多頁回傳一個 ZIP（`<name>_p01.png`…），因為有些 client 會把圖片 resource 變成模型輸入，而 ZIP 對使用者是一次下載。全部圖片合計不得超過 50 MB。有開啟密碼的 PDF 會被拒絕（只有權限密碼的 PDF 可以繪製）。`convert_document` 從 PDF 轉成圖片格式時，會指引模型改用這個工具。

**圖片轉 PDF。** `images_to_pdf` 接收一個檔案：一張圖片（PNG、JPEG、WebP、TIFF、GIF、BMP）、多頁 TIFF 的每一頁、GIF / WebP 動畫的第一格，或一個圖片 ZIP，頁序依檔名的自然順序（`page2` 在 `page10` 之前；非圖片檔、`__MACOSX/` 與隱藏檔會略過並列出）。`page_size` 為 `fit`（預設，每頁等於圖片大小，依檔案記錄的解析度，沒有則以 96 DPI 計）、`a4` 或 `letter`（圖片縮放到邊界內並置中）；`orientation` 為 `auto`（寬圖用橫向）、`portrait` 或 `landscape`；`margin_mm` 0–50（`fit` 預設 0，其他預設 10）。會套用 EXIF 方向；JPEG（以及相機 MPO 檔的主圖）逐位元組原樣嵌入，方向由頁面上的擺放方式處理；其他圖片以無損方式儲存；透明處變成白色；相符的 ICC 設定檔會保留。上限：100 頁、單張 5000 萬像素、合計 4 億像素、ZIP 500 個項目、解壓後 200 MB、輸出 100 MB。HEIC / AVIF 尚不支援（1.6）。`convert_document` 把單張圖片轉成 `pdf` 時使用同一套程式，頁面為 `fit`；先前它會對圖片做 OCR，回傳一個只有文字的 PDF。

### PDF 頁面工具

- **輸入。** `pdf_merge` 接收 `files`：一個 `{file_content, file_name}`（base64）、`{file_id}`（先前的結果）或 `{url}`（由 Morphyard 下載，只限公開位址）的清單，因為 host 的上傳注入每次呼叫只填一個 `file_content`，所以由呼叫端自己傳入檔案內容。其他頁面工具使用一般的三個參數；`pdf_watermark` 的圖片以 base64 放在 `watermark_image`。
- **頁面。** 頁面選擇與 `pdf_extract_pages` 相同，從 1 起算（`1-3,7,10-`）。`pdf_reorder_pages` 要求每頁剛好出現一次，並指出缺少或重複的頁碼；要拿掉頁面請用 `pdf_delete_pages`。
- **保留。** 合併時，每個檔案原有的書籤會放在以該檔名命名的書籤底下。刪除與重排會保留指向仍存在頁面的書籤，並指到新位置。浮水印與頁碼會保留書籤、連結、表單欄位與中繼資料。
- **位置。** 浮水印、頁碼、頁首與頁尾都畫在讀者看到的頁面上（crop box，再依頁面旋轉轉正），所以橫向、旋轉過的頁面與大小不一的文件上都是正的。單一浮水印或頁首太長時會縮小到放得下。
- **編號。** 只計算有蓋上的頁面：`{page}` 在這些頁面上依序是 `start_number`、`start_number + 1`……，`{total}` 是最後顯示的號碼；所以 10 頁的檔案跳過封面時，會是「1 / 9」到「9 / 9」。
- **字型。** 文字以 DejaVu Sans（拉丁字母）與 Droid Sans Fallback（中日韓文字）繪製，依字元段落切換字型，並以子集內嵌。這裡不能用 Noto CJK：它是 OpenType/CFF，reportlab 無法內嵌。若未安裝 Droid Sans Fallback，中日韓文字會改用不內嵌的 CID 字型，由閱讀器自行替換。
- **限制。** 合併：2–20 個檔案，合計 50 MB。每個頁面工具：每份文件 2,000 頁、輸出 100 MB、文字 200 字元、浮水印圖片 10 MB / 4,000 萬像素。加密的 PDF 會被拒絕；請先移除密碼。

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

1. **輸入。** 工具接收 `file_content`（base64）、`file_name` 與 `mime_type`，或改給 `file_id`：先前某個 Morphyard 工具回傳、仍留在伺服器上的結果（見[用 file_id 串接工具](configuration.zh-TW.md#用-file_id-串接工具)），或給 `url` 讓 Morphyard 自行下載，只限公開位址（見[網址輸入](configuration.zh-TW.md#網址輸入)）。管理上傳檔案的 host 通常會自動注入 base64；參數描述的措辭刻意引導模型只把上傳參照放進 `file_content`，不放在其他地方。
2. **偵測。** `detect_format()` 依序嘗試副檔名、MIME、容器內部結構、檔頭魔數 —— 因為實務上檔名經常缺席。
3. **規劃。** `plan_conversion()` 找出直接引擎或最多三跳的路徑，依 fidelity、跳數與中繼格式偏好排序。直接路徑永遠不會被拒絕。低於 `structural` fidelity 的多跳路徑會照轉並附上警告；`allow_quality_loss=false` 時則改為拒絕。
4. **執行。** 引擎以子程序在暫存工作區中執行並設有逾時；掃描版 PDF 會自動從文字抽取 fallback 到 OCR。
5. **輸出。** 結果檔案會在伺服器上保留一小時，回覆的文字與 MCP `resource_link` 都帶有它的下載連結與 `file_id`；預設（`auto`）下，10 MB 以內的檔案也會像 1.4 之前一樣以 MCP `EmbeddedResource` 內嵌送達。見[回傳方式](configuration.zh-TW.md#回傳方式)。

| 術語 | 意義 |
|---|---|
| **Fidelity** | `high`（保留版面）、`structural`（保留標題／清單／表格）、`lossy`（只有文字）。驅動路徑規劃。 |
| **Hop** | 多步驟路徑中的一次引擎呼叫。 |
| **Audience** | token 中的 resource URI；必須與 MCP Gate 為本伺服器註冊的值相同。 |

引擎表、規劃器的規則與格式偵測的細節在 [docs/architecture.md](architecture.md)。

## 已知限制

- 一次呼叫只處理一個檔案，`pdf_merge` 除外：它以清單接收 base64 檔案或 `file_id`（只能注入一個上傳檔的 host 目前還無法用 base64 餵它；一次呼叫多個上傳檔排在 1.4）。多張圖片請用 ZIP 交給 `images_to_pdf`。多步驟的工作（先轉檔、再加浮水印、再壓縮）用 `file_id` 把每一步的結果交給下一步，檔案不必再經過模型。
- 單檔 50 MB，每次 OCR 最多 30 頁。
- 同時執行的轉換數有上限（預設為 CPU 數，最多 8）；其餘排隊等待，超過佇列長度或等待時間時，工具會回答「伺服器忙碌」，稍後再呼叫即可。見[同時轉換](configuration.zh-TW.md#同時轉換)。
- PDF → 可編輯格式一定有損；PDF 內的表格以啟發式方式還原。
- 圖片會保留，但附加在抽取出的 Markdown 末尾，而非原本的位置。
- 不支援 `.xlsb`。
- LibreOffice 轉檔同樣不會讀取文件所連結的檔案或網址：以路徑或網址連結的圖片（HTML 的 `<img>`、DOCX/XLSX/PPTX 中標為 external 的 relationship、ODF 的 `xlink:href`、RTF 的 `INCLUDEPICTURE` 欄位）不會出現在輸出中，樣式表也不會被下載。每次轉檔都使用一個會封鎖不受信任文件連結、並把所有 HTTP/HTTPS/FTP 流量導向不存在之 proxy 的 LibreOffice 設定檔；ODF、OOXML 與 RTF 文件還會先移除指向外部的參照（依內容判斷，所以改名成 `.pptx` 的 ODP 仍當作 ODP 處理）。文件內嵌的圖片會保留。無法完整讀取以進行檢查的 zip 類文件會被拒絕，而不是照樣轉檔。
- Pandoc 轉檔不會讀取文件所指向的檔案或網址：LaTeX 的 `\input` / `\lstinputlisting` / `\includegraphics`，以及用路徑或 `http(s)` 網址引用的圖片都會被略過（圖片保留 alt 文字）；文件內嵌或以 `data:` URI 提供的圖片會保留。
- 撤銷不是即時的：啟用 [revocation feed](configuration.zh-TW.md#token-撤銷) 時，被撤銷的 token 會在一個輪詢間隔內失效（預設 30 秒）；未啟用時則要等到過期。
