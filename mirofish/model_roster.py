"""Pure helpers for the account-scoped model roster.

Source: Mirasim 0.0.303, Resources/server.cjs, FKt (UTF-8 byte 779795)
for normalization and rUe/nUe (2607718/2608110) for Claude [1m] IDs.
Numeric validation additionally rejects booleans and non-finite values.
No built-in catalog is used as an authorization fallback.
"""

from __future__ import annotations

import math
from typing import Any

_ONE_M = 1_000_000
_SUFFIX = "[1m]"


def _positive_number(value: Any) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and 0 < value < math.inf)


def normalize_roster(body: Any) -> dict[str, Any] | None:
    """Keep only FKt's fields; an entirely invalid/empty roster is unavailable."""
    if not isinstance(body, dict):
        return None
    version, agents = body.get("version"), body.get("agents")
    if not isinstance(version, str) or not version.strip() or not isinstance(agents, dict):
        return None
    normalized = {}
    for agent, models in agents.items():
        if not isinstance(models, list):
            continue
        rows = []
        for model in models:
            if not isinstance(model, dict):
                continue
            model_id, window = model.get("id"), model.get("contextWindow")
            if not isinstance(model_id, str) or not model_id.strip() or not _positive_number(window):
                continue
            model_id = model_id.strip().lower()
            label = model.get("label")
            row = {
                "id": model_id,
                "label": label.strip().lower() if isinstance(label, str) and label.strip() else model_id,
                "contextWindow": window,
            }
            if _positive_number(model.get("maxOutput")):
                row["maxOutput"] = model["maxOutput"]
            ratio = model.get("autoCompactRatio")
            if _positive_number(ratio) and ratio <= 1:
                row["autoCompactRatio"] = ratio
            if isinstance(model.get("effort"), list):
                effort = [value for value in model["effort"]
                          if isinstance(value, str) and value.strip()]
                if effort:
                    row["effort"] = effort
            if model.get("adaptive") is True:
                row["adaptive"] = True
            rows.append(row)
        if rows:
            normalized[agent] = rows
    return {"version": version.strip(), "agents": normalized} if normalized else None


def entries(roster: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Return OpenAI data rows with per-entry, not account-global, capabilities."""
    normalized = normalize_roster(roster)
    if normalized is None:
        return []
    result = []
    for agent, models in normalized["agents"].items():
        for model in models:
            model_id = model["id"]
            if agent == "claude" and model["contextWindow"] >= _ONE_M:
                model_id = model_id.removesuffix(_SUFFIX) + _SUFFIX
            capabilities = {"agent": agent, "context_window": model["contextWindow"]}
            for source, target in (
                ("maxOutput", "max_output"),
                ("autoCompactRatio", "auto_compact_ratio"),
                ("effort", "effort"),
                ("adaptive", "adaptive"),
            ):
                if source in model:
                    capabilities[target] = model[source]
            result.append({
                "id": model_id, "object": "model", "created": 0,
                "owned_by": "mirofish", "display_name": model["label"],
                "capabilities": capabilities,
            })
    return result


def supported_model(roster: dict[str, Any] | None, requested: str) -> str | None:
    """Resolve only advertised IDs; explicit Claude [1m] requires a 1M window."""
    if not isinstance(requested, str) or not requested:
        return None
    exact = None
    for model in entries(roster):
        model_id, capabilities = model["id"], model["capabilities"]
        if capabilities["agent"] == "claude":
            if model_id.endswith(_SUFFIX) and capabilities["context_window"] < _ONE_M:
                continue
            if (requested.startswith("claude-") and not requested.endswith(_SUFFIX)
                    and model_id == requested + _SUFFIX):
                return model_id
        if model_id == requested:
            exact = model_id
    return exact
