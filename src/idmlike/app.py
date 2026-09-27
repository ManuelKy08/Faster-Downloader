"""IDMLike — GUI download manager (PySide6) berbasis engine aria2.

Menjalankan Aria2Engine (aria2c RPC) + CaptureServer (auto-capture dari browser)
saat aplikasi start, dan menyajikan daftar download dengan kontrol pause/resume.
"""

from __future__ import annotations

import asyncio
import os
import sys
import threading
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QAction
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from .capture_server import CaptureServer
from .config import Config
from .engine import Aria2Engine

POLL_MS = 500
SINGLE_INSTANCE_NAME = "idmlike_gui"

_STATUS_LABEL = {
    "active": "Mengunduh",
    "waiting": "Antri",
    "paused": "Dijeda",
    "error": "Gagal",
    "complete": "Selesai",
    "removed": "Dihapus",
}


def _human(n: int | float) -> str:
    """Format ukuran byte jadi B/KB/MB/GB."""
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} TB"


def _speed(n: int) -> str:
    return _human(n) + "/s"


def _eta(remaining: int, speed: int) -> str:
    if speed <= 0 or remaining <= 0:
        return "—"
    secs = int(remaining / speed)
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}j {m:02d}m"
    if m:
        return f"{m}m {s:02d}s"
    return f"{s}s"


