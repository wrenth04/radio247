# Radio247

以 24/7 持續播送為目標的 NAS 音樂台：音檔搭配靜態圖片，背景逐首準備快取，連續播放並輸出本機 FLV 或 RTMP／RTMPS。提供簡易管理頁，可上傳素材、開始／停止、切歌、查看準備與推流狀態。

目前提供 **單一 `radio247` 容器，圖片快取＋音訊即時解碼**，包含 `Dockerfile`、`compose.yaml` 與完整的 [`.env.example`](.env.example)。圖片先編成 H.264／MP4 短 GOP 快取，播放時重用封包，不持續重新編碼圖片。音訊直接解碼原始 M4A／MP3／MPA／WAV，套用已分析的響度處理，再交給連續 AAC 編碼器；不再建立整首 `.pcm` 快取。

容器內由 `serve` 管理後台、播放與隔離 publisher 程序；不是分成 web／radio 兩個容器。使用 `RADIO247_CPUS`、`RADIO247_MEMORY_LIMIT` 設定整個容器的總資源上限。

## 使用 Docker Compose

需要 Linux 上的 Docker Engine 與 Docker Compose v2。映像包含 Python、FFmpeg／ffprobe、PyAV 及應用依賴，不需要在 NAS 主機安裝 Python。

`docker compose build` 與 `docker compose up --build -d` 都使用精簡的 `runtime` target；直接執行 `docker build` 也預設建置此 target。正式映像不包含 uv、開發依賴與測試檔案，uv 下載／建置快取透過 BuildKit cache mount 保留在建置端，不會打包進映像。建置需要啟用 BuildKit（現代 Docker／Compose 預設啟用）；建置端快取仍會占用主機磁碟，但不屬於正式映像大小。

Compose 的預設網路明確使用 `bridge` driver；不是 host network。管理介面仍透過 `.env` 的 `RADIO247_HOST_BIND`／`RADIO247_HOST_PORT` 發布。

首次啟動：

```sh
cp .env.example .env
mkdir -p ./media ./images ./cache ./data ./output ./runtime
# 若目錄擁有者不是 .env 的 UID/GID，先調整；以下為預設 1000:1000。
sudo chown 1000:1000 ./media ./images ./cache ./data ./output ./runtime
docker compose config --quiet
docker compose up --build -d
docker compose ps
```

若已存在 `.env`，請編輯原檔，不要直接覆蓋。首次建置需要網路下載公開映像、系統套件與 Python 依賴。

預設後台為 **http://127.0.0.1:8000**，帳號為 `admin`。第一次啟動自動產生隨機密碼，讀取方式：

```sh
docker compose exec radio247 cat /data/secrets/admin-password
```

這是你的登入憑證，不要貼到公開日誌。密碼與內部 token 存在主機 `./data/secrets`，重新啟動不會重新產生。

**預設目的地為 YouTube RTMPS**：`rtmps://a.rtmps.youtube.com/live2`。必須先在 `.env` 設定 `RADIO247_STREAM_KEY` 才能推流；缺少或無效的金鑰會明確顯示錯誤並重試，管理頁仍可使用，不會偷偷改成本機輸出。RTMPS 是加密的 RTMP，若需要其他端點可修改 `.env` 的 `RADIO247_INGEST_URL`。UI 不提供推流設定管理。

要先做本機驗證，請明確設定 `RADIO247_INGEST_URL=`（空值）。此時 UI 的 `output: local` 表示只在主機 `./output` 寫入 `radio-*.flv`，**沒有推送到 YouTube／RTMP**。沒有可播素材時輸出預設圖與靜音；有素材完整就緒後加入播放，不等全部曲庫分析完成。

### NAS 核心不支援 CPU／PIDs 限制

部分 NAS 核心不支援所需的 cgroup 功能，或未掛載對應的 controller，可能出現：

