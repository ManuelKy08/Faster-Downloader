"""Konfigurasi IDMLike — load/save JSON ke ~/.config/idmlike/config.json."""

from __future__ import annotations

import dataclasses
import json
import os
from pathlib import Path

CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", "~/.config")).expanduser() / "idmlike"
CONFIG_FILE = CONFIG_DIR / "config.json"


@dataclasses.dataclass
class Config:
    download_dir: str = "~/Downloads"
    max_connections: int = 16

    def __post_init__(self) -> None:
        self.download_dir = os.path.expanduser(self.download_dir)
        self.max_connections = max(1, min(int(self.max_connections), 16))

    def save(self) -> None:
        """Simpan konfigurasi ke disk (buat folder bila belum ada)."""
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        data = dataclasses.asdict(self)
        data["download_dir"] = os.path.expanduser(data["download_dir"])
        CONFIG_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")

    @classmethod
    def load(cls) -> "Config":
        """Baca dari disk; kembali ke default bila file tidak ada/rusak."""
        if CONFIG_FILE.exists():
            try:
                data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
                return cls(
                    download_dir=str(data.get("download_dir", "~/Downloads")),
                    max_connections=int(data.get("max_connections", 16)),
                )
            except (json.JSONDecodeError, ValueError, TypeError):
                pass
        return cls()