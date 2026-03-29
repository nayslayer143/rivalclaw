"""
Strategy catalog reader — loads openclaw-strategies catalog for reference.

Usage:
    from catalog_reader import StrategyCatalog
    catalog = StrategyCatalog()
    pm_strats = catalog.by_family("prediction_market_native")
    kalshi = catalog.kalshi_candidates()
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

CATALOG_PATH = Path(__file__).parent / "data" / "strategy-catalog.json"


class StrategyCatalog:
    def __init__(self, path: Path | str = CATALOG_PATH):
        self._strategies: list[dict[str, Any]] = []
        self._index: dict[str, dict[str, Any]] = {}
        self._version: str = ""
        self._generated_at: str = ""
        self._load(Path(path))

    def _load(self, path: Path) -> None:
        if not path.exists():
            return
        raw = json.loads(path.read_text())
        self._strategies = raw.get("strategies", [])
        self._index = {s["strategy_id"]: s for s in self._strategies}
        self._version = raw.get("version", "")
        self._generated_at = raw.get("generated_at", "")

    @property
    def version(self) -> str: return self._version

    @property
    def generated_at(self) -> str: return self._generated_at

    @property
    def count(self) -> int: return len(self._strategies)

    def all(self) -> list[dict]: return list(self._strategies)

    def get(self, strategy_id: str) -> dict | None:
        return self._index.get(strategy_id)

    def by_family(self, family: str) -> list[dict]:
        return [s for s in self._strategies if s.get("family") == family]

    def by_status(self, status: str) -> list[dict]:
        return [s for s in self._strategies if s.get("status") == status]

    def by_alpha_type(self, alpha_type: str) -> list[dict]:
        return [s for s in self._strategies if s.get("alpha_type") == alpha_type]

    def for_venue(self, venue_type: str) -> list[dict]:
        return [s for s in self._strategies if venue_type in s.get("venue_types", [])]

    def for_instrument(self, instrument_type: str) -> list[dict]:
        return [s for s in self._strategies if instrument_type in s.get("instrument_types", [])]

    def search(self, query: str) -> list[dict]:
        q = query.lower()
        return [
            s for s in self._strategies
            if q in s.get("name", "").lower()
            or q in s.get("alpha_hypothesis", "").lower()
            or q in s.get("family", "").lower()
        ]

    def kalshi_candidates(self) -> list[dict]:
        """Strategies relevant to Kalshi prediction markets."""
        return [
            s for s in self._strategies
            if "prediction_markets" in s.get("instrument_types", [])
            and s.get("status") not in ("retired", "banned")
        ]

    def families(self) -> list[str]:
        return sorted(set(s.get("family", "") for s in self._strategies))

    def summary(self) -> dict:
        return {
            "version": self._version,
            "generated_at": self._generated_at,
            "total": self.count,
            "families": len(self.families()),
            "kalshi_candidates": len(self.kalshi_candidates()),
        }
