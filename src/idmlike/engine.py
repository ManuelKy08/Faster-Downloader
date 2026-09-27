"""Engine aria2 — membungkus aria2c RPC untuk IDMLike.

Menjalankan proses aria2c dengan RPC di localhost:6800 (secret "idmlike"),
dan menyediakan API sederhana: add/pause/resume/cancel download + snapshot status.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from PySide6.QtCore import QCoreApplication, QObject, QTimer

from .config import Config

RPC_URL = "http://localhost:6800/jsonrpc"
RPC_SECRET = "idmlike"
POLL_INTERVAL_MS = 500

_ARIA2_FLAGS = [
    "--enable-rpc",
    "--rpc-listen-port=6800",
    "--rpc-secret=idmlike",
    "--continue=true",
    "--max-connection-per-server=16",
    "--split=16",
    "--min-split-size=1M",
    "--auto-file-renaming=false",
    "--allow-overwrite=false",
    "--max-tries=10",
    "--retry-wait=3",
    "--timeout=60",
    "--connect-timeout=30",
    "--console-log-level=warn",
    "--file-allocation=none",
]

# mapping errorCode aria2 -> deskripsi Bahasa Indonesia
_ERROR_MSG = {
    "1": "File sudah pernah diunduh",
    "2": "Duplikat GID",
    "3": "Resource tidak ditemukan",
    "4": "Lokasi penyimpanan tidak tersedia",
    "5": "Ukuran file tidak cocok",
    "6": "Satu file gagal",
    "7": "Banyak file gagal",
    "8": "Terlalu banyak redirect",
    "9": "Timeout",
    "10": "Transfer sudah berjalan",
    "11": "File tidak ditemukan",
    "12": "Unauthorized",
    "13": "RPC berhenti",
    "14": "Lokasi tidak ada",
    "15": "Tidak bisa simpan file",
    "16": "Tidak bisa membuka file",
    "17": "Tidak bisa membuat direktori",
    "18": "Ukuran file tidak dikenal",
    "19": "Silakan luncurkan kembali",
    "20": "Bukan file reguler",
    "21": "Koneksi diblokir proxy",
    "22": "Kredensial salah",
    "23": "Checksum tidak cocok",
    "24": "Checksum tidak ditemukan",
    "25": "Metalink tidak parseable",
    "26": "URI salah",
    "27": "Terlalu sedikit koneksi",
    "28": "Dibolehkan mencoba lagi",
}

_next_id = 0


def _jsonrpc(
    method: str,
    params: list[Any] | None = None,
    timeout: float = 5.0,
) -> Any:
    """Kirim JSON-RPC ke aria2 dan kembalikan result (pastikan valid.param)."""
    global _next_id
    _next_id += 1
    payload = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": _next_id,
            "method": method,
            "params": [f"token:{RPC_SECRET}", *(params or [])],
        }
    ).encode()
    req = urllib.request.Request(
        RPC_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        # aria2 mengirim HTTP 400 + body JSON-RPC error untuk request yang salah
        try:
            err_body = json.loads(exc.read().decode())
        except Exception:
            raise ConnectionError(f"aria2 RPC HTTP {exc.code}: {exc.reason}") from exc
        if "error" in err_body:
            msg = err_body["error"].get("message", str(err_body["error"]))
            raise RuntimeError(f"aria2 error: {msg}")
        raise ConnectionError(f"aria2 RPC HTTP {exc.code}: {exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise ConnectionError(f"aria2 RPC tidak terjangkau: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ConnectionError(f"Response RPC bukan JSON: {exc}") from exc
    if "error" in body:
        msg = body["error"].get("message", str(body["error"]))
        raise RuntimeError(f"aria2 error: {msg}")
    return body.get("result")


def _fmt_error(code: str | None) -> str:
    if not code:
        return ""
    return _ERROR_MSG.get(code, f"Error {code}")


class Aria2Engine(QObject):
    """Antarmuka ke aria2c via JSON-RPC.

    Contoh penggunaan:
        engine = Aria2Engine()
        engine.start()
        gid = engine.add_download("http://.../file.bin")
        for item in engine.snapshot():
            print(item["filename"], item["percent"])
        engine.stop()
    """

    def __init__(self, config: Config | None = None) -> None:
        super().__init__()
        self.config = config or Config.load()
        self._proc: subprocess.Popen[str] | None = None
        self._stop_flag = False
        self._timer = QTimer(self)
        self._timer.setInterval(POLL_INTERVAL_MS)
        self._timer.timeout.connect(self._refresh)
        self._last_snapshot: list[dict[str, Any]] = []

    # ---------------------------------------------------------------- siklus hidup
    def start(self) -> None:
        """Spawn aria2c (kalau belum ada) lalu mulai polling."""
        if self.is_running():
            return
        if shutil.which("aria2c") is None:
            raise RuntimeError(
                "aria2c tidak ditemukan. Install dulu: sudo apt install aria2"
            )
        Path(self.config.download_dir).mkdir(parents=True, exist_ok=True)
        flags = _ARIA2_FLAGS + [f"--dir={self.config.download_dir}",
                                f"--max-connection-per-server={self.config.max_connections}",
                                f"--split={self.config.max_connections}"]
        self._proc = subprocess.Popen(
            ["aria2c", *flags],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        # tunggu RPC siap (max ~5 detik)
        deadline = time.time() + 5.0
        while time.time() < deadline:
            if self.is_running():
                if QCoreApplication.instance() is not None:
                    self._timer.start()
                return
            time.sleep(0.1)
        self.stop()
        raise RuntimeError("aria2c tidak merespons RPC dalam 5 detik")

    def stop(self) -> None:
        """Kirim aria2.shutdown lalu tutup proses."""
        self._stop_flag = True
        self._timer.stop()
        try:
            _jsonrpc("aria2.shutdown", timeout=2.0)
        except Exception:
            pass
        if self._proc is not None:
            try:
                self._proc.wait(timeout=3.0)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                self._proc.wait(timeout=2.0)
            self._proc = None
        self._stop_flag = False

    def is_running(self) -> bool:
        if self._proc is None or self._proc.poll() is not None:
            return False
        try:
            _jsonrpc("aria2.getVersion", timeout=1.5)
            return True
        except Exception:
            return False

    # ------------------------------------------------------------------ operasi
    def add_download(
        self,
        url: str,
        filename: str | None = None,
        directory: str | None = None,
    ) -> str:
        """Tambahkan download. Kembalikan gid."""
        opts: dict[str, str] = {
            "max-connection-per-server": str(self.config.max_connections),
            "split": str(self.config.max_connections),
            "min-split-size": "1M",
            "continue": "true",
            "auto-file-renaming": "false",
        }
        if filename:
            opts["out"] = filename
        if directory:
            Path(directory).mkdir(parents=True, exist_ok=True)
            opts["dir"] = directory
        return str(_jsonrpc("aria2.addUri", [[url], opts]))

    def pause(self, gid: str) -> None:
        try:
            _jsonrpc("aria2.pause", [gid])
        except RuntimeError:
            pass  # gid sudah selesai/dihentikan — no-op

    def resume(self, gid: str) -> None:
        try:
            _jsonrpc("aria2.unpause", [gid])
        except RuntimeError:
            pass  # gid tidak dalam status paused — no-op

    def cancel(self, gid: str) -> None:
        """Hentikan download dan hapus part file .aria2."""
        try:
            r = _jsonrpc("aria2.remove", [gid])
        except Exception:
            r = _jsonrpc("aria2.removeDownloadResult", [gid])
        if isinstance(r, dict) and "files" in r:
            self._cleanup_partial(r)

    def remove_result(self, gid: str) -> None:
        """Hapus dari daftar riwayat (file tetap ada)."""
        try:
            _jsonrpc("aria2.removeDownloadResult", [gid])
        except Exception:
            pass

    def open_dir(self, gid: str) -> None:
        """Buka folder tempat file download lewat file manager default."""
        for item in self._last_snapshot:
            if item["gid"] == gid:
                d = item.get("dir") or self.config.download_dir
                d = re.sub(r"^file://", "", d)
                subprocess.Popen(["xdg-open", os.path.expanduser(d)])
                return

    def _cleanup_partial(self, info: dict[str, Any]) -> None:
        for f in info.get("files", []):
            path = f.get("path", "")
            for suffix in ("", ".aria2"):
                try:
                    os.unlink(path + suffix)
                except OSError:
                    pass

    def snapshot(self) -> list[dict[str, Any]]:
        """Status gabungan: active + waiting + stopped/error/complete."""
        out: dict[str, dict[str, Any]] = {}
        try:
            active = _jsonrpc("aria2.tellActive", []) or []
            waiting = _jsonrpc("aria2.tellWaiting", [0, 1000]) or []
            stopped = _jsonrpc("aria2.tellStopped", [0, 1000]) or []
        except Exception:
            return self._last_snapshot
        for raw in (*active, *waiting, *stopped):
            gid = str(raw.get("gid", ""))
            if not gid:
                continue
            total = int(raw.get("totalLength", 0) or 0)
            completed = int(raw.get("completedLength", 0) or 0)
            speed = int(raw.get("downloadSpeed", 0) or 0)
            files = raw.get("files") or []
            path = (files[0].get("path") if files else None) or ""
            filename = os.path.basename(path) or f"[{gid[:8]}]"
            status = str(raw.get("status", "unknown"))
            if status == "removed":
                status = "removed"
            entry = {
                "gid": gid,
                "status": status,
                "filename": filename,
                "total": total,
                "completed": completed,
                "speed": speed,
                "dir": str(raw.get("dir", self.config.download_dir)),
                "error": str(raw.get("errorMessage", "") or _fmt_error(str(raw.get("errorCode", "")) or None)).strip(),
                "connections": int(raw.get("numConnections", 0) or 0),
                "percent": round((completed / total * 100.0) if total else 0.0, 1),
            }
            out[gid] = entry
        self._last_snapshot = list(out.values())
        return self._last_snapshot

    # ------------------------------------------------------------------ polling
    def _refresh(self) -> None:
        self.snapshot()