| 訊息 | 意義與處理 |
| --- | --- |
| `NanoCPUs can not be set, as your kernel does not support CPU CFS scheduler or the cgroup is not mounted` | CPU 配額設定使容器建立失敗。在 `.env` 設定 `RADIO247_CPUS=0` 停用 CPU 配額。 |
| `Your kernel does not support PIDs limit capabilities or the cgroup is not mounted. PIDs limit discarded.` | 警告：Docker 已忽略程序數量限制，不代表容器啟動失敗。可設定 `RADIO247_PIDS_LIMIT=-1` 明確停用此限制。 |

若兩種限制都不支援，修改既有 `.env`：

```dotenv
RADIO247_CPUS=0
RADIO247_PIDS_LIMIT=-1
```

**不要留空或只刪除設定**，否則 Compose 會套回預設的 `2.0` CPU 與 `128` 個程序限制。停用後容器不再有 CPU 配額與程序數量上限；記憶體限制仍維持原設定，需留意 NAS 整體負載。

使用已建好的映像重新建立容器，無須重新 build，也不需刪除資料：

```sh
docker compose up -d --no-build --force-recreate
docker compose ps
```

`Up` 表示容器正在執行，`healthy` 才表示健康檢查通過；若持續重啟或顯示 `unhealthy`，請查看 `docker compose logs --tail=100 radio247`，不要只依上述警告判斷服務狀態。

### NAS 區網存取

在 `.env` 指定 NAS 的區網 IP 與主機端連接埠：

```dotenv
RADIO247_HOST_BIND=192.168.1.10
RADIO247_HOST_PORT=8000
```

重新建立容器：

```sh
docker compose up -d
```

`RADIO247_HOST_BIND` 是主機發布位址；`RADIO247_BIND_HOST` 是容器內監聽位址，通常保持 `0.0.0.0`。不要將管理埠直接暴露至公網。管理頁使用 HTTP Basic 登入，跨不可信網路存取必須加 HTTPS；若使用反向代理，需保留正確 Host／Origin 並驗證控制請求的同源檢查。

## 放入音樂與圖片

可以從管理頁上傳 MP3／MPA／M4A／WAV 音檔及 PNG 圖片；M4A 已涵蓋 AAC 與 ALAC 素材的準備測試。檔案配對使用相同主檔名：

```text
media/
  song.mp3
images/
  song.png
```

音訊以實際可解碼性為準，不只檢查副檔名。只掃描素材目錄第一層；不跟隨素材 symlink。不指定圖片時使用預設圖，也可透過 `RADIO247_DEFAULT_IMAGE=/images/default.png` 指定。

歌曲圖片與預設圖都會等比例縮放、置中裁切以填滿輸出畫面，不拉伸、不額外補黑邊；比例不同時會裁掉上下或左右內容，小圖也會放大。請將重要文字／主體放在中央。原圖本身的黑框或透明區域不會自動去除；完全未提供圖片時仍使用黑底。

升級此圖片處理方式後，舊圖片快取會失效並自動重建，既有音訊分析會沿用；不需手動刪除快取。舊 MP4 檔案不會自動刪除，仍會占用快取配額。

### 預設圖片

把預設 PNG 放在主機 `./images/default.png`（也可透過管理頁上傳此檔名），在 `.env` 設定容器內路徑：

```dotenv
RADIO247_DEFAULT_IMAGE=/images/default.png
RADIO247_DEFAULT_IMAGE_POSITION=end
RADIO247_DEFAULT_IMAGE_SECONDS=30
```

執行 `docker compose up -d` 套用環境設定。歌曲沒有同名 PNG，或沒有可播歌曲時，會使用此圖；留空則使用黑圖。指定的預設圖若不存在或無法解碼，會回報準備錯誤，不會靜默當成設定成功。

設定預設圖後，可用 `RADIO247_DEFAULT_IMAGE_POSITION` 選擇每首歌曲（包含 M4A／MP3／MPA／WAV）的顯示時段：`start` 在前 N 秒顯示預設圖，之後切回歌曲圖片；`end`（預設）先顯示歌曲圖片，依分析出的歌曲長度，在剩下 N 秒時切成預設圖直到結束。音訊持續播放，不插入額外片頭、尾段或靜音。

