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
        is_ship = info.get("platform_type") == "ship"
        if info["can_intercept"]:
            opts = []
            for atype, opt in info["options"].items():
                weapons_str = ", ".join(opt["weapons"]).replace("_", " ")
                if atype == "ship_sam":
                    opts.append(
                        f"ship_sam ({opt['response_min']}min, {opt['range_km']}km range, "
                        f"{opt['sam_count']} SAMs remaining, cost $180K/shot)"
                    )
                elif atype == "ground_defense":
                    opts.append(
                        f"ground_defense ({opt['response_min']}min, {opt['range_km']}km range, "
                        f"{opt['ammo']} rounds, cost $2K/shot)"
                    )
                else:
                    opts.append(
                        f"{atype} ({opt['response_min']}min, {opt['range_km']}km range, "
                        f"{opt.get('fuel_pct','?')}% fuel, weapons: {weapons_str})"
                    )
            opts_str = " | ".join(opts)
        else:
            opts_str = "OUT OF RANGE / NO ASSETS"

        if is_ship:
            base_lines.append(
                f"  {bid} [{info['name']}] NAVAL: {info['distance_km']} km away | "
                f"{info['available_count']} SAMs remaining\n"
                f"    Options: {opts_str}"
            )
        else:
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

{"⚠ CAPITAL UNDER THREAT — ARKTHOLM PRIORITY OVERRIDE: Arktholm is the capital (priority 10). Override ALL economy-of-force considerations. Deploy the FASTEST available intercept option regardless of cost. Set priority = immediate. Do not let fuel concerns, cost, or coverage gap calculations delay the intercept decision. Speed of response is paramount." if threat.get('target_name') == 'Arktholm' else f"Target priority: {next((t['priority'] for t in [dict(id='ARK',priority=10),dict(id='VLB',priority=6),dict(id='NDV',priority=6)] if t['id'] == threat.get('target_id')), 6)}/10 — apply economy-of-force normally"}

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

CONFIDENCE SCORING — be realistic, not optimistic. Start at 100 then subtract:
- Another base within 20% of the recommended base's response time: -15 (viable alternative exists)
- Any aircraft has fuel below 60%: -10 (degraded capability)
- Civilian traffic nearby: -15 (engagement constraints)
- Deployment creates a coverage gap: -20 (strategic risk)
- 2 or more active threats simultaneously: -10 (resource contention)
- Weapon is a suboptimal match for threat type: -10 (effectiveness uncertainty)
- No clear single best option (multiple bases tied): -10
Reserve 90–100% ONLY for a single unambiguous option with no alternatives and no risk factors. Typical decisions should score 60–80%. Only score below 40% when genuinely uncertain (multiple equally valid options, contradictory constraints, or missing critical data).

Select the optimal base, asset type, AND weapon. Apply economy-of-force principles:

ASSET MATCHING — choose the right platform AND weapon:
- Fighter: Strike aircraft, Fighter jet — air combat, long range (700km), carries LRM/SRM
- Interceptor: Ballistic missile, Cruise missile — fastest (1800km/h), carries SRM
- Drone: Armed drone threats, low-priority only — do NOT use against ballistic or aircraft
- ship_sam ($180K): Threat within 220km of a ship — NO sortie cost, 1200km/h SAM.
    Ideal for mid-passage intercept. Use if any ship has range.
- ship_ciws ($500/burst): Threat within 15km of a ship — automatic close-in gun system.
    Cheapest option for any threat that has penetrated to ship proximity.
- ground_defense ($2K/shot): Threat within 100km of a base — ground-based gun/SAM, no sortie cost.
    ALWAYS check this first. NVB can engage Nordvik threats, HRC can engage Arktholm threats.

WEAPON MATCHING (cost-effectiveness):
- long_range_missile ($1.5M): Ballistic missile, long-range cruise missile only
- short_range_missile ($300K): Aircraft and fast threats within 200km
- cannon/ground_cannon ($2K): Low-speed targets within 100km — always prefer over missiles
- armed_drone ($80K): Low-priority armed drone threats only
- ship_sam ($180K): Any threat within ship SAM range — preferred over aircraft for mid-passage

CROSS-BASE COVERAGE — critical rule:
If the natural base for a threat's target area shows "OUT OF RANGE / NO ASSETS" or available_count=0,
that base is DEPLETED and provides zero coverage. Do NOT recommend it. Immediately select the next
closest base or ship that has available assets. Examples:
- NVB depleted → Nordvik threats must be covered by HRC or BWP instead
- HRC depleted → Arktholm/Valbrek threats must be covered by NVB or BWP
- Never recommend a base with 0 available aircraft — it cannot intercept anything.

