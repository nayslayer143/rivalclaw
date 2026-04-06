#!/usr/bin/env python3
"""
Strategy Lab hypothesis generator — reads diagnostic reports and generates
bounded mutation hypotheses using Ollama (qwen3:30b).

Allowed mutations (v1 genome):
  - threshold_adjust, filter_addition, filter_removal, param_tune,
    regime_split, abstain_rule

Forbidden: full_rewrite, new_strategy, risk_override, multi_model
"""
from __future__ import annotations

import datetime
import json
import os
from pathlib import Path

import requests

try:
    from catalog_reader import StrategyCatalog
except ImportError:
    StrategyCatalog = None  # type: ignore[assignment,misc]

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("STRATEGY_LAB_MODEL", "gemma4:26b")
MAX_HYPOTHESES = int(os.environ.get("STRATEGY_LAB_MAX_HYPOTHESES", "5"))
MEMORY_PATH = Path(__file__).resolve().parent / "memory.json"
REGISTRY_PATH = Path(__file__).resolve().parent.parent / "strategy_registry.json"

ALLOWED_MUTATIONS = {
    "threshold_adjust", "filter_addition", "filter_removal",
    "param_tune", "regime_split", "abstain_rule",
}


def _load_memory() -> list[dict]:
    """Load lessons learned from memory.json."""
    if not MEMORY_PATH.exists():
        return []
    try:
        data = json.loads(MEMORY_PATH.read_text())
        return data.get("lessons", [])
    except (json.JSONDecodeError, KeyError):
        return []


def _load_registry() -> dict:
    """Load strategy registry."""
    if not REGISTRY_PATH.exists():
        return {"strategies": []}
    try:
        return json.loads(REGISTRY_PATH.read_text())
    except json.JSONDecodeError:
        return {"strategies": []}


def _build_prompt(diagnostic: dict, lessons: list[dict], registry: dict) -> str:
    """Build the LLM prompt from diagnostic data + memory."""
    lesson_text = ""
    if lessons:
        recent = lessons[-10:]  # Last 10 lessons
        lesson_text = "\n\nPAST LESSONS (do NOT repeat these failed experiments):\n"
        for les in recent:
            lesson_text += f"- {les.get('mutation', '?')}: {les.get('outcome', '?')}\n"

    strategies_text = ""
    for s in registry.get("strategies", []):
        if s.get("status") == "production":
            strategies_text += f"- {s['id']}: params={json.dumps(s.get('params', {}))}\n"

    return f"""You are a quantitative trading strategy researcher. Analyze this diagnostic report and generate {MAX_HYPOTHESES} improvement hypotheses.

DIAGNOSTIC REPORT:
{json.dumps(diagnostic, indent=2, default=str)}

CURRENT PRODUCTION STRATEGIES:
{strategies_text}
{lesson_text}

RULES:
1. Each hypothesis must be a bounded mutation of an existing strategy
2. Allowed mutation types: threshold_adjust, filter_addition, filter_removal, param_tune, regime_split, abstain_rule
3. FORBIDDEN: full_rewrite, new_strategy, risk_override, multi_model
4. param_tune changes must be within ±20% of current value
5. Focus on the WORST performing strategies or biggest missed opportunities
6. Each hypothesis must explain expected benefit and overfit risk

Return ONLY a JSON array of hypothesis objects with these fields:
- id: "hyp-YYYYMMDD-NNN"
- observation: what the data shows
- proposed_change: specific change to make
- mutation_type: one of the allowed types
- parent_version: which strategy version to modify (e.g. "arbitrage_v1.0")
- candidate_version: proposed new version (e.g. "arbitrage_v1.1")
- expected_benefit: expected improvement
- overfit_risk: low/medium/high with explanation
- evaluation_plan: how to test this

/no_think
Return ONLY the JSON array, no other text."""


def _call_ollama(prompt: str) -> str | None:
    """Call Ollama API. Returns raw text response."""
    try:
        resp = requests.post(
            f"{OLLAMA_URL}/api/generate",
            json={"model": OLLAMA_MODEL, "prompt": prompt, "stream": False,
                  "options": {"temperature": 0.7, "num_predict": 4096}},
            timeout=120,
        )
        resp.raise_for_status()
        return resp.json().get("response", "")
    except Exception as e:
        print(f"[strategy_lab/hypothesize] Ollama error: {e}")
        return None


def _parse_hypotheses(raw: str) -> list[dict]:
    """Parse JSON array from LLM response. Tolerant of markdown fences."""
    if not raw:
        return []
    # Strip markdown code fences if present
    text = raw.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        lines = [l for l in lines if not l.strip().startswith("```")]
        text = "\n".join(lines)
    try:
        arr = json.loads(text)
        if isinstance(arr, list):
            return arr
    except json.JSONDecodeError:
        # Try to find JSON array in the text
        start = text.find("[")
        end = text.rfind("]")
        if start >= 0 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                pass
    return []