`RADIO247_DEFAULT_IMAGE_SECONDS` 預設 30，可設為任意非負有限秒數（支援小數），`0` 停用定時顯示；未設定 `RADIO247_DEFAULT_IMAGE` 時也不啟用。短於設定秒數的歌曲全程顯示預設圖；自然換曲、循環重播或點播都依新一輪播放進度重新計算。手動切歌直接換曲，不強制補播尾段。切圖以輸出影格為單位（1 fps 時約 1 秒精度），從快取的關鍵影格開始，不需重新編碼或中斷音訊。管理頁現播圖片會跟隨實際輸出畫面。

若先前已設定 `RADIO247_TRACK_INTRO_SECONDS` 或 `RADIO247_TRACK_OUTRO_SECONDS`，請移除，改用 `RADIO247_DEFAULT_IMAGE_SECONDS` 並指定 `RADIO247_DEFAULT_IMAGE_POSITION`；舊名稱會明確報錯，避免設定被忽略。更新 env 後需重新建立容器，單純 restart 不會載入新值。

### ZIP 批次上傳

同一個上傳入口也接受 `.zip`，可一次放入多首音訊與 PNG。ZIP 內的子資料夾會攤平，音訊匯入 `./media`、圖片匯入 `./images`，同主檔名自動配對。例如 `album/song.m4a` 與 `covers/song.png` 會配成一組；副檔名轉為小寫，主檔名大小寫保持不變。

ZIP 內重複的目的檔名（不區分大小寫）會拒絕整包，避免攤平後互相覆蓋；與磁碟上既有素材同名時，則沿用單檔上傳的取代行為。隱藏檔、`__MACOSX` 及不支援的格式會略過，介面顯示匯入／略過數量；不會遞迴解開巢狀 ZIP。

只接受未加密的 Stored／Deflate ZIP，拒絕路徑穿越、絕對路徑、連結、空素材或損壞的素材項目。所有待匯入素材完成解壓與 CRC 檢查後才逐檔原子發佈，先圖片再音訊；磁碟故障若發生在發佈途中，可能已有部分檔案匯入，會回報錯誤而非成功。媒體是否有效仍由背景解碼／快取準備判定。

限制可在 `.env` 調整，不需修改 Compose：

| 變數 | 預設 | 用途 |
|---|---|---|
| `RADIO247_UPLOAD_MAX_BYTES` | 536870912（512 MiB） | ZIP 上傳大小及每個解壓素材的上限，也適用單檔上傳 |
| `RADIO247_ZIP_MAX_BYTES` | 1073741824（1 GiB） | ZIP 宣告的總解壓大小與實際素材解壓總量上限 |
| `RADIO247_ZIP_MAX_FILES` | 1000 | ZIP 項目數上限，包含資料夾與略過項目 |

上傳與解壓期間需要暫存磁碟空間；解壓在背景執行緒處理，不阻塞管理 API 的事件迴圈。匯入後仍須等待背景準備，不會直接把未準備的音檔送上直播。

### 音樂台介面

管理頁採深色、大封面與曲目優先的配置，桌面與手機皆可使用。主按鈕依狀態顯示「開始播送／停止播送」，旁邊可切換下一首；**停止不是暫停**。此頁控制伺服器播送，不在瀏覽器播放音訊，也不提供音量或拖曳進度控制。

「你的音樂」保留曲名搜尋與全部／可播放／準備中／需處理篩選。右上「匯入音樂」會展開頁面下方的匯入區，可選檔或拖放單一檔案，多個素材請打包為 ZIP。同名素材會被取代；上傳完成不代表已可播放，仍需等待背景準備。

