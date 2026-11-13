# Radio247

以 24/7 持續播送為目標的 NAS 音樂台：音檔搭配靜態圖片，連續播放並輸出本機 FLV 或 RTMP／RTMPS（預設 YouTube）。提供管理頁，可上傳素材、開始／停止、切歌、點播、刪除曲目、調整預設圖顯示方式，並查看準備與推流狀態。

## 運作方式

- **單一 `radio247` 容器**：由 `serve` 管理後台、播放與隔離的 publisher 程序，使用 `RADIO247_CPUS`、`RADIO247_MEMORY_LIMIT` 設定整個容器的總資源上限。
- **圖片快取**：每張 PNG 或 MP4 先編成 H.264／MP4 封閉 GOP 快取（MP4 依影片長度，四捨五入到 GOP 整數倍，上限 `RADIO247_LOGO_MAX_SECONDS`，預設 30 秒，循環播放、不含聲音），播放時直接重用封包，不持續重新編碼圖片。
- **音訊即時解碼**：直接解碼 M4A／MP3／MPA／WAV，套用已分析的響度處理，再交給連續 AAC 編碼器；不儲存整首 PCM。快取只保留響度分析 JSON 與圖片 MP4。
- **背景準備**：一次分析一首，驗證後才原子發佈。第一首 ready 即可播放，只有一首時循環；其他曲目就緒後加入隨機待播池。壞檔或配圖失敗會顯示在後台，可按「重試失敗素材」。
- **推流恢復**：publisher 與播放程序隔離；連線拒絕、寫入無進度或佇列滿時回收 publisher，退避後重連，恢復時等待當下的關鍵影格，不囤積斷線期間的節目。

## 使用 Docker Compose

需要 Linux 上的 Docker Engine 與 Docker Compose v2（啟用 BuildKit）。映像已含 Python、FFmpeg／ffprobe、PyAV 與應用依賴。`docker compose build` 使用精簡的 `runtime` target，不含 uv、開發依賴與測試檔案。Compose 使用 `bridge` 網路（非 host network）。

首次啟動：

```sh
cp .env.example .env
mkdir -p ./media ./images ./cache ./data ./output ./runtime
# 目錄擁有者須與 .env 的 UID/GID 一致（預設 1000:1000）
sudo chown 1000:1000 ./media ./images ./cache ./data ./output ./runtime
docker compose config --quiet
docker compose up --build -d
docker compose ps
```

管理頁預設為 **http://127.0.0.1:8000**，帳號 `admin`。首次啟動自動產生隨機密碼，保存在 `./data/secrets`，重啟不會重新產生：

```sh
docker compose exec radio247 cat /data/secrets/admin-password
```

### 推流目的地

預設目的地為 YouTube RTMPS，須在 `.env` 設定 Stream Key 才能推流：

```dotenv
RADIO247_INGEST_URL=rtmps://a.rtmps.youtube.com/live2
RADIO247_STREAM_KEY=
```

- 金鑰去除頭尾空白後至少 12 字元；`INGEST_URL` 只放 endpoint，不要把金鑰接在 URL 中。
- 缺少或無效的金鑰會明確顯示錯誤並重試，管理頁仍可使用，不會偷偷改成本機輸出。
- 將 `RADIO247_INGEST_URL=` 設為空值即為**本機輸出**：只在 `./output` 寫入 `radio-*.flv`，不推送到任何平台。建議正式直播前先以此驗證。
- `.env` 含推流秘密，請 `chmod 600 .env`，不要提交或公開；避免公開 `docker inspect` 等會展開 env 的輸出。
- 使用者須自行準備有權直播的素材、YouTube 直播活動及串流金鑰。本程式不管理 YouTube OAuth／活動建立，也不控制路由器重撥；斷網期間無法送達，長斷線可能使 YouTube 結束直播活動。

修改 `.env` 後需重新建立容器，**單純 `docker compose restart` 不會載入新值**：

```sh
docker compose up -d --force-recreate
docker compose logs --tail=100 radio247
```

開始／停止狀態存於 SQLite；`RADIO247_AUTOSTART` 只決定新資料庫的初始值。

### 區網存取

```dotenv
RADIO247_HOST_BIND=192.168.1.10   # 主機發布位址
RADIO247_HOST_PORT=8000
```

