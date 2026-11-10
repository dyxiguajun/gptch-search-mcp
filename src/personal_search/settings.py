from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from platformdirs import user_data_path


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    request_interval: float = 0.8

    @classmethod
    def load(cls, data_dir: str | Path | None = None):
        path = data_dir or os.getenv("GPTCH_DATA_DIR") or user_data_path("GPTCHSearch", "XiGua")
        return cls(
            Path(path).expanduser().resolve(),
            max(0.2, float(os.getenv("GPTCH_REQUEST_INTERVAL", "0.8"))),
        )

    def prepare(self):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        for name in ("auth", "raw", "searches", "logs", "backups"):
            (self.data_dir / name).mkdir(exist_ok=True)
        if os.name != "nt":
            self.data_dir.chmod(0o700)
            (self.data_dir / "auth").chmod(0o700)
