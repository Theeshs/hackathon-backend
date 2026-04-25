import requests
import json
import os
from dotenv import load_dotenv
load_dotenv()

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "YOUR_KEY_HERE")
MODEL = "google/gemini-2.0-flash-001"


def call_gemini(prompt: str) -> dict:
    try:
        response = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                "Content-Type": "application/json",
                "HTTP-Referer": "http://localhost:3000",
            },
            json={
                "model": MODEL,
                "messages": [
                    {"role": "system", "content": (
                        "You are an AI air defense decision support system. "
                        "Respond with valid JSON only. No markdown, no text outside the JSON object."
                    )},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.2,
            },
            timeout=15,
        )
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"].strip()
        if content.startswith("```"):
            content = content.split("```")[1]
            if content.startswith("json"):
                content = content[4:]
        return json.loads(content.strip())
    except requests.exceptions.Timeout:
        return {"error": "Gemini request timed out", "fallback": True}
    except json.JSONDecodeError as e:
        return {"error": f"Failed to parse Gemini response: {str(e)}", "fallback": True}
    except Exception as e:
        return {"error": str(e), "fallback": True}


def build_decision_prompt(threat: dict, state_summary: dict, distance_matrix: dict, coverage: dict) -> str:
    base_lines = []
    for bid, info in distance_matrix.items():
        if info["can_intercept"]:
            opts = []
            for atype, opt in info["options"].items():
                weapons_str = ", ".join(opt["weapons"]).replace("_", " ")
                opts.append(
                    f"{atype} ({opt['response_min']}min, {opt['range_km']}km range, "
                    f"{opt['fuel_pct']}% fuel, weapons: {weapons_str})"
                )
            opts_str = " | ".join(opts)
        else:
            opts_str = "OUT OF RANGE / NO ASSETS"
        base_lines.append(
            f"  {bid} ({info['name']}): {info['distance_km']} km away | "
            f"{info['available_count']} aircraft available\n"
            f"    Options: {opts_str}"
        )

    coverage_lines = ""
    if coverage["gaps"]:
        coverage_lines += f"\n  COVERAGE GAPS if selected base deploys: {', '.join(coverage['gaps'])}"
    if coverage["warnings"]:
        coverage_lines += f"\n  WARNINGS: {'; '.join(coverage['warnings'])}"

    north_targets = "\n".join(
        f"  {t['name']} ({t['subtype']}, priority {t['priority']}): ({t['x_km']}, {t['y_km']}) km"
        for t in state_summary.get("protection_targets", []) if t["side"] == "north"
    )

    tx = threat.get("x_km", threat.get("x", 0))
    ty = threat.get("y_km", threat.get("y", 0))

    session = state_summary.get("session_costs", {})
    session_cost_str = f"${session.get('total_usd', 0):,.0f} across {session.get('sorties', 0)} sorties"

    resource_lines = []
    for b in state_summary.get("bases", []):
        inv = b.get("weapons_inventory", {})
        inv_str = ", ".join(f"{k.replace('_',' ')}: {v}" for k, v in inv.items() if v > 0)
        warnings = b.get("resource_warnings", [])
        warn_str = f" ⚠ {'; '.join(warnings)}" if warnings else ""
        resource_lines.append(
            f"  {b['id']}: fuel {b.get('fuel_pct',0)}% ({b.get('fuel_stock_liters',0):,}L) | {inv_str}{warn_str}"
        )

    return f"""You are the AI decision support system for NORTHERN COMMAND air defense.

INCOMING THREAT:
  ID: {threat.get('id')} | Type: {threat.get('type')}
  Position: ({tx:.1f}, {ty:.1f}) km
  Speed: {threat.get('speed', '?')} km/h | Heading: {threat.get('heading', '?')}deg
  ETA to target: {threat.get('eta', '?')}s | Intended target: {threat.get('target_name', 'unknown')}
  Civilian traffic nearby: {threat.get('civilian_nearby', False)}

BASE INTERCEPT ANALYSIS:
{chr(10).join(base_lines)}
{coverage_lines}

NORTH PROTECTION TARGETS:
{north_targets}

BASE ASSET DETAILS:
{json.dumps(state_summary['bases'], indent=2)}

Active deployments: {state_summary['active_deployments']} | Active threats: {state_summary['active_threats']}

RESOURCE STATUS (inventory · fuel · warnings):
{chr(10).join(resource_lines)}

SESSION COSTS SO FAR: {session_cost_str}

Select the optimal base, asset type, AND weapon. Apply economy-of-force principles:

ASSET MATCHING:
- Fighter: Strike aircraft, Fighter jet — air combat, long range (700km), can carry LRM
- Interceptor: Ballistic missile, Cruise missile — fastest (1800km/h), short-range missiles
- Drone: Armed drone threats, low-priority only — do NOT use for ballistic or aircraft threats

WEAPON MATCHING (cost-effectiveness):
- long_range_missile ($1.5M): Ballistic missile, long-range cruise missile — justified by threat value
- short_range_missile ($300K): Most aircraft and slow threats within 200km
- cannon ($2K): Low-speed targets only (armed drones, slow aircraft) — preserve missiles
- armed_drone ($80K): Low-priority armed drone threats — cheapest counter

ECONOMY OF FORCE — always consider:
1. Is the weapon cost proportionate to the threat? Don't fire a $1.5M missile at an armed drone.
2. Check inventory: if a base has ≤ 2 LRMs, avoid using them unless no alternative exists.
3. Check fuel: if base fuel < 30%, prefer drones or ground defense to preserve manned sorties.
4. Session cost context: total spent so far — factor in sustainability.
5. If a cheaper option can intercept, use it; preserve high-value weapons for high-value threats.

Respond ONLY with this JSON object:
{{
  "recommended_base": "<NVB|HRC|BWP>",
  "recommended_base_name": "<full name>",
  "recommended_asset_type": "<fighter|interceptor|drone>",
  "recommended_weapon": "<long_range_missile|short_range_missile|cannon|armed_drone|air_defense>",
  "confidence": <0-100>,
  "reasoning": "<2-3 sentences: why this base, asset type, AND weapon — include cost rationale>",
  "alternatives_rejected": [
    {{"base": "<id>", "reason": "<specific: out of range / no assets / slower / coverage risk / resource concern>"}}
  ],
  "trade_offs": "<1-2 sentences: cost vs effectiveness vs coverage vs sustainability>",
  "estimated_cost_usd": <integer: sortie cost + weapon cost>,
  "cost_rationale": "<1 sentence: why this cost level is proportionate or necessary>",
  "civilian_risk": "<none|low|medium|high>",
  "civilian_note": "<relevant note or empty string>",
  "future_risk": "<1-2 sentences: wave risk, resource depletion risk, coverage after deployment>",
  "alternative_base": "<backup base ID>",
  "priority": "<immediate|urgent|monitor>"
}}"""