首頁只顯示白話播放／連線狀態與必要錯誤。完整推流目的地、協定、準備狀態、原始錯誤與 JSON 收在頁面下方預設關閉的「進階診斷」；失敗曲目也能展開個別詳情。操作回覆會先顯示要求已送出，實際播放或停止仍以電台回報為準。

管理頁斷線、電台狀態過期及平台重連會分別提示，不將網頁失聯當成電台已停止。本機錄製也不會標示為平台直播成功。

### 目前播放圖片與選曲

下方「你的音樂」提供可點選的封面縮圖與播放按鈕。點選已 ready 的歌曲會淡出目前音訊並從該首開頭播放；停止狀態下會自動開始。點選現播歌曲可重新播放，播完後仍回到原有隨機播放流程。尚未準備完成或失敗的曲目不可點選；沒有專屬配圖的歌曲顯示預設圖片。曲目仍可選，但縮圖讀取失敗時會明確提示。

管理頁會依播放程序回報，顯示目前選用的 H.264 快取圖片預覽，而不是只顯示最新上傳圖或曲名對應的原始圖。圖片在實際 GOP 換圖後隨狀態輪詢更新；沒有曲目時可顯示保底圖，停止播放或狀態過期時隱藏預覽。預覽需登入才能讀取。

這是**本機播放圖片，不是 YouTube 回傳畫面**；斷線或平台緩衝時，不能用它判定觀眾已看到同一張圖。

「進階診斷」顯示設定的 RTMP／RTMPS 目的位置與 publisher 連線狀態。串流金鑰以 `<stream-key>` 遮蔽，URL 的帳密、query 與 fragment 不會送到瀏覽器；本機輸出模式會明確顯示「本機 FLV，不推送 RTMP」。目的位址存在不代表已連線，請同時看連線狀態。

### 直接放入素材

直接把素材放入 `compose.yaml` 同目錄下的資料夾：

```sh
cp /path/to/song.mp3 ./media/
cp /path/to/song.png ./images/
```

所有資料均使用 `./` 相對路徑 bind mount，不另外建立 Docker named volumes，也不提供 volume 名稱設定。

主機目錄必須允許 `RADIO247_UID:RADIO247_GID` 寫入；預設為 `1000:1000`。請在啟動前建立目錄並確認權限，避免 Docker 自動建立 root 擁有的資料夾。程式不會擅自遞迴修改 NAS 檔案權限。若更改 UID／GID，須重新建置映像，並確認既有資料權限。SQLite 的 `./data` 應放本機檔案系統，不使用 SMB／NFS。

準備工作一次分析一首，分析資料與圖片快取驗證後才原子發佈。第一首 ready 即可播放；只有一首時循環；其他曲目就緒後加入隨機待播池。壞檔或配圖失敗會顯示在後台，可按「重試失敗素材」。上傳同名檔案會在上傳完成後原子取代舊素材，不直接寫入正在讀取的檔案。

### 容器內目錄

| 主機目錄 | 容器目錄 | 用途 |
|---|---|---|
| 映像內建 | `/app` | 程式與 Python 虛擬環境，唯讀 |
| `./media` | `/media` | 原始音檔 |
| `./images` | `/images` | 原始圖片 |
| `./cache` | `/cache` | 小型音訊分析 JSON 與 H.264／MP4 圖片快取 |
| `./data` | `/data` | SQLite、私有憑證檔案及上傳暫存 |
| `./output` | `/output` | 有保留上限的本機 FLV |
| `./runtime` | `/run/radio247` | 播放心跳等執行狀態 |

若從舊名稱 `airadio` 升級，先停止舊 Compose 專案，避免同時啟動兩套推流；**不要加 `-v` 刪除資料 volume**：

```sh
docker compose -p airadio down --remove-orphans
```

若先前已使用 named volumes，請先備份並將資料搬到對應的 `./` 目錄、確認 UID/GID 權限後，再啟動新配置。既有 volume 不會自動搬移或刪除。

