#!/usr/bin/env python3
"""
artisan_weather_ws.py
---------------------
PC 端天氣 proxy:Trident(ESP32)烘豆控制器用純 HTTP 來這裡拿環境資料
(氣溫/氣壓/濕度),再經 WebSocket 餵給 Artisan。HTTPS 由 PC 這端對 Open-Meteo
處理(ESP32 記憶體不夠做 TLS)。

功能:
  1. 設定網頁 (http://<PC-IP>:8765/):用城市名稱搜尋選位置,存檔即時生效,
     並顯示該地海拔 (MASL)。
  2. GET /api/preview → {"temp":..,"pressure":..,"humidity":..,"valid":bool}
     供 Trident 拉取(含快取,平常不打 Open-Meteo)。
  3. mDNS 廣播 _artisanwx._tcp:Trident 自動探索本機 IP,不必手動設定;
     PC IP 變了 Trident 也會自動重新找到。

依賴:
    aiohttp、zeroconf(zeroconf 缺少時仍可運作,只是沒有自動探索)

執行:
    uv run --python 3.14 --with aiohttp --with zeroconf -- python artisan_weather_ws.py
然後用瀏覽器開 http://<PC-IP>:8765/(或 http://localhost:8765/)選位置。
"""

import asyncio
import json
import pathlib
import socket
import time

from aiohttp import ClientSession, ClientTimeout, WSMsgType, web

# zeroconf 讓 Trident 用 mDNS 自動找到本機(不必手動填 IP)。缺這個套件時
# 只是少了自動探索,其餘功能照常。
try:
    from zeroconf import ServiceInfo
    from zeroconf.asyncio import AsyncZeroconf
    _HAVE_ZEROCONF = True
except ImportError:
    _HAVE_ZEROCONF = False

# Trident 會查詢的 mDNS 服務類型（對應 Trident 端 MDNS.queryService("artisanwx","tcp")）。
MDNS_SERVICE_TYPE = "_artisanwx._tcp.local."
MDNS_SERVICE_NAME = "Artisan Weather._artisanwx._tcp.local."

# ---- 基本設定 ------------------------------------------------------------
# 綁定 0.0.0.0 讓 LAN 上的 Trident 也能連到 /api/preview（不只本機）。
HOST = "0.0.0.0"
PORT = 8765
WS_PATH = "/artisan"                # ← 在 Artisan 的 Port 對話框 Path 欄填 "artisan"
CONFIG_PATH = pathlib.Path(__file__).with_name("weather_config.json")
CACHE_TTL = 300                     # 秒；每 5 分鐘才真的向 API 抓一次

# ---- 位置設定的讀寫 ------------------------------------------------------
def load_config():
    """讀取已存的位置設定；沒有就回 None。"""
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception as e:
            print("讀取設定失敗:", e)
    return None


def save_config(cfg):
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


# ---- 天氣抓取 + 快取 -----------------------------------------------------
_cache = {"key": None, "ts": 0.0, "data": None}
EMPTY = {"temp": -1, "humidity": -1, "pressure": -1}


async def fetch_weather(session, lat, lon):
    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lon}"
        "&current=temperature_2m,relative_humidity_2m,surface_pressure"
    )
    async with session.get(url) as r:
        cur = (await r.json())["current"]
    return {
        "temp": cur["temperature_2m"],          # °C
        "humidity": cur["relative_humidity_2m"],  # %
        "pressure": cur["surface_pressure"],      # hPa（當地實測絕對氣壓）
    }


async def get_weather(session):
    """回傳目前位置的天氣；含快取與位置變更偵測。"""
    cfg = load_config()
    if not cfg:
        return dict(EMPTY)
    key = (cfg["latitude"], cfg["longitude"])
    now = time.time()
    if _cache["data"] is None or _cache["key"] != key or now - _cache["ts"] > CACHE_TTL:
        try:
            _cache["data"] = await fetch_weather(session, *key)
            _cache["key"], _cache["ts"] = key, now
        except Exception as e:
            print("向 Open-Meteo 抓取天氣失敗:", e)
    return _cache["data"] or dict(EMPTY)


# ---- Artisan 用的 WebSocket 端點 ----------------------------------------
async def ws_handler(request):
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    print("Artisan 已連線")
    session = request.app["session"]
    async for msg in ws:
        if msg.type == WSMsgType.TEXT:
            try:
                req = json.loads(msg.data)
            except json.JSONDecodeError:
                continue
            if req.get("command") == "getData":
                data = await get_weather(session)
                await ws.send_str(json.dumps({"id": req.get("id"), "data": data}))
        elif msg.type == WSMsgType.ERROR:
            break
    print("Artisan 連線中斷")
    return ws