def build_forecast_prompt(wave_log: list, state_summary: dict) -> str:
    wave_lines = []
    for i, w in enumerate(wave_log[-10:], 1):
        age_min = round((w.get("now", w["time"]) - w["time"]) / 60000, 1) if "time" in w else "?"
        wave_lines.append(
            f"  Wave {i}: {w.get('count', '?')} threat(s) | targets: {', '.join(w.get('targets', []))} | {age_min} min ago"
        )

    intervals = []
    times = [w["time"] for w in wave_log if "time" in w]
    for i in range(1, len(times)):
        intervals.append((times[i] - times[i - 1]) / 60000)
    avg_interval = round(sum(intervals) / len(intervals), 1) if intervals else None

    base_lines = "\n".join(
        f"  {b['id']} ({b['name']}): {b['available_count']} aircraft ready, avg fuel ~{round(sum(a['fuel_pct'] for a in b['assets']) / max(len(b['assets']), 1))}%"
        for b in state_summary.get("bases", [])
    )

    return f"""You are a tactical air defense intelligence analyst for Northern Command.

WAVE HISTORY (most recent last):
{chr(10).join(wave_lines) if wave_lines else "  No waves recorded yet."}

AVERAGE INTERVAL BETWEEN WAVES: {f"{avg_interval} min" if avg_interval else "insufficient data"}

CURRENT BASE READINESS:
{base_lines}

NORTH PROTECTION TARGETS: Arktholm (capital, priority 10), Valbrek (city, priority 6), Nordvik (city, priority 6)

Analyse the attack pattern and forecast the next wave. Consider: timing intervals, target preferences, escalation trends, and current base readiness gaps.

Respond ONLY with this JSON:
{{
  "next_wave_estimate_min": <integer minutes from now, or null if unknown>,
  "predicted_targets": ["<city name>"],
  "threat_types_expected": ["<type>"],
  "recommended_readiness": "<1-2 sentences: which bases to prioritise, refuel, or hold in reserve>",
  "risk_level": "<low|medium|high|critical>",
  "reasoning": "<1-2 sentences explaining the forecast>"
}}"""


def build_iff_prompt(aircraft: dict) -> str:
    return f"""You are an IFF (Identify Friend or Foe) system for Northern Command.

UNKNOWN AIRCRAFT:
  Callsign: {aircraft.get('callsign', 'UNKNOWN')}
  Position: ({aircraft.get('x', 0):.1f}, {aircraft.get('y', 0):.1f})
  Speed: {aircraft.get('speed', 0)} km/h | Altitude: {aircraft.get('altitude', 0)} m
  Heading: {aircraft.get('heading', 0)}deg | Squawk: {aircraft.get('squawk', 'none')}

Respond ONLY with this JSON:
{{
  "classification": "<FRIENDLY|CIVILIAN|HOSTILE|UNKNOWN>",
  "threat_probability": <0-100>,
  "reasoning": "<1-2 sentences>",
  "recommended_action": "<clear|monitor|intercept|emergency>"
}}"""