既有 `.env` 的 `AIRADIO_` 前綴需改成 `RADIO247_`，原設定值保留；偵測到舊前綴時程式會明確報錯，不靜默使用預設值。將執行狀態路徑更新為 `RADIO247_RUNTIME_DIR=/run/radio247`。`./media`、`./images`、`./cache`、`./data`、`./output`、`./runtime` 及其中資料不因更名而改變。

舊版分開的 CPU／RAM 限制改為 `RADIO247_CPUS`／`RADIO247_MEMORY_LIMIT`。`RADIO247_INTERNAL_API_URL` 留空，讓它使用容器內的 `127.0.0.1`，不要保留舊的 `http://web:8000`。自訂 API 客戶端需將控制標頭改為 `X-Radio247-Action: 1`。

```sh
docker compose up --build -d
```

## 推送 YouTube RTMPS

正式直播前可先用 `RADIO247_INGEST_URL=` 驗證本機輸出，再恢復下列預設值。使用者須自行準備有權直播的素材、YouTube 直播活動及串流金鑰。

在 `.env` 分別設定伺服器 URL 與 Stream Key/token。範本的金鑰為空，請自行填入平台提供的值：

```dotenv
RADIO247_INGEST_URL=rtmps://a.rtmps.youtube.com/live2
RADIO247_STREAM_KEY=
```

金鑰去除頭尾空白後必須至少 12 個字元。`INGEST_URL` 只放 ingest endpoint，**不要把金鑰接在 URL 中**。程式啟動時直接讀取環境變數，不自行載入 `.env`；Compose 透過 `env_file` 注入設定，直接執行 Python 時則需自行設定 process environment。

**舊設定遷移：`RADIO247_STREAM_KEY_FILE` 已移除，不再讀取推流金鑰檔案，也不提供 fallback。** 請自行將舊檔案中的金鑰填入 `RADIO247_STREAM_KEY` 並移除舊變數。程式不會搬移或刪除舊金鑰檔案。管理密碼與內部 token 仍使用原本的私有檔案，不受影響。

`.env` 現在包含推流秘密，請限制檔案權限（例如 `chmod 600 .env`），不要提交、貼出或公開。設定顯示及公開狀態會遮蔽金鑰，但環境變數不是加密儲存；具容器管理權限者仍可能讀取。避免公開 `docker inspect` 或會展開 env 值的 Compose 設定輸出。

修改 `.env` 後需重新建立容器以載入新值，**單純 `docker compose restart` 不會更新環境變數**：

```sh
docker compose up -d --force-recreate
docker compose logs --tail=100 radio247
```

若後台先前已按停止，需要再按開始。開始／停止狀態存於 SQLite；`RADIO247_AUTOSTART` 只決定新資料庫的初始值。

推流程序與播放程序隔離。連線拒絕、寫入無進度或佇列滿時會回收 publisher，退避後建立新連線；恢復時等待當下的關鍵影格，不囤積整段斷線期間的節目。後台的 `streaming` 表示本機已成功寫出資料，不代表 YouTube 觀眾已看到。

**ADSL 斷網期間不可能持續送達 YouTube。** 長斷線可能讓 YouTube 結束直播活動；本程式不管理 YouTube OAuth／活動建立，也不控制路由器重撥。

## 所有運作參數使用 env

[`.env.example`](.env.example) 列出目前全部設定；[`settings.py`](src/radio247/settings.py) 定義型別及有效範圍。Compose 使用 `env_file: .env` 將設定直接傳入單一 `radio247` 服務，不再逐項列出應用參數。埠號、掛載與資源限制仍由 Compose 對同一份 `.env` 進行插值；請統一編輯 `.env`，首次啟動前需先從範本建立此檔案。

