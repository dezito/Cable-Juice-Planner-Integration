"""Learn a conservative charging-power cap from stable, phase-matched SOC segments."""
from __future__ import annotations

from typing import Any

SOC_POWER_BAND_SIZE = 10
SOC_POWER_MIN_SESSIONS = 3
SOC_POWER_SESSION_GAP = 30 * 60
SOC_POWER_STABILITY_RANGE = 0.1
SOC_POWER_TAPER_RATIO = 0.9
SOC_POWER_HISTORY_DAYS = 180
SOC_POWER_MAX_SEGMENTS = 2000


def _stable(segment: dict[str, Any]) -> bool:
    try:
        kw = float(segment["kw"])
        low = float(segment["power_min_w"])
        high = float(segment["power_max_w"])
        return (
            kw > 0
            and low > 0
            and high >= low
            and high - low <= kw * 1000 * SOC_POWER_STABILITY_RANGE
        )
    except (KeyError, TypeError, ValueError):
        return False


def merge_power_segments(
    old: list[dict[str, Any]],
    new: list[dict[str, Any]],
    window_start: float,
    now: float,
) -> list[dict[str, Any]]:
    """Replace recent raw-history segments and retain a bounded rolling history."""
    retained = []
    for segment in old:
        if not isinstance(segment, dict) or segment.get("source") != "history" or segment.get("phase_observed") is not True:
            continue
        try:
            if float(segment["end"]) < window_start:
                retained.append(segment)
        except (KeyError, TypeError, ValueError):
            continue
    merged = retained + [
        segment for segment in new
        if segment.get("source") == "history" and segment.get("phase_observed") is True
    ]
    cutoff = now - SOC_POWER_HISTORY_DAYS * 86400
    unique = {
        (segment["start"], segment["end"], segment["phases"]): segment
        for segment in merged
        if segment.get("start", 0) >= cutoff
    }
    return sorted(unique.values(), key=lambda segment: segment["start"])[-SOC_POWER_MAX_SEGMENTS:]


def _sessions(segments: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    sessions: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    last_end = 0.0
    last_soc_end = 0.0
    last_phases = 0
    for segment in sorted(segments, key=lambda item: item["start"]):
        try:
            start = float(segment["start"])
            end = float(segment["end"])
            phases = int(segment["phases"])
            soc_start = float(segment["soc_start"])
            soc_end = float(segment["soc_end"])
        except (KeyError, TypeError, ValueError):
            continue
        if (
            not _stable(segment)
            or segment.get("source") != "history"
            or segment.get("phase_observed") is not True
            or phases not in (1, 3)
            or end <= start
            or not 0 <= soc_start < soc_end <= 100
        ):
            continue
        if current and (
            phases != last_phases
            or start > last_end + SOC_POWER_SESSION_GAP
            or soc_start < last_soc_end - 1.0
        ):
            sessions.append(current)
            current = []
        current.append(segment)
        last_end = max(last_end, end)
        last_soc_end = soc_end
        last_phases = phases
    if current:
        sessions.append(current)
    return sessions


def build_soc_power_model(segments: list[dict[str, Any]]) -> dict[str, Any]:
    """Learn a band's cap only when it repeatedly tapers within distinct stable sessions."""
    samples: dict[tuple[int, int], dict[float, float]] = {}
    tapered: dict[tuple[int, int], dict[float, float]] = {}

    for session in _sessions(segments):
        session_id = float(session[0]["start"])
        by_band: dict[tuple[int, int], list[dict[str, Any]]] = {}
        for segment in session:
            midpoint = (float(segment["soc_start"]) + float(segment["soc_end"])) / 2
            band = min(int(midpoint // SOC_POWER_BAND_SIZE) * SOC_POWER_BAND_SIZE, 90)
            key = (int(segment["phases"]), band)
            by_band.setdefault(key, []).append(segment)
            samples.setdefault(key, {})[session_id] = 0.0

        for (phases, band), rows in by_band.items():
            candidate_caps = []
            for segment in rows:
                start_soc = float(segment["soc_start"])
                midpoint = (start_soc + float(segment["soc_end"])) / 2
                lower_power = [
                    float(previous["kw"])
                    for previous in session
                    if int(previous["phases"]) == phases
                    and float(previous["end"]) <= float(segment["start"])
                    and float(previous["soc_end"]) <= start_soc + 1.0
                    and (float(previous["soc_start"]) + float(previous["soc_end"])) / 2
                    <= midpoint - SOC_POWER_BAND_SIZE
                ]
                if lower_power and float(segment["kw"]) < max(lower_power) * SOC_POWER_TAPER_RATIO:
                    candidate_caps.append(float(segment["kw"]))
            if candidate_caps:
                tapered.setdefault((phases, band), {})[session_id] = min(candidate_caps)

    rows = []
    for phases, band in sorted(set(samples) | set(tapered)):
        observations = tapered.get((phases, band), {})
        cap = min(observations.values()) if len(observations) >= SOC_POWER_MIN_SESSIONS else None
        rows.append({
            "phases": phases,
            "soc_start": band,
            "soc_end": band + SOC_POWER_BAND_SIZE,
            "sessions": len(observations),
            "sample_sessions": len(samples.get((phases, band), {})),
            "cap_kw": round(cap, 2) if cap is not None else None,
        })
    return {
        "status": "ready" if any(row["cap_kw"] is not None for row in rows) else "learning" if rows else "no_data",
        "min_sessions": SOC_POWER_MIN_SESSIONS,
        "bands": rows,
    }


def planning_power_cap(model: dict[str, Any], phases: int, soc_start: float, soc_end: float, base_kw: float) -> float:
    """Clamp an interval to the lowest learned cap in every SOC band it crosses."""
    low, high = sorted((max(0.0, min(100.0, soc_start)), max(0.0, min(100.0, soc_end))))
    if high <= low:
        high = min(low + 0.001, 100.0)
    cap = base_kw
    for row in model.get("bands", []):
        if (
            row.get("phases") == phases
            and row.get("cap_kw") is not None
            and low < row["soc_end"]
            and high > row["soc_start"]
        ):
            cap = min(cap, float(row["cap_kw"]))
    return cap
