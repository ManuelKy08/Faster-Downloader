# IDMLike — Kontrak Antar Modul (SPEC)

GUI download manager mirip IDM di Linux. Stack: PySide6 (GUI) + aria2c (engine) + HTTP capture server (auto-capture dari browser) + Chromium MV3 extension.

## Arsitektur

```
Browser (Chromium + extension)
   │  POST http://127.0.0.1:20129/api/capture {"url": "...", "filename": "..."}
   ▼
capture_server.py  (aiohttp, port 20129)  ──▶  engine.py (add_download)
                                                     │
GUI (app.py, PySide6) ◀── polling status ──▶ engine.py ◀──▶ aria2c RPC (port 6800, secret "idmlike")
```

- `engine.py`: mengelola proses aria2c + komunikasi JSON-RPC (port 6800, secret `idmlike`).
- `capture_server.py`: server HTTP lokal port **20129** untuk menerima URL dari extension browser lalu meneruskan ke engine (auto-capture). Hanya listen `127.0.0.1`.
- `app.py`: GUI PySide6. Satu-satunya entry point. Menjalankan engine + capture server saat start.

## File Layout

```
~/tools/idmlike/
├── run.sh                    # source venv + jalankan GUI
├── requirements.txt
├── .venv/                    # sudah ada, PySide6+aiohttp+websockets terinstall
├── src/idmlike/
│   ├── __init__.py           # __version__ = "0.1.0"
│   ├── config.py             # dataclass Config: download_dir, max_connections, dll + load/save JSON
│   ├── engine.py             # Aria2Engine class (kontrak di bawah)
│   ├── capture_server.py     # CaptureServer class (kontrak di bawah)
│   └── app.py                # GUI (kontrak di bawah)
└── extension/                # Chromium MV3 (kontrak di bawah)
```

## Kontrak engine.py

```python
class Aria2Engine(QObject):   # PySide6 QObject, emit signal via QTimer polling
    # signals
    status_updated = Signal()          # dipancarkan setiap polling selesai

    def start(self) -> None            # spawn aria2c subprocess + siapkan session RPC
    def stop(self) -> None             # shutdown aria2c (kirim aria2.shutdown), tunggu exit
    def is_running(self) -> bool

    def add_download(self, url: str, filename: str | None = None,
                     directory: str | None = None) -> str
        # returns gid; ditambah dengan split = config.max_connections (default 16),
        # continue=true (resume), auto-file-renaming=false, max-tries=10, retry-wait=3
    def pause(self, gid: str) -> None
    def resume(self, gid: str) -> None
    def cancel(self, gid: str) -> None  # aria2.remove + hapus file .aria2
    def open_dir(self, gid: str) -> None

    def snapshot(self) -> list[dict]:
        # AKTIF + WAITING + STOPPED digabung, tiap item:
        # {
        #   "gid": str, "status": "active"|"waiting"|"paused"|"error"|"complete"|"removed",
        #   "filename": str, "total": int, "completed": int, "speed": int,  # bytes
        #   "dir": str, "error": str | "", "connections": int,
        #   "percent": float  # 0..100
        # }
```

Rule engine:
- Port aria2 RPC: **localhost:6800**, secret: **idmlike**
- `aria2c` flags WAJIB: `--enable-rpc --rpc-listen-port=6800 --rpc-secret=idmlike
  --dir=<download_dir> --continue=true --max-connection-per-server=16 --split=16
  --min-split-size=1M --auto-file-renaming=false --allow-overwrite=false
  --max-tries=10 --retry-wait=3 --timeout=60 --connect-timeout=30 --console-log-level=warn`
- `download_dir` default: `~/Downloads` (baca config).

## Kontrak capture_server.py

```python
class CaptureServer:
    def __init__(self, engine: Aria2Engine) -> None
    async def start(self) -> None   # aiohttp app listen 127.0.0.1:20129
    async def stop(self) -> None
```

Routes:
- `POST /api/capture`  body `{"url": "...", "filename": "..."}` → `engine.add_download(url, filename)` → `200 {"ok": true, "gid": "..."}`. filename optional.
- `GET  /api/ping` → `200 {"ok": true}` (dipakai extension untuk cek app hidup)
- Semua request lain → `404 {"ok": false}`

## Kontrak app.py (GUI)

- QMainWindow, judul "IDMLike", ukuran awal 900x560.
- **Tabel download** (QTableWidget) kolom: Nama File | Ukuran | Progress (bar) | Kecepatan | Status | Aksi
- Toolbar atas: tombol `+ Tambah` (dialog URL), `Pause`, `Resume`, `Batal`, `Hapus`, `Buka Folder`, dan QSpinBox `Max koneksi` (1-16).
- Setiap 500ms: polling `engine.snapshot()` → update tabel. Progress bar di dalam sel; format ukuran human-readable (B/KB/MB/GB), kecepatan (KB/s/MB/s), ETA.
- Menampilkan tray icon keterangan "cap: <filename> — <persen>%" — cukup jika mudah; boleh dilewatkan bila memakan waktu.
- Dialog "Tambah Download": input URL (QLineEdit) + folder tujuan (QLineEdit + browse) + nama file optional. OK → `engine.add_download()`.
- Status bar bawah: jumlah download aktif + total kecepatan gabungan.
- Saat close: `engine.stop()` (bukan cancel download — aria2 resume state tetap tersimpan).

## Kontrak extension (Chromium MV3)

- `extension/manifest.json`: MV3, permission `["downloads", "storage"]`, host_permissions `http://127.0.0.1:20129/*`.
- `extension/background.js`: intercept `chrome.downloads.onCreated`:
  1. `chrome.downloads.cancel(id)` (batalkan download asli browser biar kecepatan penuh di aria2)
  2. `fetch("http://127.0.0.1:20129/api/capture", {method:"POST", body: JSON.stringify({url: item.url, filename: item.filename})})`
  3. Handle error: jika app mati (fetch gagal), **ulangi download asli** (jangan hilangkan download user!) — pakai `chrome.downloads.download({url, filename})` ulang.
  - JANGAN intercept file yang skema-nya `blob:`, `data:`, `chrome-extension://`, atau ukuran tidak diketahui.
- Extension TIDAK perlu settings UI — behavior default nyalakan.

## Rules Global

- Kode Python: strict typing, tanpa `Any` asal; error path di-handle (tidak ada empty except). Bahasa komentar/UI: **Bahasa Indonesia**.
- Hanya boleh mengimpor: PySide6, aiohttp, json, urllib, socket, subprocess, pathlib, dataclasses, typing, time.
- `requirements.txt` berisi: PySide6, aiohttp.
- Jangan buat file selain kontrak di atas tanpa izin.
- Jangan install package tambahan.

## Test Cepat (manual, jalankan di /tmp)

```bash
cd ~/tools/idmlike
./.venv/bin/python - <<'EOF'
import sys; sys.path.insert(0, "src")
from idmlike.engine import Aria2Engine
engine = Aria2Engine()
engine.start()
gid = engine.add_download("https://speed.hetzner.de/100MB.bin")
import time
for _ in range(20):
    time.sleep(0.5)
    s = [x for x in engine.snapshot() if x["gid"]==gid]
    if s: print(round(s[0]["percent"],1), "%", s[0]["speed"]//1024, "KB/s")
engine.stop()
EOF
```