| 類別 | 主要變數與單位 |
|---|---|
| 推流 | `RADIO247_INGEST_URL`：RTMP／RTMPS endpoint，空值為本機輸出；`RADIO247_STREAM_KEY`：Stream Key/token，至少 12 字元 |
| 音訊 | `RADIO247_SAMPLE_RATE`：44100／48000；`RADIO247_CHANNELS`：1／2；`RADIO247_AUDIO_BITRATE`：bps |
| 響度 | `RADIO247_TARGET_LUFS`、`RADIO247_TARGET_PEAK`：dBTP 目標；`RADIO247_TARGET_LRA`；`RADIO247_FADE_MS`：毫秒 |
| 視訊 | `RADIO247_WIDTH`／`HEIGHT`：偶數像素；`RADIO247_FPS`：1／24／25／30／50／60；`RADIO247_GOP_SECONDS`：秒 |
| 編碼 | `RADIO247_VIDEO_BITRATE`：bps；`RADIO247_VIDEO_BUFFER_SIZE`：bits；`RADIO247_VIDEO_PRESET`；`RADIO247_THREADS` |
| 準備 | `RADIO247_SCAN_INTERVAL`、`RADIO247_FILE_STABLE_SECONDS`、`RADIO247_PREPARE_TIMEOUT`、`RADIO247_PROBE_TIMEOUT`：秒 |
| 限額 | `RADIO247_CACHE_MAX_BYTES`、`RADIO247_UPLOAD_MAX_BYTES`；`RADIO247_IMAGE_MAX_PIXELS` |
| 恢復 | `RADIO247_CONNECT_TIMEOUT`、`RADIO247_STALL_TIMEOUT`、`RADIO247_RETRY_INITIAL`／`MAX`／`RESET_SECONDS`：秒；`RADIO247_RETRY_JITTER_RATIO`：0～1 |
| 有界緩衝 | `RADIO247_QUEUE_SECONDS`：封包佇列容量依 fps／AAC frame rate 換算，滿時重建連線 |
| 即時音訊 | `RADIO247_DECODE_BUFFER_SECONDS`：記憶體預讀秒數，預設 2；`RADIO247_DECODE_TIMEOUT`：解碼器無進度期限，預設 15 秒 |
| 控制與 UI | `RADIO247_POLL_INTERVAL`、`RADIO247_REQUEST_TIMEOUT`、`RADIO247_UI_POLL_SECONDS`：秒 |
| 容器內監督 | `RADIO247_SERVICE_START_TIMEOUT`、`RADIO247_SERVICE_STALL_TIMEOUT`：秒 |
| 本機錄製 | `RADIO247_LOCAL_SEGMENT_SECONDS`：每段秒數；`RADIO247_LOCAL_KEEP_SEGMENTS`：保留段數 |
| 部署 | `RADIO247_HOST_BIND`／`HOST_PORT`、`RADIO247_UID`／`GID`、CPU／RAM／PID／日誌／healthcheck 限制；資料固定掛載同目錄的 `./` 資料夾 |

音視訊初始設定為 **44.1 kHz／雙聲道／192 kbps AAC、1080p30／5 Mbps H.264、2 秒 GOP**。這是本程式設定，不是對 YouTube 官方建議值的宣稱。請依目標 NAS、上傳頻寬與平台健康狀態調整。

### 1 fps 與低流量設定

可設定 `RADIO247_FPS=1`。視訊每秒一幀，音訊仍依樣本時鐘連續輸出，不會變成每秒才送一次音訊。預設 2 秒 GOP 在此時是 2 幀，換圖仍在 GOP 邊界進行。

**只降 FPS 不會讓固定碼率流量按比例下降**；目前 H.264 使用 CBR／VBV，省流量需一起降低視訊 bitrate。以下是可嘗試的低流量設定，並非 YouTube 官方建議值：

```dotenv
RADIO247_FPS=1
RADIO247_GOP_SECONDS=2
RADIO247_VIDEO_BITRATE=500000
RADIO247_VIDEO_BUFFER_SIZE=1000000
RADIO247_AUDIO_BITRATE=192000
```