ECONOMY OF FORCE — layered defense priority:
1. Ground defense first: if threat within range of a base and ground ammo available → use it ($2K)
2. Ship SAM second: if any ship has range → intercept mid-passage ($180K, no sortie cost)
3. Drone third: low-priority/slow threats only ($3K sortie)
4. Interceptor: ballistic/cruise missiles requiring speed ($30K sortie)
5. Fighter: aircraft threats or when nothing else has range ($45K sortie)
6. Never fire a $1.5M LRM at an armed drone. Never scramble a fighter for a drone.
7. If base fuel < 30%, prefer ship SAM or ground defense to preserve manned sortie capability.

Respond ONLY with this JSON object:
{{
  "recommended_base": "<NVB|HRC|BWP|SNS-1|SNS-2|SNS-3>",
  "recommended_base_name": "<full name>",
  "recommended_asset_type": "<fighter|interceptor|drone|ship_sam|ship_ciws|ground_defense>",
  "recommended_weapon": "<long_range_missile|short_range_missile|cannon|armed_drone|ship_sam|ship_ciws|ground_cannon>",
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
    from collections import Counter

    wave_lines = []
    for i, w in enumerate(wave_log[-10:], 1):
        age_min = round((w.get("now", w["time"]) - w["time"]) / 60000, 1) if "time" in w else "?"
        targets_str = ", ".join(w.get("targets", []))
        types_str   = ", ".join(w.get("types", [])) if w.get("types") else "unknown"
        outcomes_str = ", ".join(w.get("outcomes", [])) if w.get("outcomes") else "unknown"
        wave_lines.append(
            f"  Wave {i}: {w.get('count', '?')} threats | targets: {targets_str} | "
            f"types: {types_str} | outcomes: {outcomes_str} | {age_min} min ago"
        )

    intervals = []
    times = [w["time"] for w in wave_log if "time" in w]
    for i in range(1, len(times)):
        intervals.append((times[i] - times[i - 1]) / 60000)
    avg_interval = round(sum(intervals) / len(intervals), 1) if intervals else None

    # Derive attack pattern stats
    all_targets = [t for w in wave_log for t in w.get("targets", [])]
    all_types   = [t for w in wave_log for t in w.get("types", [])]
    target_freq = Counter(all_targets).most_common()
    type_freq   = Counter(all_types).most_common()
    target_freq_str = ", ".join(f"{t}×{c}" for t, c in target_freq) if target_freq else "none"
    type_freq_str   = ", ".join(f"{t}×{c}" for t, c in type_freq)   if type_freq   else "none"

    escalation = "escalating" if len(wave_log) >= 2 and wave_log[-1].get("count", 0) > wave_log[0].get("count", 0) else \
                 "de-escalating" if len(wave_log) >= 2 and wave_log[-1].get("count", 0) < wave_log[0].get("count", 0) else "stable"

    base_lines = "\n".join(
        f"  {b['id']} ({b['name']}): {b['available_count']} aircraft ready, "
        f"avg fuel ~{round(sum(a['fuel_pct'] for a in b['assets']) / max(len(b['assets']), 1))}%"
        for b in state_summary.get("bases", [])
    )

    return f"""You are a tactical air defense intelligence analyst for Northern Command.

WAVE HISTORY (most recent last, newest = wave {len(wave_log)}):
{chr(10).join(wave_lines) if wave_lines else "  No waves recorded yet."}

ATTACK PATTERN ANALYSIS:
  Average interval between waves: {f"{avg_interval} min" if avg_interval else "insufficient data"}
  Wave size trend: {escalation}
  Target hit frequency: {target_freq_str}
  Threat types used so far: {type_freq_str}

CURRENT BASE READINESS:
{base_lines}

NORTH PROTECTION TARGETS:
  Arktholm (capital, priority 10) — most valuable, likely primary target
  Valbrek (major city, priority 6) — eastern flank
  Nordvik (major city, priority 6) — western flank, near NVB

INTELLIGENCE ASSESSMENT TASK:
Based on the attack history above, reason as a tactical attacker:
1. Which city has been hit least (lowest coverage, most vulnerable)?
2. Which threat types caused the most damage or evaded intercept?
3. Is this attacker escalating wave size, probing defenses, or focusing on one target?
4. What would a rational attacker do next — repeat success or probe a new gap?

Predict the SPECIFIC next attack: which city will be targeted, which threat type will be used,
and how many threats in the wave. Be specific — do not say "any of the three cities".

Respond ONLY with this JSON:
{{
  "next_wave_estimate_min": <integer minutes from now, or null if unknown>,
  "predicted_targets": ["<specific city name — Arktholm or Valbrek or Nordvik>"],
  "threat_types_expected": ["<specific type from: Ballistic missile|Cruise missile|Strike aircraft|Fighter jet|Armed drone>"],
  "predicted_wave_size": <integer 2-7>,
  "recommended_readiness": "<2 sentences: which specific base(s) to alert and why, and what weapon types to prioritize>",
  "risk_level": "<low|medium|high|critical>",
  "reasoning": "<2-3 sentences: explain attacker logic, pattern observed, and why you predict this specific target/type>"
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