`RADIO247_BIND_HOST` 為容器內監聽位址，通常維持 `0.0.0.0`。不要將管理埠直接暴露至公網；管理頁使用 HTTP Basic 登入，跨不可信網路須加 HTTPS，反向代理需保留正確 Host／Origin。

### NAS 核心不支援 CPU／PIDs 限制

| 訊息 | 處理 |
| --- | --- |
| `NanoCPUs can not be set, as your kernel does not support CPU CFS scheduler ...` | 容器建立失敗；設定 `RADIO247_CPUS=0` 停用 CPU 配額。 |
| `Your kernel does not support PIDs limit capabilities ... PIDs limit discarded.` | 僅警告，非啟動失敗；可設 `RADIO247_PIDS_LIMIT=-1` 明確停用。 |

```dotenv
RADIO247_CPUS=0
RADIO247_PIDS_LIMIT=-1
```

不要留空或刪除設定，否則會套回預設值。記憶體限制仍維持，需留意 NAS 整體負載。套用後：

```sh
docker compose up -d --no-build --force-recreate
```

`healthy` 才表示健康檢查通過；持續重啟或 `unhealthy` 時請看 `docker compose logs --tail=100 radio247`。

## 素材

### 放入方式

- **管理頁上傳**：MP3／MPA／M4A／WAV 音檔與 PNG／MP4 圖片，單檔或 ZIP。
- **直接複製**到 `compose.yaml` 同目錄：`./media/song.mp3`、`./images/song.png`（或 `song.mp4`；兩者並存時優先用 PNG）。

音檔與圖片以**相同主檔名**配對。音訊以實際可解碼性為準，不只看副檔名。只掃描素材目錄第一層，不跟隨 symlink。上傳同名檔案會在完成後原子取代舊素材；上傳完成不代表可播放，仍需等待背景準備。

所有資料使用 `./` 相對路徑 bind mount，不使用 named volumes。主機目錄須允許 `RADIO247_UID:RADIO247_GID` 寫入；程式不會遞迴修改 NAS 檔案權限；更改 UID／GID 須重新建置映像。SQLite 的 `./data` 應放本機檔案系統，不使用 SMB／NFS。

| 主機目錄 | 容器目錄 | 用途 |
|---|---|---|
| 映像內建 | `/app` | 程式與 Python 虛擬環境，唯讀 |
| `./media` | `/media` | 原始音檔 |
| `./images` | `/images` | 原始圖片 |
| `./cache` | `/cache` | 音訊分析 JSON 與 H.264／MP4 圖片快取 |
| `./data` | `/data` | SQLite、私有憑證、上傳暫存 |
| `./output` | `/output` | 有保留上限的本機 FLV |
| `./runtime` | `/run/radio247` | 播放心跳等執行狀態 |

### 圖片縮放

歌曲圖片與預設圖都會等比例縮放、置中裁切以填滿輸出畫面，不拉伸、不補黑邊；比例不同時會裁掉上下或左右，小圖會放大，請把重點放在中央。原圖的黑框或透明區域不會自動去除。完全沒有圖片時使用黑底。

### ZIP 批次上傳

`.zip` 可一次放入多首音訊與 PNG：子資料夾會攤平，音訊進 `./media`、圖片進 `./images`，同主檔名自動配對（副檔名轉小寫，主檔名大小寫不變）。

- ZIP 內重複的目的檔名（不分大小寫）會拒絕整包；與磁碟既有素材同名則取代。
- 隱藏檔、`__MACOSX`、不支援的格式會略過並顯示匯入／略過數量；不遞迴解開巢狀 ZIP。
- 只接受未加密的 Stored／Deflate ZIP；拒絕路徑穿越、絕對路徑、連結、空素材與損壞項目。
- 全部解壓與 CRC 檢查完成後才逐檔原子發佈（先圖片再音訊）；發佈途中若磁碟故障可能已有部分檔案匯入，會回報錯誤。

| 變數 | 預設 | 用途 |
|---|---|---|
| `RADIO247_UPLOAD_MAX_BYTES` | 536870912（512 MiB） | ZIP／單檔上傳大小及每個解壓素材上限 |
| `RADIO247_ZIP_MAX_BYTES` | 1073741824（1 GiB） | ZIP 總解壓大小上限 |
| `RADIO247_ZIP_MAX_FILES` | 1000 | ZIP 項目數上限 |