目標視訊 500 kbps＋音訊 192 kbps 的理論資料量約 7.5 GB／日，實際仍受封裝、編碼、TLS 與重傳影響。複雜圖片可能失去細節；YouTube 對 1 fps／低碼率的 ingest 健康與觀看端表現需實播確認，不保證平台接受或穩定。遇到平台警告可恢復一般幀率並依畫質需求調整 bitrate。

舊版程式只允許 24／25／30／50／60，會出現 `fps literal_error`；必須更新部署程式後才支援 1 fps，單改 `.env` 或重新整理網頁不會移除舊版限制。

變更 `.env` 後執行 `docker compose up -d`；建置參數另加 `--build`。`docker compose restart` 不會重新載入新的容器環境。音訊格式、視訊設定與處理版本參與快取識別；格式變更時先清除舊 ready 狀態，重新準備相容素材。

程式直接以 Python 啟動時，讀取的是程序環境變數，不會自動讀 `.env`。路徑設定須為絕對路徑；容器內路徑保持在對應掛載內。AAC frame size 由編碼器決定，不能任意改成不相容值。非法設定會明確報錯，不靜默回退。

## 更新程式與介面

修改原始碼或取得新版後，需要重建映像並重新建立容器；只重新整理瀏覽器或 `docker compose restart` 不會更新映像內程式：

```sh
docker compose up --build -d
```

完成後重新整理管理頁。現播圖片需要新版前端、web API 與 radio 回報程序共同運作；停止或播放狀態過期時不顯示圖片，也不以圖片預覽宣稱 YouTube 已收到直播。

## 為什麼不再儲存 PCM 快取

舊版 `cache/*.pcm` 是 44.1 kHz、雙聲道、32-bit float 的未壓縮音訊：每秒 352800 bytes，每小時約 **1.27 GB**。新版不再產生這些檔案，只保留響度分析 JSON、原始音檔與圖片 MP4 快取。

播放時由 FFmpeg 即時解碼與套用響度／limiter；PCM 只短暫留在有上限的記憶體佇列，再由 PyAV 的同一個 AAC 編碼器持續輸出。預讀下一首可降低換歌時的啟動空檔，曲尾依實際解碼 EOF 做淡出，不用檔案標示時長裁掉音訊。解碼不足時會補靜音並記錄 `audio_underruns`；NAS 的 CPU／磁碟仍須有足夠餘裕，不能保證任何設備皆無音訊空檔。

舊資料庫會保留並重新準備分析資料，不再讀取舊 PCM。**既有 `.pcm` 不會自動刪除**；要回收空間，先停止服務並確認新版已不再使用，再手動清理 `./cache` 內舊的 `.pcm`，不要刪除原始音檔。舊檔仍占用 cache 配額，若因此無法準備，需先清理。

## 觀察與停止

```sh
docker compose ps
docker compose logs --tail=100 radio247
docker compose cp radio247:/output ./recordings
docker compose stop
```

本機 FLV 預設每 300 秒換檔，最多保留 3 段。只清理專用 output 目錄中符合 `radio-*.flv` 的舊檔，請勿把其他重要資料使用這個檔名前綴。段檔切換會重建本機 publisher，不承諾錄製檔之間無縫；YouTube 模式不做這種定時換檔。

`restart: unless-stopped` 處理容器程序退出；Docker healthcheck 本身不會重啟 unhealthy 容器。容器內 supervisor 會監看子程序退出及後台／播放心跳，超過設定期限便回收子程序並退出，交給 Docker 重啟。短暫控制 API 失聯時，radio 保留最近取得的播放快照。

## 本機 venv 開發與測試

