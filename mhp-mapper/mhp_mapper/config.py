"""Settings loaded from environment variables and an optional .env file."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if value and key not in os.environ:
            os.environ[key] = value


_load_dotenv(ROOT / ".env")


@dataclass
class Settings:
    regrid_token: str = field(default_factory=lambda: os.environ.get("REGRID_API_TOKEN", ""))
    google_key: str = field(default_factory=lambda: os.environ.get("GOOGLE_MAPS_API_KEY", ""))
    anthropic_key: str = field(default_factory=lambda: os.environ.get("ANTHROPIC_API_KEY", ""))
    claude_model: str = field(default_factory=lambda: os.environ.get("MHP_CLAUDE_MODEL", "claude-opus-5-5"))
    overture_release: str = field(
        default_factory=lambda: os.environ.get("MHP_OVERTURE_RELEASE", "2026-10-21.0")
    )
    data_dir: Path = field(default_factory=lambda: Path(os.environ.get("MHP_DATA_DIR", ROOT / "data")))
    out_dir: Path = field(default_factory=lambda: Path(os.environ.get("MHP_OUT_DIR", ROOT / "out")))
    overpass_urls: tuple[str, ...] = (
        "https://overpass-api.de/api/interpreter",
        "https://overpass.kumi.systems/api/interpreter",
        "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
    )

    # --- Classification thresholds (tune after the pilot) ---
    min_units_for_park: int = 4          # fewer MH-like footprints than this -> not a park
    min_mh_share: float = 0.5            # MH-like footprints / all footprints on the parcel
    registry_tolerance: float = 0.15     # |our count - registry count| / registry count
    single_lot_max_acres: float = 0.5    # tiny parcel with 1-2 homes = single lot / subdivision

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.out_dir.mkdir(parents=True, exist_ok=True)


def get_settings() -> Settings:
    s = Settings()
    s.ensure_dirs()
    return s