# ---- 設定網頁的 API ------------------------------------------------------
async def api_config_get(request):
    return web.json_response(load_config() or {})


async def api_config_post(request):
    body = await request.json()
    try:
        cfg = {
            "name": body.get("name", ""),
            "admin1": body.get("admin1", ""),
            "country": body.get("country", ""),
            "latitude": float(body["latitude"]),
            "longitude": float(body["longitude"]),
            "elevation": body.get("elevation"),  # 海拔（公尺），做為 MASL 建議值
        }
    except (KeyError, TypeError, ValueError):
        return web.json_response({"ok": False, "error": "缺少或不正確的經緯度"}, status=400)
    save_config(cfg)
    _cache["data"] = None  # 讓下次立即重新抓
    return web.json_response({"ok": True, "config": cfg})


async def api_preview(request):
    """給網頁「測試讀取」與 Trident 拉取用。附上 valid 旗標:只有真的抓到過
    線上讀數(_cache 有資料)才是 True,讓 Trident 不會把未設定位置時的
    -1 佔位值當成真值送給 Artisan。"""
    w = await get_weather(request.app["session"])
    out = dict(w)
    out["valid"] = _cache["data"] is not None
    return web.json_response(out)


async def index(request):
    return web.Response(text=INDEX_HTML, content_type="text/html")


# ---- 設定網頁（位置選取介面）--------------------------------------------
INDEX_HTML = """<!doctype html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>烘豆環境天氣 — 地區設定</title>
<style>
  :root { color-scheme: light dark; }
  body { font: 15px/1.5 system-ui, "Microsoft JhengHei", sans-serif;
         max-width: 640px; margin: 24px auto; padding: 0 16px; }
  h1 { font-size: 20px; }
  .row { display: flex; gap: 8px; margin: 12px 0; }
  input[type=text] { flex: 1; padding: 8px 10px; font-size: 15px; }
  button { padding: 8px 14px; font-size: 15px; cursor: pointer; }
  ul { list-style: none; padding: 0; margin: 8px 0; }
  li { border: 1px solid #8886; border-radius: 8px; padding: 10px 12px;
       margin: 6px 0; display: flex; justify-content: space-between; align-items: center; gap: 8px; }
  li .meta { font-size: 13px; opacity: .7; }
  .current { border: 1px solid #4a9; border-radius: 8px; padding: 12px; margin: 16px 0;
             background: #4a91; }
  .masl { font-weight: 700; }
  .muted { opacity: .6; font-size: 13px; }
  pre { background: #8881; padding: 10px; border-radius: 8px; overflow:auto; }
</style>
</head>
<body>
  <h1>烘豆環境天氣 — 地區設定</h1>
  <p class="muted">搜尋城市 → 選取,存檔即時生效。這裡設定的地區,是 Trident 透過 mDNS 自動抓取、再送進 Artisan 烘焙屬性的環境資料來源。</p>

  <div class="row">
    <input id="q" type="text" placeholder="輸入城市名稱，例如：台北 / Tokyo / Seattle" autofocus>
    <button id="searchBtn">搜尋</button>
  </div>
  <ul id="results"></ul>

  <div id="current" class="current" hidden></div>

  <div class="row">
    <button id="previewBtn">測試讀取目前天氣</button>
  </div>
  <pre id="preview" hidden></pre>

<script>
const $ = s => document.querySelector(s);

async function loadCurrent() {
  const c = await (await fetch('/api/config')).json();
  const el = $('#current');
  if (c && c.latitude !== undefined) {
    const masl = (c.elevation ?? null) !== null ? Math.round(c.elevation) : '未知';
    el.hidden = false;
    el.innerHTML = `目前地區：<b>${c.name || ''}</b> ${c.admin1 || ''} ${c.country || ''}`
      + `<br>經緯度：${c.latitude.toFixed(4)}, ${c.longitude.toFixed(4)}`
      + `<br>海拔（MASL）：<span class="masl">${masl}</span> 公尺`;
  } else {
    el.hidden = false;
    el.textContent = '尚未設定位置，請先搜尋並選取。';
  }
}

async function search() {
  const name = $('#q').value.trim();
  if (!name) return;
  const url = 'https://geocoding-api.open-meteo.com/v1/search?count=10&language=zh&format=json&name='
              + encodeURIComponent(name);
  const results = $('#results');
  results.innerHTML = '<li>搜尋中…</li>';
  try {
    const data = await (await fetch(url)).json();
    const list = data.results || [];
    if (!list.length) { results.innerHTML = '<li>找不到符合的地點</li>'; return; }
    results.innerHTML = '';
    for (const r of list) {
      const li = document.createElement('li');
      const label = `${r.name}${r.admin1 ? '，'+r.admin1 : ''}，${r.country || ''}`;
      li.innerHTML = `<span>${label}<br><span class="meta">`
        + `${r.latitude.toFixed(3)}, ${r.longitude.toFixed(3)} · 海拔 `
        + `${r.elevation != null ? Math.round(r.elevation)+' m' : '未知'}</span></span>`;
      const btn = document.createElement('button');
      btn.textContent = '選取';
      btn.onclick = () => select(r);
      li.appendChild(btn);
      results.appendChild(li);
    }
  } catch (e) {
    results.innerHTML = '<li>搜尋失敗：' + e + '</li>';
  }
}

async function select(r) {
  const payload = {
    name: r.name, admin1: r.admin1 || '', country: r.country || '',
    latitude: r.latitude, longitude: r.longitude, elevation: r.elevation,
  };
  const res = await (await fetch('/api/config', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(payload),
  })).json();
  if (res.ok) { $('#results').innerHTML = ''; $('#q').value = ''; loadCurrent(); }
  else alert('存檔失敗：' + (res.error || '未知錯誤'));
}

async function preview() {
  const pre = $('#preview');
  pre.hidden = false; pre.textContent = '讀取中…';
  const w = await (await fetch('/api/preview')).json();
  pre.textContent =
    `氣溫 temp     = ${w.temp} °C\n` +
    `氣壓 pressure = ${w.pressure} hPa\n` +
    `濕度 humidity = ${w.humidity} %`;
}

$('#searchBtn').onclick = search;
$('#q').addEventListener('keydown', e => { if (e.key === 'Enter') search(); });
$('#previewBtn').onclick = preview;
loadCurrent();
</script>
</body>
</html>
"""