不需要 Docker 即可測試。主機需要 Python 3.12 以上、[uv](https://docs.astral.sh/uv/)、含 libx264／loudnorm／alimiter 的 FFmpeg 與 ffprobe：

```sh
uv sync --frozen --dev
.venv/bin/python -m pytest -q
.venv/bin/python -m ruff check src tests
.venv/bin/python -m ruff format --check src tests
.venv/bin/python -m mypy src tests
```

若舊 `.venv` 出現模組缺失或無效 wheel，依同一鎖檔重新安裝：

```sh
uv sync --frozen --dev --reinstall --no-cache
```

測試包括媒體重封裝、env 驗證、曲庫準備／配額、控制與登入、publisher 卡死回收、本機 RTMP 接收，以及真的啟動 web／radio 子程序後解碼輸出的端到端測試。只使用暫存素材與 loopback 測試端點，不需要 YouTube 金鑰，不會向 YouTube 推流。

UI 改版另有 Playwright 瀏覽器測試，涵蓋桌面／手機、縮圖點播、鍵盤焦點、搜尋篩選、匯入、失聯及停止確認。包含一條真實暫存服務的匯入與播送流程，不使用既有 `.env` 或素材：

```sh
uv sync --frozen --dev --group ui
.venv/bin/python -m playwright install chromium
.venv/bin/python -m pytest -q tests/test_ui.py tests/test_web.py tests/test_runtime.py
```

主機需具備 Chromium 所需系統函式庫。未安裝可選的 `ui` 依賴時，UI 測試模組會被略過；若已安裝 Playwright 但缺少瀏覽器，測試會失敗並提示安裝。略過不代表 UI 驗收通過。

測試使用 `.venv` 即可。若需要在映像中執行測試，可另行建置 Dockerfile 的 test target；Compose 本身只有一個服務：

```sh
docker build --target test -t radio247:test .
docker run --rm radio247:test
```

統一入口為 `python -m radio247 serve`，會管理 web／radio 子程序；`python -m radio247 health` 檢查兩者健康。本機直接啟動時，需自行將所有 storage／secret 路徑設為可寫目錄。內部 API 預設使用 `127.0.0.1` 與 `RADIO247_WEB_PORT`。

## 資源成本與尚未完成的部分

- 靜態圖 GOP 快取主要省 CPU，不代表固定碼率上傳流量下降。5 Mbps 視訊加 192 kbps 音訊約 **56.1 GB／日**，不含協定開銷與重傳。
- 音訊改成即時解碼，不再儲存整首 PCM；圖片 MP4 與分析 JSON 仍受 cache 配額限制。目前不自動 LRU 淘汰，需停止服務後由管理者清理不再需要的快取。
- 目前提供音訊即時解碼、圖片快取、ZIP／單檔上傳、同名配圖及控制頁。進階曲庫編輯、資源壓力下的分析暫停與完整快取管理仍在後續範圍。
- 音訊有離線響度分析、播放時響度處理、輸出峰值保護與短淡變，但不能修復來源已削波的失真，也未完成真實音樂 AAC true-peak 與長時間量測。
- NAS CPU／I/O／上傳及 amd64／arm64 實機能力仍需驗證；不能以開發機通過測試代替 NAS／YouTube 驗收。
- 本次環境沒有 Docker Engine，已驗證 Compose 解析、env 接線、Python 套件建置及 venv 服務端到端流程；**Docker 映像建置／容器實啟與 YouTube RTMPS 實播仍待目標環境驗證**。

## 結構

```text
Dockerfile / compose.yaml / .env.example
src/radio247/
  __main__.py       服務與健康檢查入口
  settings.py       集中 env 解析與驗證
  media.py          媒體準備、編碼及封裝
  audio.py          原始音訊串流解碼與無進度期限
  playout.py        有界 PCM 供料、選曲與淡變
  library.py        SQLite、增量準備與快取
  web.py            管理／控制 API
  uploads.py        ZIP 驗證、分類與匯入
  radio.py          持續播放與控制同步
  publisher.py      隔離推流、watchdog、重連
  static/index.html 管理頁
tests/             單元與本機整合測試
```

參考：[YouTube 編碼設定](https://support.google.com/youtube/answer/2853702?hl=en)、[FFmpeg filters](https://ffmpeg.org/ffmpeg-filters.html)、[PyAV](https://pyav.org/docs/stable/)、[Docker Compose](https://docs.docker.com/reference/compose-file/services/)。