### 預設圖與顯示模式

把預設圖放在 `./images/default.png`（也可由管理頁上傳此檔名），並設定：

```dotenv
RADIO247_DEFAULT_IMAGE=/images/default.png
RADIO247_DEFAULT_IMAGE_POSITION=end
RADIO247_DEFAULT_IMAGE_SECONDS=30
RADIO247_DEFAULT_IMAGE_INTERVAL=120
```

歌曲沒有同名 PNG 或沒有可播歌曲時使用預設圖；留空則用黑圖。指定的預設圖不存在或無法解碼會回報準備錯誤。

`RADIO247_DEFAULT_IMAGE_POSITION` 決定每首歌何時顯示預設圖，其餘時間顯示歌曲自己的圖：

| 模式 | 行為 |
|---|---|
| `start` | 前 N 秒顯示預設圖 |
| `end`（預設） | 結束前 N 秒顯示預設圖 |
| `both` | 頭尾都顯示 |
| `middle` | 以歌曲正中央為中心顯示 |
| `interval` | 每 `RADIO247_DEFAULT_IMAGE_INTERVAL` 秒顯示一次（須大於顯示秒數） |
| `always` | 全程顯示 |
| `none` | 永不顯示預設圖 |
| `random` | 每首（含重播）隨機取 start／end／both／middle |

N 為 `RADIO247_DEFAULT_IMAGE_SECONDS`（預設 30，可為小數，`0` 停用定時顯示；未設定預設圖時不啟用）。比 N 短的歌曲全程顯示預設圖。自然換曲、循環重播或點播都依新一輪進度重新計算；手動切歌直接換曲，不補播尾段。切圖以輸出影格為單位，從快取關鍵影格開始，不重新編碼也不中斷音訊。

**管理頁「預設圖顯示」可即時調整模式與秒數，存於 SQLite，優先於環境變數，無需重啟。**

## 管理頁

深色、大封面、曲目優先，桌面與手機皆可使用。此頁控制伺服器播送，不在瀏覽器播放音訊，也不提供音量或進度拖曳。

- **播放控制**：主按鈕「開始播送／停止播送」（停止不是暫停），旁邊可切下一首。操作回覆先顯示要求已送出，實際狀態以電台回報為準。
- **曲目清單**：寬螢幕為兩欄。可依曲名搜尋，並篩選全部／可播放／準備中／需處理。顯示封面縮圖與播放按鈕；點選 ready 的歌曲會淡出目前音訊並從該首開頭播放（停止狀態下自動開始），播完回到原有隨機流程。未完成或失敗的曲目不可點選，失敗曲目可展開詳情。
- **刪除曲目**：從清單刪除會移除該曲來源音檔與同名 PNG（已產生的快取保留供重用）；若正在播放該曲則自動切到下一首。
- **匯入音樂**：右上按鈕展開匯入區，可選檔或拖放單一檔案，多個素材請打包 ZIP。
- **現播圖片**：顯示目前選用的 H.264 快取畫面並隨實際換圖更新；停止或狀態過期時隱藏。這是**本機播放圖片，不是 YouTube 回傳畫面**，不能用來判定觀眾已看到同一張圖。需登入才能讀取。
- **狀態與診斷**：首頁只顯示白話播放／連線狀態與必要錯誤；完整推流目的地、協定、準備狀態、原始錯誤與 JSON 收在「進階診斷」。串流金鑰顯示為 `<stream-key>`，URL 的帳密、query、fragment 不會送到瀏覽器；本機輸出明確標示「本機 FLV，不推送 RTMP」。管理頁斷線、電台狀態過期、平台重連會分別提示；本機錄製不會標示為平台直播成功。`streaming` 僅表示本機已成功寫出資料。

## 設定（全部使用 env）

[`.env.example`](.env.example) 列出全部設定，[`settings.py`](src/radio247/settings.py) 定義型別與有效範圍。Compose 以 `env_file: .env` 把設定傳入容器；埠號、掛載與資源限制也由同一份 `.env` 插值。直接用 Python 啟動時不會讀 `.env`，需自行設定 process environment，且路徑須為絕對路徑。非法設定會明確報錯，不靜默回退。

