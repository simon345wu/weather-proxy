# weather-proxy

給咖啡烘豆流程用的 **PC 端天氣 proxy**。它向 [Open-Meteo](https://open-meteo.com/)
抓取所在地區的**氣溫、大氣壓力、相對濕度**,快取後透過**純 HTTP** 提供給
[Trident](#和-trident-專案的關係) 烘豆控制器;Trident 再把這些值送進
**Artisan** 的烘焙屬性(Roast Properties),並顯示在自己的螢幕上。

一句話:**讓 Artisan 的烘焙紀錄自動帶上當下的環境天氣,而不必手動查、手動填。**

---

## 用途

Artisan 的烘焙屬性可以記錄環境溫度 / 濕度 / 氣壓,但預設要靠實體感測器
(如 Phidget)或手動輸入。這個 proxy 讓這些值改成**依地區從網路自動取得**:

- 在網頁上選一次城市(存進 `weather_config.json`);
- 之後 proxy 每 5 分鐘向 Open-Meteo 更新一次(其餘時間回快取);
- 烘豆時整條鏈自動把當下天氣帶進 Artisan。

---

## 為什麼需要一個「PC 端 proxy」

天氣資料最終是要進 Trident(ESP32-S3 韌體)再轉給 Artisan 的,那為什麼不讓
Trident 自己上網抓?

因為 **Trident 這顆 ESP32-S3 做不了 HTTPS**。它同時跑 BLE(NimBLE)+ WiFi +
LVGL 螢幕 + AsyncWebServer,內部 RAM 只剩約 **17 KB**、最大連續區塊約
**7.6 KB**;而一次 TLS 握手需要 **16–40 KB 連續記憶體**。實測結果:TLS 不但
配置失敗,還會把 AsyncWebServer 一起餓死(WebSerial 卡死)。

解法就是把 HTTPS 這件「吃記憶體」的事**移到 PC** 上做:

- **PC(這個 proxy)**:對 Open-Meteo 走 HTTPS、做快取、保存地區設定;
- **Trident**:只用**純 HTTP**(幾 KB,無 TLS)向 proxy 拉取,記憶體無虞。

烘豆時 PC 本來就開著跑 Artisan、也在同一個 LAN 上,所以 proxy 隨時可用。

---

## 架構

### 資料流

```mermaid
flowchart LR
    U[使用者瀏覽器] -- 選城市 --> P
    OM[Open-Meteo API] -- HTTPS --> P
    P[「weather-proxy」<br/>artisan_weather_ws.py<br/>PC · 0.0.0.0:8765]
    P -. mDNS 廣播 _artisanwx._tcp .-> T
    T -- 純 HTTP GET /api/preview --> P
    T[Trident 韌體<br/>ESP32-S3]
    T -- WebSocket 通道 AT/AP/AH --> A[Artisan<br/>Roast Properties]
    T -- 顯示 --> D[Trident Config 畫面]
```

重點:
- **HTTPS 只發生在 PC ↔ Open-Meteo。** Trident ↔ proxy 全程純 HTTP。
- **mDNS 自動探索**:proxy 廣播 `_artisanwx._tcp`,Trident 用它已有的
  ESPmDNS 查到 PC 目前 IP,**不必手動填 IP**;PC 換 IP(抓取失敗)時 Trident
  會自動重新探索,可自癒。

### 程式結構(`artisan_weather_ws.py`,單檔、以 aiohttp 實作)

| 區塊 | 功能 |
|---|---|
| `load_config` / `save_config` | 讀寫 `weather_config.json`(地區:名稱 / 經緯度 / 海拔) |
| `fetch_weather` / `get_weather` | 向 Open-Meteo 抓 `current`(temperature_2m、relative_humidity_2m、surface_pressure);快取 `CACHE_TTL = 300` 秒,並在地區變更時失效 |
| `index` + `INDEX_HTML` | 地區選取網頁(城市搜尋用 Open-Meteo geocoding,選取後存檔) |
| `api_config_get` / `api_config_post` | 讀取 / 設定目前地區 |
| `api_preview` | 回 `{temp, pressure, humidity, valid}` 給 Trident 拉取(`valid` 為 false 代表尚未設定地區或還沒抓到) |
| `_lan_ip` / `on_startup` / `on_cleanup` | 用 `zeroconf`(`AsyncZeroconf`)在 LAN 廣播 mDNS 服務 |

> 註:檔案中還留有一個未使用的 WebSocket 端點 `/artisan`,是早期
> 「proxy 直接當 Artisan 的 WS 裝置」設計的殘留,目前架構不會用到,可日後移除。

---

## 安裝與執行

需求:Python 3.11+,套件 `aiohttp`、`zeroconf`(缺 `zeroconf` 仍可運作,只是
少了 mDNS 自動探索,Trident 需手動設定 proxy)。

用 [uv](https://github.com/astral-sh/uv) 一行啟動(免手動建立 venv):

```bash
uv run --python 3.14 --with aiohttp --with zeroconf -- python C:\myproject\weather-proxy\artisan_weather_ws.py
```

啟動後:
- 監聽 `0.0.0.0:8765`(綁全介面,LAN 上的 Trident 才連得到);
- 開始 mDNS 廣播;
- 若被 Windows 防火牆詢問,需放行 **8765 inbound**,Trident 才能拉取。

---

## 設定地區

瀏覽器開 **`http://<PC-IP>:8765/`**(或 `http://localhost:8765/`):

1. 輸入城市名稱(支援中文,如「台北」),按「搜尋」;
2. 從結果清單按「選取」——存進 `weather_config.json`,即時生效;
3. 頁面會顯示該地海拔(MASL)供參考。

Trident 下一次拉取(最慢 5 分鐘,或它自己的重試週期)就會用新地區。

---

## API 端點

| 方法 | 路徑 | 說明 |
|---|---|---|
| GET | `/` | 地區選取網頁 |
| GET | `/api/config` | 目前地區設定(JSON) |
| POST | `/api/config` | 設定地區(body:`{name, latitude, longitude, elevation}`) |
| GET | `/api/preview` | 目前天氣讀數 `{temp, pressure, humidity, valid}` ← Trident 拉這個 |

---

## 檔案

| 檔案 | 版控 | 說明 |
|---|---|---|
| `artisan_weather_ws.py` | ✅ | proxy 本體(單檔) |
| `.gitignore` | ✅ | 忽略執行期產物 |
| `weather_config.json` | ❌(忽略) | 執行期寫入的目前地區,因人而異,不進版控 |

---

## 和 Trident 專案的關係

**Trident** 是驅動 Skywalker V1 咖啡烘豆機的 ESP32-S3 韌體(支援 USB / BLE /
WiFi),位於另一個 repo:

```
C:\myproject\skyproject\SkywalkerRoasterLab\Trident   (branch: ambient-weather-http)
```

分工:

- **weather-proxy(本專案,PC)**:負責上網(HTTPS)、快取、保存地區、mDNS 廣播。
- **Trident(ESP32 韌體)**:
  - 用 mDNS 找到本 proxy,純 HTTP 拉 `/api/preview`(`src/weather.cpp`);
  - 把 `AT`/`AP`/`AH`(環境溫度/壓力/濕度)三個節點加進給 Artisan 的
    WebSocket 回應(`src/CommandLoop.cpp`),對應 Artisan 的 extra device 與
    Ambient 來源;
  - 在 Config 畫面顯示讀數(`src/display.cpp`)。
- **Artisan(PC 上的烘豆軟體)**:主裝置是 Trident 的 WebSocket;把 Trident 送
  來的 AT/AP/AH 指派為 Ambient 來源,於下豆(CHARGE)時寫入 Roast Properties。

也就是說,本 proxy 是這條鏈最上游的「上網代打」,存在的唯一理由,就是替
**記憶體不足以做 TLS 的 Trident** 完成 HTTPS 抓取。兩者透過 LAN 上的純 HTTP +
mDNS 鬆耦合,任一邊重啟或換 IP 都能自動接回。
