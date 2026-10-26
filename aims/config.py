"""Runtime configuration: where data lives and which port to use.

Private data is kept outside the repository (docs/DECISIONS.md D2).
Environment overrides: AIMS_DATA_DIR, AIMS_PORT.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_PORT = 8765


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Config:
    data_dir: Path
    port: int = DEFAULT_PORT

    @property
    def db_path(self) -> Path:
        return self.data_dir / "aims.db"

    @property
    def log_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def backup_dir(self) -> Path:
        return self.data_dir / "backups"

    @property
    def documents_dir(self) -> Path:
        return self.data_dir / "documents"

    @property
    def tmp_dir(self) -> Path:
        return self.data_dir / "tmp"

    @property
    def lock_path(self) -> Path:
        return self.data_dir / "server.lock"

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def ensure_dirs(self) -> None:
        for d in (self.data_dir, self.log_dir, self.backup_dir, self.documents_dir, self.tmp_dir):
            d.mkdir(parents=True, exist_ok=True)


def default_data_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "AIMS"


def load_config() -> Config:
    raw_dir = os.environ.get("AIMS_DATA_DIR")
    data_dir = Path(raw_dir) if raw_dir else default_data_dir()
    raw_port = os.environ.get("AIMS_PORT", str(DEFAULT_PORT))
    try:
        port = int(raw_port)
    except ValueError:
        raise ConfigError(f"AIMS_PORT must be a number, got {raw_port!r}") from None
    if not 1024 <= port <= 65535:
        raise ConfigError(f"AIMS_PORT must be between 1024 and 65535, got {port}")
    return Config(data_dir=data_dir.resolve(), port=port)
