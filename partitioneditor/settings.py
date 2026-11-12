"""Einstellungen (%APPDATA%\\PartitionEditor\\settings.json).

``allow_internal`` (interne Platten freischalten) lässt sich nur in der GUI ändern – die CLI
liest den Wert nur. Für Tests kann der Pfad per Umgebungsvariable PARTITIONEDITOR_SETTINGS
umgelenkt werden.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path


def settings_path() -> Path:
    env = os.environ.get("PARTITIONEDITOR_SETTINGS")
    if env:
        return Path(env)
    base = os.environ.get("APPDATA") or str(Path.home())
    return Path(base) / "PartitionEditor" / "settings.json"


@dataclass
class Settings:
    allow_internal: bool = False

    @classmethod
    def load(cls) -> "Settings":
        try:
            data = json.loads(settings_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls()
        s = cls()
        s.allow_internal = data.get("allow_internal") is True
        return s

    def save(self) -> None:
        p = settings_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