| 類別 | 主要變數與單位 |
|---|---|
| 推流 | `RADIO247_INGEST_URL`、`RADIO247_STREAM_KEY` |
| 音訊 | `RADIO247_SAMPLE_RATE`：44100／48000；`RADIO247_CHANNELS`：1／2；`RADIO247_AUDIO_BITRATE`：bps |
| 響度 | `RADIO247_TARGET_LUFS`、`RADIO247_TARGET_PEAK`（dBTP）、`RADIO247_TARGET_LRA`、`RADIO247_FADE_MS`（毫秒） |
| 視訊 | `RADIO247_WIDTH`／`HEIGHT`（偶數像素）、`RADIO247_FPS`：1／24／25／30／50／60、`RADIO247_GOP_SECONDS` |
| 編碼 | `RADIO247_VIDEO_BITRATE`（bps）、`RADIO247_VIDEO_BUFFER_SIZE`（bits）、`RADIO247_VIDEO_PRESET`、`RADIO247_THREADS` |
| 預設圖 | `RADIO247_DEFAULT_IMAGE`、`_POSITION`、`_SECONDS`、`_INTERVAL` |
| 準備 | `RADIO247_SCAN_INTERVAL`、`RADIO247_FILE_STABLE_SECONDS`、`RADIO247_PREPARE_TIMEOUT`、`RADIO247_PROBE_TIMEOUT`（秒） |
| 限額 | `RADIO247_CACHE_MAX_BYTES`、`RADIO247_UPLOAD_MAX_BYTES`、`RADIO247_IMAGE_MAX_PIXELS` |
| 恢復 | `RADIO247_CONNECT_TIMEOUT`、`RADIO247_STALL_TIMEOUT`、`RADIO247_RETRY_INITIAL`／`MAX`／`RESET_SECONDS`、`RADIO247_RETRY_JITTER_RATIO`（0～1） |
| 緩衝 | `RADIO247_QUEUE_SECONDS`（封包佇列，滿時重建連線）、`RADIO247_DECODE_BUFFER_SECONDS`（預設 2）、`RADIO247_DECODE_TIMEOUT`（預設 15 秒） |
| 控制與 UI | `RADIO247_POLL_INTERVAL`、`RADIO247_REQUEST_TIMEOUT`、`RADIO247_UI_POLL_SECONDS` |
| 容器監督 | `RADIO247_SERVICE_START_TIMEOUT`、`RADIO247_SERVICE_STALL_TIMEOUT` |
| 本機錄製 | `RADIO247_LOCAL_SEGMENT_SECONDS`（預設 300）、`RADIO247_LOCAL_KEEP_SEGMENTS`（預設 3） |
| 部署 | `RADIO247_HOST_BIND`／`HOST_PORT`、`RADIO247_UID`／`GID`、CPU／RAM／PID／日誌／healthcheck 限制 |

預設為 **44.1 kHz／雙聲道／192 kbps AAC、1080p30／5 Mbps H.264、2 秒 GOP**。這是本程式設定，不是 YouTube 官方建議值；請依 NAS、上傳頻寬與平台狀況調整。音訊／視訊設定與處理版本參與快取識別，格式變更後會重新準備相容素材。

### 1 fps 低流量設定

`RADIO247_FPS=1` 時視訊每秒一幀，音訊仍依樣本時鐘連續輸出。**只降 FPS 不會讓固定碼率流量下降**（H.264 使用 CBR／VBV），須同時降低 bitrate：

```dotenv
RADIO247_FPS=1
RADIO247_GOP_SECONDS=2
RADIO247_VIDEO_BITRATE=500000
RADIO247_VIDEO_BUFFER_SIZE=1000000
RADIO247_AUDIO_BITRATE=192000
```

視訊 500 kbps＋音訊 192 kbps 約 7.5 GB／日（未含協定開銷與重傳）。這不是 YouTube 官方建議值，平台是否接受與穩定須實播確認；遇到警告可恢復一般幀率。

### 流量與資源

5 Mbps 視訊加 192 kbps 音訊約 **56.1 GB／日**。靜態圖 GOP 快取主要節省 CPU，不降低固定碼率的上傳流量。

## 更新程式

修改原始碼或取得新版後須重建映像並重新建立容器；只重新整理瀏覽器或 `restart` 不會更新：

