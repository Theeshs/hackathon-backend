from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
import requests
import uuid
import time

from state import (
    world_state, PROTECTION_TARGETS, AIRCRAFT_PROFILES,
    WEAPON_COSTS_USD, SORTIE_COSTS_USD, session_costs,
    get_available_assets, deploy_asset, return_asset, auto_return_assets,
    get_state_summary, build_distance_matrix, coverage_assessment, dist_km,
    get_resource_warnings,
    pending_approvals, check_approval_required, APPROVAL_RULES,
)
from gemini import call_gemini, build_decision_prompt, build_iff_prompt, build_forecast_prompt

app = FastAPI(title="Air Defense Decision Support API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Models ────────────────────────────────────────────────────────────────────

class Threat(BaseModel):
    id: str
    type: str
    x: float
    y: float
    x_km: Optional[float] = None
    y_km: Optional[float] = None
    speed: Optional[float] = 800
    heading: Optional[float] = 180
    eta: Optional[float] = 60
    classification: Optional[str] = "HOSTILE"
    civilian_nearby: Optional[bool] = False
    target_id: Optional[str] = None
    target_name: Optional[str] = None

class Aircraft(BaseModel):
    callsign: str
    x: float
    y: float
    speed: float
    altitude: float
    heading: float
    squawk: Optional[str] = "none"

class AssetReturn(BaseModel):
    asset_id: str


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/")
def root():
    return {"status": "Air Defense API running"}


@app.get("/state")
def get_state():
    auto_return_assets()
    return world_state


@app.get("/state/summary")
def state_summary_route():
    return get_state_summary()


@app.get("/state/aircraft")
def aircraft_status():
    """Per-base aircraft detail including fuel levels."""
    auto_return_assets()
    result = []
    for base in world_state["bases"]:
        available = [a for a in base["assets"] if a["status"] == "available"]
        deployed  = [a for a in base["assets"] if a["status"] == "deployed"]
        result.append({
            "id": base["id"],
            "name": base["name"],
            "x_km": base["x_km"],
            "y_km": base["y_km"],
            "available": [
                {"id": a["id"], "type": a["type"], "fuel_pct": a["fuel_pct"],
                 "weapons": a["weapons"], "range_km": AIRCRAFT_PROFILES[a["type"]]["range_km"]}
                for a in available
            ],
            "deployed": [{"id": a["id"], "type": a["type"]} for a in deployed],
            "weapons_inventory": base.get("weapons_inventory", {}),
            "fuel_stock_liters": base.get("fuel_stock_liters", 0),
            "fuel_pct": round(base.get("fuel_stock_liters", 0) /
                              {"NVB": 40000, "HRC": 90000, "BWP": 18000}.get(base["id"], 40000) * 100),
            "resource_warnings": get_resource_warnings(base),
            "ground_ammo": base["ground_defense"]["ammo"],
        })
    return {"bases": result}


@app.get("/distances")
def get_distances():
    """Pairwise distance matrix for all bases and protection targets."""
    entities = [
        {"id": b["id"], "name": b["name"], "type": "base", "x_km": b["x_km"], "y_km": b["y_km"]}
        for b in world_state["bases"]
    ] + [
        {"id": t["id"], "name": t["name"], "type": t["subtype"], "x_km": t["x_km"], "y_km": t["y_km"]}
        for t in PROTECTION_TARGETS
    ]
    matrix = {
        e1["id"]: {
            e2["id"]: round(dist_km(e1["x_km"], e1["y_km"], e2["x_km"], e2["y_km"]), 1)
            for e2 in entities if e1["id"] != e2["id"]
        }
        for e1 in entities
    }
    return {"entities": entities, "matrix": matrix}


def _execute_deployment(decision: dict, threat_dict: dict) -> Optional[str]:
    """Deploy the asset specified in decision. Returns asset_id or None."""
    base_id = decision.get("recommended_base")
    if not base_id:
        return None
    candidates = get_available_assets(base_id)
    preferred = [a for a in candidates if a["type"] == decision.get("recommended_asset_type")]
    chosen = (preferred or candidates or [None])[0]
    if chosen:
        deploy_asset(chosen["id"], threat_dict["id"], decision.get("recommended_weapon", ""))
        return chosen["id"]
    return None


@app.post("/decide")
def decide(threat: Threat):
    auto_return_assets()

    threat_dict = threat.dict()
    threat_dict["timestamp"] = time.time()

    SCALE_X = 1000 / 1666.7
    SCALE_Y = 780 / 1300
    tx = threat.x_km if threat.x_km is not None else threat.x / SCALE_X
    ty = threat.y_km if threat.y_km is not None else threat.y / SCALE_Y
    threat_dict["x_km"] = tx
    threat_dict["y_km"] = ty

    world_state["active_threats"].append(threat_dict)

    distance_matrix = build_distance_matrix(tx, ty)

    candidate_base_id = None
    best_t = 999
    for bid, info in distance_matrix.items():
        if info["can_intercept"]:
            fastest = min((o["response_min"] for o in info["options"].values()), default=999)
            if fastest < best_t:
                best_t = fastest
                candidate_base_id = bid

    coverage = coverage_assessment(candidate_base_id)
    state_sum = get_state_summary()
    prompt = build_decision_prompt(threat_dict, state_sum, distance_matrix, coverage)
    decision = call_gemini(prompt)

    if decision.get("fallback"):
        available = get_available_assets()
        # Prefer asset type matched to threat class
        aircraft_threats = {"Strike aircraft", "Fighter jet"}
        missile_threats  = {"Ballistic missile", "Cruise missile"}
        drone_threats    = {"Armed drone"}
        preferred_type = (
            "fighter"     if threat.type in aircraft_threats else
            "interceptor" if threat.type in missile_threats  else
            "drone"       if threat.type in drone_threats    else
            None
        )
        best_a, best_time = None, 999
        for a in available:
            d = dist_km(a["base_x_km"], a["base_y_km"], tx, ty)
            p = AIRCRAFT_PROFILES[a["type"]]
            if d <= p["range_km"] * (a["fuel_pct"] / 100):
                t = d / p["speed_km_h"] * 60
                # Bias toward preferred type — give it a 20% time bonus
                effective_t = t * (0.8 if a["type"] == preferred_type else 1.0)
                if effective_t < best_time:
                    best_time, best_a = effective_t, a
        if not best_a and available:
            best_a = available[0]
        if best_a:
            decision = {
                "recommended_base": best_a["base_id"],
                "recommended_base_name": best_a["base_name"],
                "recommended_asset_type": best_a["type"],
                "recommended_weapon": best_a["weapons"][0] if best_a["weapons"] else "cannon",
                "confidence": 55,
                "reasoning": f"Spatial fallback: {best_a['base_name']} — nearest in-range base. AI offline.",
                "alternatives_rejected": [],
                "trade_offs": "No coverage lookahead — fallback only.",
                "civilian_risk": "medium" if threat.civilian_nearby else "none",
                "civilian_note": "",
                "future_risk": "Reconnect AI engine for full support.",
                "alternative_base": "N/A",
                "priority": "urgent",
            }

    decision_id = f"D{str(uuid.uuid4())[:6].upper()}"

    # ── Human-in-the-loop gate ────────────────────────────────────────────────
    needs_approval, approval_reasons = check_approval_required(decision, coverage)

    if needs_approval:
        pending_approvals[decision_id] = {
            "decision_id": decision_id,
            "threat": threat_dict,
            "decision": decision,
            "approval_reasons": approval_reasons,
            "coverage": coverage,
            "distance_matrix": distance_matrix,
            "created_at": time.time(),
        }
        return {
            "decision_id": decision_id,
            "threat_id": threat.id,
            "threat_type": threat.type,
            "timestamp": time.time(),
            "status": "pending_approval",
            "approval_reasons": approval_reasons,
            "asset_deployed": None,
            "distance_matrix": distance_matrix,
            **decision,
        }

    # Auto-execute when all clear
    asset_id = _execute_deployment(decision, threat_dict)
    return {
        "decision_id": decision_id,
        "threat_id": threat.id,
        "threat_type": threat.type,
        "timestamp": time.time(),
        "status": "auto_executed",
        "asset_deployed": asset_id,
        "distance_matrix": distance_matrix,
        **decision,
    }


@app.get("/pending")
def get_pending():
    """Return all decisions waiting for human approval."""
    now = time.time()
    # Auto-expire after 90 seconds (missed intercept window)
    expired = [did for did, p in pending_approvals.items() if now - p["created_at"] > 90]
    for did in expired:
        pending_approvals.pop(did, None)
    return {"pending": list(pending_approvals.values()), "count": len(pending_approvals)}


@app.post("/approve/{decision_id}")
def approve_decision(decision_id: str, override_base: Optional[str] = None):
    """Approve a pending decision and execute deployment. Optionally override the base."""
    if decision_id not in pending_approvals:
        raise HTTPException(status_code=404, detail="Decision not found or expired")
    pending = pending_approvals.pop(decision_id)
    decision = pending["decision"]
    threat_dict = pending["threat"]

    if override_base:
        for base in world_state["bases"]:
            if base["id"] == override_base:
                decision["recommended_base"] = override_base
                decision["recommended_base_name"] = base["name"]
                break

    asset_id = _execute_deployment(decision, threat_dict)
    return {
        "status": "approved",
        "decision_id": decision_id,
        "asset_deployed": asset_id,
        "base": decision.get("recommended_base_name"),
        "override_applied": override_base is not None,
    }


@app.post("/reject/{decision_id}")
def reject_decision(decision_id: str):
    """Reject a pending decision — no asset deployed."""
    if decision_id not in pending_approvals:
        raise HTTPException(status_code=404, detail="Decision not found or expired")
    pending = pending_approvals.pop(decision_id)
    world_state["active_threats"] = [
        t for t in world_state["active_threats"] if t["id"] != pending["threat"]["id"]
    ]
    return {"status": "rejected", "decision_id": decision_id}


@app.get("/approval-rules")
def get_approval_rules():
    return APPROVAL_RULES


@app.put("/approval-rules")
def update_approval_rules(confidence_threshold: Optional[int] = None,
                          coverage_gap: Optional[bool] = None):
    """Adjust approval thresholds at runtime."""
    if confidence_threshold is not None:
        APPROVAL_RULES["confidence_threshold"] = max(0, min(100, confidence_threshold))
    if coverage_gap is not None:
        APPROVAL_RULES["coverage_gap"] = coverage_gap
    return APPROVAL_RULES


@app.post("/iff")
def identify_aircraft(aircraft: Aircraft):
    prompt = build_iff_prompt(aircraft.dict())
    result = call_gemini(prompt)
    if result.get("fallback"):
        return {"classification": "UNKNOWN", "threat_probability": 50,
                "reasoning": "IFF offline.", "recommended_action": "monitor"}
    return result


@app.post("/asset/return")
def asset_returned(data: AssetReturn):
    if not return_asset(data.asset_id):
        raise HTTPException(status_code=404, detail="Asset not found")
    return {"status": "returned", "asset_id": data.asset_id}


@app.delete("/threats/{threat_id}")
def resolve_threat(threat_id: str):
    before = len(world_state["active_threats"])
    world_state["active_threats"] = [t for t in world_state["active_threats"] if t["id"] != threat_id]
    return {"resolved": len(world_state["active_threats"]) < before, "threat_id": threat_id}


class WaveLog(BaseModel):
    wave_log: list
    base_count: Optional[int] = 0


def _heuristic_forecast(wave_log: list) -> dict:
    from collections import Counter
    intervals, targets = [], []
    times = [w.get("time", 0) for w in wave_log]
    for i in range(1, len(times)):
        intervals.append((times[i] - times[i - 1]) / 60000)
    for w in wave_log:
        targets.extend(w.get("targets", []))
    avg = round(sum(intervals) / len(intervals), 1) if intervals else None
    top = Counter(targets).most_common(1)[0][0] if targets else "Arktholm"
    return {
        "next_wave_estimate_min": round(avg) if avg else None,
        "predicted_targets": [top],
        "threat_types_expected": ["Ballistic missile", "Strike aircraft"],
        "recommended_readiness": "Maintain readiness at all bases. Refuel any aircraft below 50%.",
        "risk_level": "medium",
        "reasoning": "Heuristic estimate based on wave interval history. AI forecast unavailable.",
    }


@app.post("/forecast")
def forecast(data: WaveLog):
    state_sum = get_state_summary()
    prompt = build_forecast_prompt(data.wave_log, state_sum)
    result = call_gemini(prompt)
    if result.get("fallback"):
        return _heuristic_forecast(data.wave_log)
    return result


@app.get("/civilian")
def get_civilian_flights():
    try:
        r = requests.get(
            "https://opensky-network.org/api/states/all?lamin=55.0&lomin=10.0&lamax=69.0&lomax=24.0",
            timeout=8,
        )
        data = r.json()
        flights = []
        for s in (data.get("states") or [])[:20]:
            if s[5] and s[6]:
                flights.append({
                    "callsign": (s[1] or "").strip() or f"N{s[0][:4]}",
                    "lat": s[6], "lng": s[5], "altitude": s[7] or 0,
                    "speed": s[9] or 0, "heading": s[10] or 0,
                    "on_ground": s[8], "squawk": s[14] or "none",
                })
        return {"flights": flights, "count": len(flights), "source": "opensky"}
    except Exception as e:
        return {"flights": [], "count": 0, "error": str(e), "source": "opensky"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000, reload=True)