class AddDialog(QDialog):
    """Dialog tambah download: URL + folder tujuan + nama file opsional."""

    def __init__(self, parent: QWidget | None, config: Config) -> None:
        super().__init__(parent)
        self.setWindowTitle("Tambah Download")
        self.setMinimumWidth(520)

        self.url_edit = QLineEdit(self)
        self.url_edit.setPlaceholderText("https://contoh.com/file.zip")
        self.dir_edit = QLineEdit(config.download_dir, self)
        self.fname_edit = QLineEdit(self)
        self.fname_edit.setPlaceholderText("(opsional) nama file tujuan")

        browse = QPushButton("Telusuri…", self)
        browse.clicked.connect(self._browse)

        dir_row = QHBoxLayout()
        dir_row.addWidget(self.dir_edit, 1)
        dir_row.addWidget(browse)

        form = QFormLayout()
        form.addRow("URL:", self.url_edit)
        form.addRow("Folder:", dir_row)
        form.addRow("Nama file:", self.fname_edit)

        ok = QPushButton("Unduh", self)
        ok.setDefault(True)
        ok.clicked.connect(self.accept)
        cancel = QPushButton("Batal", self)
        cancel.clicked.connect(self.reject)
        btns = QHBoxLayout()
        btns.addStretch(1)
        btns.addWidget(cancel)
        btns.addWidget(ok)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addLayout(btns)
        self.url_edit.setFocus()

    def _browse(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Pilih Folder Tujuan", self.dir_edit.text())
        if d:
            self.dir_edit.setText(d)

    def result_data(self) -> tuple[str, str, str]:
        return self.url_edit.text().strip(), self.dir_edit.text().strip(), self.fname_edit.text().strip()


class MainWindow(QMainWindow):
    """Jendela utama IDMLike."""

    capture_failed = Signal(str)

    def __init__(self, config: Config | None = None) -> None:
        super().__init__()
        self.config = config or Config.load()
        self.engine = Aria2Engine(self.config)
        self.capture = CaptureServer(self.engine)
        self._capture_thread: threading.Thread | None = None
        self._shutting_down = False
        self._rows: dict[str, int] = {}
        self.capture_failed.connect(self._on_capture_failed)

        self.setWindowTitle("IDMLike")
        self.resize(900, 560)
        self._build_ui()
        self._apply_qss()
        self._start_backends()

        self._timer = QTimer(self)
        self._timer.setInterval(POLL_MS)
        self._timer.timeout.connect(self._refresh)
        self._timer.start()

    # ------------------------------------------------------------- UI
    def _build_ui(self) -> None:
        toolbar = QToolBar("Utama", self)
        toolbar.setMovable(False)
        self.addToolBar(toolbar)

        self.add_action = QAction("+ Tambah", self)
        self.add_action.setShortcut("Ctrl+N")
        self.add_action.triggered.connect(self._on_add)
        toolbar.addAction(self.add_action)
        toolbar.addSeparator()

        self.pause_act = QAction("Pause", self)
        self.pause_act.triggered.connect(self._on_pause)
        toolbar.addAction(self.pause_act)
        self.resume_act = QAction("Resume", self)
        self.resume_act.triggered.connect(self._on_resume)
        toolbar.addAction(self.resume_act)
        self.cancel_act = QAction("Batal", self)
        self.cancel_act.triggered.connect(self._on_cancel)
        toolbar.addAction(self.cancel_act)
        self.del_act = QAction("Hapus", self)
        self.del_act.triggered.connect(self._on_delete)
        toolbar.addAction(self.del_act)
        self.open_act = QAction("Buka Folder", self)
        self.open_act.triggered.connect(self._on_open_dir)
        toolbar.addAction(self.open_act)
        toolbar.addSeparator()

        self.conn_spin = QSpinBox(self)
        self.conn_spin.setRange(1, 16)
        self.conn_spin.setValue(self.config.max_connections)
        self.conn_spin.setToolTip("Jumlah koneksi paralel per unduhan")
        self.conn_spin.valueChanged.connect(self._on_conn_changed)
        toolbar.addWidget(self.conn_spin)

        self.table = QTableWidget(0, 6, self)
        self.table.setHorizontalHeaderLabels(["Nama File", "Ukuran", "Progress", "Kecepatan", "Status", "Waktu Tersisa"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        hdr = self.table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.Stretch)
        for col in (1, 2, 3, 4, 5):
            hdr.setSectionResizeMode(col, QHeaderView.ResizeToContents)
        self.table.doubleClicked.connect(lambda _i: self._on_open_dir())
        self.setCentralWidget(self.table)

        self.statusBar().showMessage("Siap")
        self.show()

    def _apply_qss(self) -> None:
        self.setStyleSheet(
            """
            QToolBar { background: #2b2b2b; border: none; padding: 2px; spacing: 2px; }
            QToolBar QToolButton { color: #ddd; background: #3c3c3c; border-radius: 4px; padding: 4px 10px; }
            QToolBar QToolButton:hover { background: #4a4a4a; }
            QTableWidget { gridline-color: #333; }
            QHeaderView::section { background: #3c3c3c; color: #ddd; padding: 4px; border: none; }
            QSpinBox { background: #3c3c3c; color: #ddd; border: 1px solid #555; border-radius: 4px; padding: 2px 6px; }
            QStatusBar { color: #aaa; }
            """
        )

    # ---------------------------------------------------------------- backend
    def _start_backends(self) -> None:
        try:
            self.engine.start()
        except RuntimeError as exc:
            QMessageBox.critical(self, "IDMLike", f"Gagal menjalankan aria2c:\n{exc}")
            sys.exit(1)
        self._capture_thread = threading.Thread(
            target=self._run_capture, daemon=True, name="capture-server"
        )
        self._capture_thread.start()

    def _run_capture(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self.capture.start())
        except OSError as exc:
            self.capture_failed.emit(f"Server capture gagal start: {exc}")
            loop.close()
            return
        loop.run_forever()
        loop.close()

    def _stop_backends(self) -> None:
        self._shutting_down = True
        self._timer.stop()
        if self._capture_thread is not None and self._capture_thread.is_alive():
            loop = self.capture.loop
            if loop is not None and loop.is_running():

                async def _stop_and_quit() -> None:
                    await self.capture.stop()
                    loop.stop()

                fut = asyncio.run_coroutine_threadsafe(_stop_and_quit(), loop)
                try:
                    fut.result(timeout=2.0)
                except Exception:
                    pass
                self._capture_thread.join(timeout=2.0)
        self.engine.stop()

    def _on_capture_failed(self, message: str) -> None:
        self.statusBar().showMessage(message)
        self.statusBar().setStyleSheet("color: #e06c75;")

    # ----------------------------------------------------------------- aksi
    def _selected_gid(self) -> str | None:
        row = self.table.currentRow()
        if row < 0:
            return None
        gid = self.table.item(row, 0).data(Qt.UserRole) if self.table.item(row, 0) else None
        return str(gid) if gid else None

    def _on_add(self) -> None:
        dlg = AddDialog(self, self.config)
        if dlg.exec() != QDialog.Accepted:
            return
        url, directory, fname = dlg.result_data()
        if not url:
            QMessageBox.warning(self, "IDMLike", "URL tidak boleh kosong.")
            return
        if directory and not Path(directory).is_dir():
            QMessageBox.warning(self, "IDMLike", "Folder tujuan tidak ditemukan.")
            return
        try:
            self.engine.add_download(url, filename=fname or None, directory=directory or None)
        except Exception as exc:
            QMessageBox.critical(self, "IDMLike", f"Gagal menambah unduhan:\n{exc}")

    def _on_pause(self) -> None:
        gid = self._selected_gid()
        if gid:
            self.engine.pause(gid)

    def _on_resume(self) -> None:
        gid = self._selected_gid()
        if gid:
            self.engine.resume(gid)

    def _on_cancel(self) -> None:
        gid = self._selected_gid()
        if gid:
            self.engine.cancel(gid)

    def _on_delete(self) -> None:
        gid = self._selected_gid()
        if gid:
            self.engine.cancel(gid)
            self.engine.remove_result(gid)

    def _on_open_dir(self) -> None:
        gid = self._selected_gid()
        if gid:
            self.engine.open_dir(gid)

    def _on_conn_changed(self, value: int) -> None:
        self.config.max_connections = value
        self.config.save()

    # ----------------------------------------------------------------- polling
    def _refresh(self) -> None:
        items = self.engine.snapshot()
        active = 0
        total_speed = 0
        rows: dict[str, int] = {}

        self.table.setRowCount(len(items))
        for i, it in enumerate(items):
            gid = str(it["gid"])
            rows[gid] = i
            status = str(it["status"])

            name = QTableWidgetItem(str(it["filename"]))
            name.setData(Qt.UserRole, gid)
            total = int(it["total"])
            completed = int(it["completed"])
            speed = int(it["speed"])
            if status == "active":
                active += 1
                total_speed += speed

            size_item = QTableWidgetItem(_human(total))
            size_item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)

            bar = QProgressBar(self)
            bar.setRange(0, 100)
            bar.setValue(min(100, int(it["percent"])))
            bar.setFormat(f"{it['percent']:.1f}%")

            speed_item = QTableWidgetItem(_speed(speed) if status in ("active",) else "—")
            speed_item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)

            if status == "error":
                status_label = str(it["error"]) or "Gagal"
            else:
                status_label = _STATUS_LABEL.get(status, status)
            st_item = QTableWidgetItem(status_label)

            eta_item = QTableWidgetItem(_eta(total - completed, speed) if status == "active" else "—")
            eta_item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)

            self.table.setItem(i, 0, name)
            self.table.setItem(i, 1, size_item)
            self.table.setCellWidget(i, 2, bar)
            self.table.setItem(i, 3, speed_item)
            self.table.setItem(i, 4, st_item)
            self.table.setItem(i, 5, eta_item)

        self._rows = rows
        self.statusBar().showMessage(
            f"{active} unduhan aktif · total {_speed(total_speed)}"
        )

    # ----------------------------------------------------------------- close
    def closeEvent(self, event) -> None:  # noqa: N802 (Qt override)
        self._stop_backends()
        super().closeEvent(event)


class _SingleInstance:
    """Cegah dua instance GUI sekaligus; fokuskan yang sudah ada."""

    def __init__(self, name: str) -> None:
        self._name = name
        self._server: QLocalServer | None = None
        self.is_primary = False

    def acquire(self) -> bool:
        sock = QLocalSocket()
        sock.connectToServer(self._name)
        if sock.waitForConnected(300):
            sock.close()
            return False
        self._server = QLocalServer()
        self._server.setSocketOptions(QLocalServer.UserAccessOption)
        if not self._server.listen(self._name):
            QLocalServer.removeServer(self._name)
            self._server.listen(self._name)
        self.is_primary = True
        return True


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("IDMLike")
    app.setOrganizationName("idmlike")

    single = _SingleInstance(SINGLE_INSTANCE_NAME)
    if not single.acquire():
        print("IDMLike sudah berjalan.")
        return 0

    config = Config.load()
    window = MainWindow(config)
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())