```sh
docker compose up --build -d
```

## 觀察與停止

```sh
docker compose ps
docker compose logs --tail=100 radio247
docker compose cp radio247:/output ./recordings
docker compose stop
```

本機 FLV 預設每 300 秒換檔、最多保留 3 段，只清理 output 目錄中符合 `radio-*.flv` 的舊檔，請勿用此前綴存放其他資料；換檔會重建本機 publisher，不保證檔間無縫。YouTube 模式不做定時換檔。

`restart: unless-stopped` 處理容器程序退出；Docker healthcheck 以 curl 每 30 秒只檢查 web `/healthz`，且本身不會重啟 unhealthy 容器。容器內 supervisor 每 5 秒檢查 web 與 radio 心跳， 會監看子程序及後台／播放心跳，逾時便回收子程序並退出，交由 Docker 重啟。控制 API 短暫失聯時，radio 保留最近的播放快照。

## 開發與測試

不需要 Docker。需要 Python 3.12 以上、[uv](https://docs.astral.sh/uv/)、含 libx264／loudnorm／alimiter 的 FFmpeg 與 ffprobe：

```sh
uv sync --frozen --dev
.venv/bin/python -m pytest -q
.venv/bin/python -m ruff check src tests
.venv/bin/python -m ruff format --check src tests
.venv/bin/python -m mypy src tests
```

測試涵蓋媒體重封裝、env 驗證、曲庫準備／配額、控制與登入、publisher 卡死回收、本機 RTMP 接收，以及實際啟動 web／radio 子程序的端到端流程。只使用暫存素材與 loopback 端點，不需 YouTube 金鑰，不會向 YouTube 推流。

UI 的 Playwright 測試（桌面／手機、點播、鍵盤焦點、搜尋篩選、匯入、失聯、停止確認）需額外依賴與瀏覽器：

```sh
uv sync --frozen --dev --group ui
.venv/bin/python -m playwright install chromium
.venv/bin/python -m pytest -q tests/test_ui.py tests/test_web.py tests/test_runtime.py
```

未安裝 `ui` 依賴時 UI 測試會被略過，略過不代表 UI 驗收通過。亦可在映像內執行測試：

```sh
docker build --target test -t radio247:test .
docker run --rm radio247:test
```

入口為 `python -m radio247 serve`（管理 web／radio 子程序）與 `python -m radio247 health`。本機直接啟動時需把所有 storage／secret 路徑設為可寫目錄。

## 已知限制

- 圖片 MP4 與分析 JSON 受 `RADIO247_CACHE_MAX_BYTES` 限制，目前沒有自動淘汰；需停止服務後由管理者清理不再需要的快取。
- 音訊有離線響度分析、播放時響度處理、峰值保護與短淡變，但無法修復來源已削波的失真；真實音樂的 AAC true-peak 與長時間量測尚未完成。
- 音訊解碼不足時會補靜音並記錄 `audio_underruns`；NAS 的 CPU／磁碟需有足夠餘裕。
- 尚未提供進階曲庫編輯與資源壓力下的分析暫停。
- NAS 實機（amd64／arm64）效能與 YouTube RTMPS 實播仍需在目標環境驗證；開發機測試通過不代表 NAS／YouTube 驗收。

## 結構

```text
Dockerfile / compose.yaml / .env.example
src/radio247/
  __main__.py       服務與健康檢查入口
  settings.py       集中 env 解析與驗證
  media.py          媒體準備、編碼及封裝
  audio.py          原始音訊串流解碼與無進度期限
  playout.py        有界 PCM 供料、選曲、預設圖時段與淡變
  library.py        SQLite、增量準備、快取與曲目刪除
  web.py            管理／控制 API
  uploads.py        ZIP 驗證、分類與匯入
  radio.py          持續播放與控制同步
  publisher.py      隔離推流、watchdog、重連
  static/index.html 管理頁
tests/              單元與本機整合測試
```

參考：[YouTube 編碼設定](https://support.google.com/youtube/answer/2853702?hl=en)、[FFmpeg filters](https://ffmpeg.org/ffmpeg-filters.html)、[PyAV](https://pyav.org/docs/stable/)、[Docker Compose](https://docs.docker.com/reference/compose-file/services/)。