# ---- mDNS 廣播（給 Trident 自動探索）------------------------------------
def _lan_ip():
    """回傳本機對外(LAN)的 IPv4。先用「UDP connect」技巧;在多網卡機器
    (如本機的 Hyper-V vEthernet)上它可能回 0.0.0.0,所以退而掃描主機的
    位址,優先挑私網段(192.168 / 10 / 172),避免廣播出無效位址。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
    except Exception:
        ip = ""
    finally:
        s.close()
    if ip and ip != "0.0.0.0" and not ip.startswith("169.254."):
        return ip
    try:
        addrs = {i[4][0] for i in socket.getaddrinfo(socket.gethostname(), None,
                                                      socket.AF_INET)}
    except Exception:
        addrs = set()
    for pref in ("192.168.", "10.", "172."):
        for a in sorted(addrs):
            if a.startswith(pref):
                return a
    return "127.0.0.1"


# ---- 啟動 ----------------------------------------------------------------
async def on_startup(app):
    app["session"] = ClientSession(timeout=ClientTimeout(total=10))
    app["azc"] = None
    if _HAVE_ZEROCONF:
        try:
            ip = _lan_ip()
            info = ServiceInfo(
                MDNS_SERVICE_TYPE,
                MDNS_SERVICE_NAME,
                addresses=[socket.inet_aton(ip)],
                port=PORT,
                properties={"path": "/api/preview"},
                server="artisanwx.local.",
            )
            azc = AsyncZeroconf()
            await azc.async_register_service(info)
            app["azc"] = (azc, info)
            print(f"mDNS: 廣播 {MDNS_SERVICE_TYPE} @ {ip}:{PORT}(Trident 自動探索)")
        except Exception as e:
            print("mDNS 廣播失敗（不影響其他功能）:", e)
    else:
        print("未安裝 zeroconf → 無 mDNS 自動探索(Trident 需手動設 proxy)")


async def on_cleanup(app):
    z = app.get("azc")
    if z:
        azc, info = z
        try:
            await azc.async_unregister_service(info)
            await azc.async_close()
        except Exception:
            pass
    await app["session"].close()


def main():
    app = web.Application()
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    app.add_routes([
        web.get("/", index),
        web.get(WS_PATH, ws_handler),
        web.get("/api/config", api_config_get),
        web.post("/api/config", api_config_post),
        web.get("/api/preview", api_preview),
    ])
    print(f"設定網頁:  http://<PC-IP>:{PORT}/   (或 http://localhost:{PORT}/)")
    print(f"Trident 拉取: GET http://<PC-IP>:{PORT}/api/preview")
    web.run_app(app, host=HOST, port=PORT, print=None)


if __name__ == "__main__":
    main()
