# 設定

[English](configuration.md) · [README](../README.zh-TW.md)

所有設定都在 [`config/config.yaml`](../config/config.yaml)，以 `${VAR:-default}` 讀取環境變數。[`.env.example`](../.env.example) 列出了這些變數：

| 變數 | 預設值 | 說明 |
|---|---|---|
| `SERVER_HOST` / `SERVER_PORT` | `0.0.0.0` / `5055` | 綁定位址。 |
| `MCP_GATE_URL` | `http://localhost:4568` | 簽發 token 的 MCP Gate（`auth.issuer`）。 |
| `MORPHYARD_BASE_URL` | `http://127.0.0.1:5055` | 本伺服器對外的位址。token 的 audience 為 `<MORPHYARD_BASE_URL>/mcp`，必須等於在 MCP Gate 註冊的服務；也會出現在 protected-resource metadata 中。 |
| `AUTH_ENABLED` | `true` | 只有本機實驗時才設為 `false`。 |
| `MAX_INPUT_MB` | `50` | 單檔上限；同時提高 MCP 傳輸層的 body 上限（×1.5）。 |
| `ENGINE_TIMEOUT` | 各引擎預設 | 每次引擎呼叫的秒數上限，套用到所有引擎。未設定時各引擎維持自己的上限：LibreOffice 180、Pandoc 120、OCR 300。 |
| `MORPHYARD_MAX_CONCURRENT_CONVERSIONS` | CPU 數，最多 8 | 同時執行的轉換數，所有引擎合計。見[同時轉換](#同時轉換)。 |
| `MORPHYARD_MAX_QUEUED_CONVERSIONS` | 上限的 4 倍 | 最多還能有幾個請求排隊等空位；`0` 表示所有空位都被占用時立即拒絕。 |
| `MORPHYARD_QUEUE_TIMEOUT` | `30` | 請求等待空位的秒數上限，超過即以「忙碌」拒絕。 |
| `MORPHYARD_DATA_DIR` | `./data` | 暫存檔案與自動產生的連結簽章金鑰。見[暫存檔案與下載連結](#暫存檔案與下載連結)。 |
| `MORPHYARD_PUBLIC_URL` | `MORPHYARD_BASE_URL` | 下載連結指向的位址。 |
| `MORPHYARD_SIGNING_KEY` | 自動產生 | 簽署下載連結的金鑰。 |
| `MORPHYARD_FILE_TTL_SEC` | `3600` | 檔案保留的秒數（最多 86400）。 |
| `MORPHYARD_STORE_MAX_MB` / `MORPHYARD_STORE_OWNER_MAX_MB` | `2048` / `512` | 暫存上限：整體、每人。 |
| `MORPHYARD_RESULT_DELIVERY` / `MORPHYARD_INLINE_MAX_MB` | `auto` / `10` | 工具回傳檔案的方式，見[回傳方式](#回傳方式)。 |
| `LOG_LEVEL` | `INFO` | |

YAML 中的 `auth.required_scopes` 可要求每個 token 都必須帶有某個 scope（例如 `mcp:tools:invoke`）；`auth.audience` 則可在註冊的 resource URI 不是 `<base_url>/mcp` 的少數情況下覆寫推導值。

## 同時轉換

每個轉換在整個執行期間（多段路徑的每一段都算在內）占用一個空位，不論由哪個引擎處理：LibreOffice、Pandoc、Tesseract、Poppler 或內建引擎。所有引擎共用一個上限，而不是各自一個，因為它們都在搶同樣的 CPU 與記憶體，而且一個請求會依序使用它的引擎；一個空位同時最多只有一個引擎程序。`list_supported_conversions` 與 `inspect_document` 不占空位。

請求遇到空位全滿時，會進入先進先出的佇列等待。若已有 `MORPHYARD_MAX_QUEUED_CONVERSIONS` 個請求在等，或等待超過 `MORPHYARD_QUEUE_TIMEOUT` 秒，就會被拒絕。拒絕是一個 MCP 工具錯誤，說明伺服器忙碌、尚未開始任何轉換，並請在幾秒後以相同參數重試（若經由 HTTP 則是 `503` 加上 `Retry-After` header）。`GET /health` 的 `conversions` 顯示目前狀態：`running`、`queued` 與三個上限。

預設上限是此程序可用的 CPU 數，最多 8，因為 LibreOffice 與 Tesseract 每個都要數百 MB，記憶體通常比 CPU 先用完。記憶體充足的大機器可以調高，小機器則調低。每個排隊中的請求都把上傳的檔案留在記憶體中（最多 `MAX_INPUT_MB`），佇列長度限制的就是這部分。上限以伺服器程序為單位：有多個 worker 或副本時，各自有自己的上限。

## 暫存檔案與下載連結

Morphyard 會把每個結果檔案在伺服器上保留一小段時間，並發出有簽章的下載連結，讓大型結果不必以 base64 塞進模型的上下文，下一個工具也能用 `file_id` 取用它，不必再傳一次檔案內容。

連結的樣子是 `<MORPHYARD_PUBLIC_URL>/files/<檔案 id>/<檔名>?exp=<unix 時間>&sig=<簽章>`。**任何拿到連結的人，在到期前都能下載**，和 S3 預簽網址一樣：只有擁有者的工具呼叫會拿到連結，擁有者檢查就發生在那裡。連結在 `exp` 到期或檔案被刪除時失效，以先到者為準。錯誤、過期或被竄改的連結，以及已不存在的檔案，都回同樣的 `404`，因此連結不會透露檔案是否曾經存在。下載一律以附件提供，並帶 `X-Content-Type-Options: nosniff`、`Cache-Control: private, no-store`、`Referrer-Policy: no-referrer` 與 `Content-Security-Policy: sandbox`，HTML 結果不會在本伺服器上當成網頁執行。查詢字串（簽章）不會寫進日誌。

| 變數／設定 | 預設值 | 說明 |
|---|---|---|
| `MORPHYARD_DATA_DIR`（`files.data_dir`） | `./data` | 暫存區在 `<data_dir>/store`，自動產生的金鑰在 `<data_dir>/.signing_key`。Docker 映像在 `/app/data` 宣告了 volume。 |
| `MORPHYARD_PUBLIC_URL`（`files.public_url`） | `MORPHYARD_BASE_URL` | 使用者的瀏覽器與 agent 連到本伺服器的位址。MCP 位址是內部位址時（reverse proxy 後面或區網名稱）請設定。若它是 loopback 位址而伺服器監聽所有介面，啟動時會警告。 |
| `MORPHYARD_SIGNING_KEY`（`files.signing_key`） | 自動產生 | 至少 32 個字元（`openssl rand -hex 32`）。未設定時，第一次啟動會產生 32 個隨機位元組存到 `<data_dir>/.signing_key`（權限 600）。YAML 中必須維持 `${ENV_VAR}` 參照；寫死的值會拒絕啟動。金鑰檔讀不出來或太短也會拒絕啟動，絕不會悄悄換掉。 |
| `MORPHYARD_FILE_TTL_SEC`（`files.ttl_sec`） | `3600` | 檔案保留多久，最多 86400（24 小時）。工具可以要求更短，不能更長。 |
| `MORPHYARD_STORE_MAX_MB`（`files.max_total_mb`） | `2048` | 整個暫存區。 |
| `MORPHYARD_STORE_OWNER_MAX_MB`（`files.max_per_owner_mb`） | `512` | 每人（token 的 `sub`；關閉驗證時所有人算同一人）。 |
| `files.sweep_interval_sec` | `60` | 每隔這麼久、以及啟動時，刪除過期檔案。 |
| `MORPHYARD_RESULT_DELIVERY`（`files.result_delivery`） | `auto` | 工具回傳檔案的方式：`auto`、`inline`、`both` 或 `link`，見[回傳方式](#回傳方式)。 |
| `MORPHYARD_INLINE_MAX_MB`（`files.inline_max_mb`） | `10` | `auto` 的門檻。 |

**暫存區滿了**時，新檔案會被拒絕，錯誤為 `storage_full` 並說明何時可重試；會先清掉過期檔案，但絕不刪除尚未過期的檔案來騰出空間，因此已發出的連結在標示的到期時間前都有效。每個檔案放在 `<store>/<擁有者的 sha256>/<檔案 id>/`（磁碟上沒有原始身分），先寫到暫存名稱再改名，當機也不會留下半個檔案。

**重新啟動**會保留未過期的檔案與它們的連結。請把金鑰（`.signing_key` 或 `MORPHYARD_SIGNING_KEY`）和暫存區一起保留：若有檔案時金鑰遺失或變更，這些連結已無法驗證，啟動時會刪除這些檔案並記錄警告。只遺失暫存區、金鑰還在，則只會少了檔案。已存檔案的索引放在記憶體中，所以一個暫存區屬於一個伺服器程序。

`GET /health` 的 `store` 顯示暫存區：`files`、`bytes`、`owners` 與上限——不含檔名或 id。

### 回傳方式

會產生檔案的工具，會把每個檔案放進暫存區（屬於呼叫者：token 的 `sub`；未啟用認證時為 `anonymous`），並回覆：

1. **文字：** 工具原本的摘要，接著每個檔案一行，列出檔名、大小、`file_id` 與下載網址，以及連結的到期時間。網址放在文字裡，是因為有些 client（包括 Claude Code）會忽略 `resource_link`。
2. **內嵌檔案：** 以 MCP embedded resource（base64）回傳，和 1.3 送出的區塊完全相同——是否附上依模式而定。
3. **每個檔案一個 `resource_link`：** `uri`（簽章網址）、`name`、`mimeType`、`size`，說明欄寫著到期時間與 `file_id`。

| 模式 | 內嵌檔案 | 連結 + `file_id` | 適用情況 |
|---|---|---|---|
| `auto`（預設） | 檔案不超過 `MORPHYARD_INLINE_MAX_MB`（10 MB）時附上 | 一律提供 | 會把內嵌檔案轉成 artifact（對話中的下載）的 host，日常大小的結果照常運作；大型結果不再塞滿模型的上下文：只回傳連結，文字也會說明檔案沒有附上。 |
| `inline` | 一律附上 | 不提供（不存任何檔案） | 與 1.3 完全相同，給完全無法使用連結的 host。結果不會保存，所以無法用 `file_id` 交給下一個工具；不論多大，每個結果都會經過上下文。 |
| `both` | 一律附上 | 一律提供 | 需要內嵌檔案、也想要連結的 host。大型結果佔用的上下文和 1.3 一樣多。 |
| `link` | 從不附上 | 一律提供 | 會讀連結的 client（或用瀏覽器下載的使用者）：回覆最小。只看得懂內嵌檔案的 host 就不會顯示下載。 |

`MORPHYARD_INLINE_MAX_MB` 以單一檔案計算：`pdf_split` 的結果可以小的部分內嵌、大的部分只給連結。base64 檔案在回覆中約佔原檔 1.33 倍，因此這個門檻是在「需要內嵌檔案的 host」與「模型上下文」之間取捨。

沒有檔案的回答（`inspect_document`、`extract_text`、`pdf_metadata`、`list_supported_conversions`，或「這個 PDF 沒有加密」）在任何模式下都不變。

暫存區放不下某個結果時，工具會回傳錯誤，說明是哪個上限、何時可以重試，而且這個結果的任何部分都不會留下（`pdf_split` 要嘛整組存好，要嘛完全不存）。結果本身就超過每人或整體上限時，會直接告知永遠放不下。

### 用 file_id 串接工具

每個接收檔案的工具都可以用 `file_id` 取代 `file_content`（`pdf_merge` 的項目也可以用 `{file_id}` 取代 `{file_content, file_name}`）。檔案直接從暫存區讀取，因此多步驟的工作不會把結果再送回模型：

```python
r1 = await client.call_tool("convert_document", {"file_content": md, "file_name": "report.md",
                                                 "target_format": "pdf", "output_name": "report"})
# r1 的文字："- report.pdf (20.8 KB, file_id f_o2UZ…): http://…/files/f_o2UZ…/report.pdf?exp=…&sig=…"
r2 = await client.call_tool("pdf_watermark", {"text": "DRAFT", "file_id": "f_o2UZ…"})
r3 = await client.call_tool("pdf_compress", {"file_id": "<r2 的 file_id>"})
```

- `file_content` 與 `file_id` 擇一，不能同時給。空的 `file_id` 視為沒有給（注入上傳檔的 host 會把放參照的參數清空）。
- `file_id` 只有產生它的那個人能用，而且只到到期為止。不存在、已過期或屬於別人的 id 都得到同樣的回答，不會透露檔案是否存在。未啟用認證時，所有人都是同一個人（`anonymous`）。
- 給 `file_id` 時仍可另給 `file_name` / `mime_type`；沒給就用存檔時的名稱與類型。一般的 50 MB 輸入上限照樣適用。

## Token 撤銷

Morphyard 以離線方式驗證 MCP Gate 的 token，因此預設情況下，在 MCP Gate 撤銷的 token 在這裡會一直有效到過期為止——而 personal access token 最長可活一年。開啟 **revocation feed** 後，Morphyard 會在背景輪詢 MCP Gate 的 `POST /oauth/revoked`（每 `poll_interval_sec` 秒，帶 jitter 與錯誤退避），在記憶體保存「已撤銷但尚未過期」的 `jti`，並對 `/mcp` 上的這些 token 回應 401 `invalid_token`。撤銷約在一個輪詢間隔內生效。

| 變數 / 設定 | 預設 | 說明 |
|---|---|---|
| `MORPHYARD_REVOCATION_ENABLED`（`auth.revocation.enabled`） | `false` | 開啟 feed。關閉 = 不輪詢。 |
| `MORPHYARD_REVOCATION_CLIENT_ID` | — | 在 MCP Gate 註冊的 resource-server client（見下方）。 |
| `MORPHYARD_REVOCATION_CLIENT_SECRET` | — | 它的 secret。在 YAML 中**必須**維持 `${ENV_VAR}` 參照；寫成字面值會拒絕啟動。 |
| `MORPHYARD_REVOCATION_POLL_INTERVAL`（`poll_interval_sec`） | `30` | 輪詢間隔秒數（5–3600）。 |
| `MORPHYARD_REVOCATION_FAIL_MODE`（`fail_mode`） | `open` | MCP Gate 連不上時：`open` 繼續接受 token 並大聲記錄；`closed` 在連續 `stale_after_intervals` × `poll_interval_sec`（YAML，預設 4）沒有成功輪詢後拒絕所有 token，啟動後第一次輪詢成功前也一樣。 |

第一次輪詢抓完整清單；之後只問上次以來的撤銷（重疊 30 秒），每小時再完整抓一次。token 本來就會過期之後，項目即被移除。啟動時會記錄 feed 是否啟用，`GET /health` 的 `revocation` 會顯示 `last_success`、`revoked_count`、`consecutive_failures`、`last_error`、`stale`；feed 過期時 `status` 會變成 `degraded`。

**設定步驟**（每個部署做一次）：

1. **在 MCP Gate 註冊 confidential client。** 主控台 → *OAuth Clients* → *Register trusted client*。名稱例如 `Morphyard revocation feed`；*Redirect URIs* 留空；*Grant types* 只選 `client_credentials`；*Token endpoint authentication*：**Client secret (POST body)**（HTTP Basic 也可以，MCP Gate 兩者都接受）；*Resource server for*：**Morphyard**。註冊後複製 `client_id`（`mcpc_…`）與 client secret——secret 只顯示一次。
2. **綁定到 Morphyard**——若步驟 1 已選 *Resource server for* 就已完成。否則在該 client 那一列點伺服器圖示（*Resource server…*），選 Morphyard 服務後 *Save*；或以主控台管理員呼叫 `PUT /api/oauth/clients/<client_id>/resource-server`，body 為 `{"service_id": "<Morphyard service id>"}`。該列會顯示 *Resource server for Morphyard*。feed 只列出 audience 等於該服務 audience 的 token，所以它必須等於 `<MORPHYARD_BASE_URL>/mcp`。
3. **把憑證交給 Morphyard**（環境變數或 `.env`），然後重啟：
   ```bash
   MORPHYARD_REVOCATION_ENABLED=true
   MORPHYARD_REVOCATION_CLIENT_ID=mcpc_...
   MORPHYARD_REVOCATION_CLIENT_SECRET=...
   ```
4. **確認。** 啟動 log 會出現 `MCP Gate revocation feed: ENABLED …`；`curl -s localhost:5055/health` 會看到最近的 `revocation.last_success`。在 MCP Gate 撤銷一個測試 token，`poll_interval_sec` 內用它呼叫 `/mcp` 會得到 401。`last_error` 出現 `HTTP 401: invalid_client` 表示憑證錯誤；`HTTP 403: unauthorized_client` 表示 client 尚未綁定伺服器。

## 改名的變數

Morphyard 在 1.2.2 之前叫 AnyDoc，MCP Gate 以前叫 MCP Center。舊的變數名稱不再讀取，升級時請在環境變數或 `.env` 中改名：

| 1.2.2 之前 | 現在 |
|---|---|
| `MCP_CENTER_URL` | `MCP_GATE_URL` |
| `ANYDOC_BASE_URL` | `MORPHYARD_BASE_URL` |
| `ANYDOC_REVOCATION_ENABLED` | `MORPHYARD_REVOCATION_ENABLED` |
| `ANYDOC_REVOCATION_CLIENT_ID` | `MORPHYARD_REVOCATION_CLIENT_ID` |
| `ANYDOC_REVOCATION_CLIENT_SECRET` | `MORPHYARD_REVOCATION_CLIENT_SECRET` |
| `ANYDOC_REVOCATION_POLL_INTERVAL` | `MORPHYARD_REVOCATION_POLL_INTERVAL` |
| `ANYDOC_REVOCATION_FAIL_MODE` | `MORPHYARD_REVOCATION_FAIL_MODE` |