def _validate_hypothesis(hyp: dict) -> bool:
    """Check hypothesis is valid, bounded, and respects autonomy boundaries."""
    mutation = hyp.get("mutation_type", "")
    if mutation not in ALLOWED_MUTATIONS:
        return False
    # Mandate 6: Autonomy boundary check
    try:
        from strategy_lab.governor import validate_mutation
        valid, _ = validate_mutation(hyp)
        if not valid:
            return False
    except ImportError:
        pass
    required = ["id", "observation", "proposed_change", "parent_version",
                 "candidate_version", "expected_benefit"]
    return all(hyp.get(k) for k in required)


def _catalog_suggestions(diagnostics: dict) -> list[dict]:
    """Cross-reference catalog with active strategies to find untested candidates.

    Returns catalog entries for Kalshi-eligible strategies that RivalClaw
    isn't currently running, sorted by complexity_score (lower = easier).
    """
    if StrategyCatalog is None:
        return []
    try:
        catalog = StrategyCatalog()
    except Exception:
        return []
    if catalog.count == 0:
        return []

    # Collect names/families of strategies RivalClaw is already testing
    active_names: set[str] = set()
    for s in diagnostics.get("strategies", []):
        name = s.get("name", "").lower()
        family = s.get("family", "").lower()
        if name:
            active_names.add(name)
        if family:
            active_names.add(family)

    # Also pull from the strategy registry for broader coverage
    registry = _load_registry()
    for s in registry.get("strategies", []):
        sid = s.get("id", "").lower()
        fam = s.get("family", "").lower()
        if sid:
            active_names.add(sid)
        if fam:
            active_names.add(fam)

    # Filter to Kalshi candidates not already active
    candidates = []
    for entry in catalog.kalshi_candidates():
        entry_name = entry.get("name", "").lower()
        entry_family = entry.get("family", "").lower()
        entry_id = entry.get("strategy_id", "").lower()
        # Skip if any active name overlaps with this catalog entry
        if any(tok in entry_name or tok in entry_family or tok in entry_id
               for tok in active_names if tok):
            continue
        candidates.append(entry)

    # Sort by complexity_score ascending (simpler first)
    candidates.sort(key=lambda e: e.get("complexity_score", 999))
    return candidates


def generate_hypotheses(diagnostic: dict) -> list[dict]:
    """Generate bounded hypotheses from a diagnostic report.

    Each hypothesis dict may include a ``catalog_suggestions`` key with
    untested Kalshi-eligible strategies from the openclaw-strategies catalog,
    sorted by complexity (simplest first).
    """
    lessons = _load_memory()
    registry = _load_registry()
    prompt = _build_prompt(diagnostic, lessons, registry)

    # Gather catalog suggestions (non-blocking — empty list on failure)
    suggestions = _catalog_suggestions(diagnostic)
    if suggestions:
        print(f"[strategy_lab/hypothesize] {len(suggestions)} untested catalog strategies available")

    # Try twice on malformed output
    for attempt in range(2):
        raw = _call_ollama(prompt)
        if raw is None:
            print(f"[strategy_lab/hypothesize] Ollama returned nothing (attempt {attempt + 1})")
            continue
        hypotheses = _parse_hypotheses(raw)
        valid = [h for h in hypotheses if _validate_hypothesis(h)]
        if valid:
            # Cap at MAX_HYPOTHESES
            valid = valid[:MAX_HYPOTHESES]
            today = datetime.date.today().isoformat().replace("-", "")
            for i, h in enumerate(valid):
                if not h.get("id", "").startswith("hyp-"):
                    h["id"] = f"hyp-{today}-{i + 1:03d}"
                # Attach top catalog suggestions for reference
                if suggestions:
                    h["catalog_suggestions"] = [
                        {
                            "strategy_id": s["strategy_id"],
                            "name": s["name"],
                            "family": s.get("family", ""),
                            "complexity_score": s.get("complexity_score"),
                            "alpha_type": s.get("alpha_type", ""),
                        }
                        for s in suggestions[:5]
                    ]
            print(f"[strategy_lab/hypothesize] Generated {len(valid)} hypotheses")
            return valid
        print(f"[strategy_lab/hypothesize] No valid hypotheses parsed (attempt {attempt + 1})")

    print("[strategy_lab/hypothesize] Failed to generate hypotheses after 2 attempts")
    return []


if __name__ == "__main__":
    # Load most recent diagnostic
    reports_dir = Path(__file__).resolve().parent / "reports"
    diags = sorted(reports_dir.glob("diagnostic-*.json"), reverse=True)
    if not diags:
        print("No diagnostic reports found. Run diagnose.py first.")
    else:
        diag = json.loads(diags[0].read_text())
        hyps = generate_hypotheses(diag)
        print(json.dumps(hyps, indent=2))
