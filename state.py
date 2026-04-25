import math
import time

# ── Human-in-the-loop approval rules ─────────────────────────────────────────
APPROVAL_RULES = {
    "confidence_threshold": 60,          # below 60% confidence → human reviews
    "civilian_risk_levels": ["high"],    # high civilian risk → human reviews
    "coverage_gap": True,                # deployment creating a coverage gap → human reviews
}

pending_approvals = {}   # decision_id -> {decision, threat, reasons, created_at, ...}


def check_approval_required(decision: dict, coverage: dict) -> tuple:
    """Return (requires_approval: bool, reasons: list[str])."""
    reasons = []
    conf = decision.get("confidence", 100)
    if conf < APPROVAL_RULES["confidence_threshold"]:
        reasons.append(f"AI confidence {conf}% below threshold ({APPROVAL_RULES['confidence_threshold']}%)")
    if decision.get("civilian_risk") in APPROVAL_RULES["civilian_risk_levels"]:
        reasons.append(f"Civilian risk level: {decision['civilian_risk']}")
    if APPROVAL_RULES["coverage_gap"] and coverage.get("gaps"):
        reasons.append(f"Deployment creates coverage gap: {', '.join(coverage['gaps'])}")
    return len(reasons) > 0, reasons


PROTECTION_TARGETS = [
    {"id": "ARK", "name": "Arktholm",  "side": "north", "subtype": "capital",    "x_km": 418.3,  "y_km": 95.0,   "priority": 10},
    {"id": "VLB", "name": "Valbrek",   "side": "north", "subtype": "major_city", "x_km": 1423.3, "y_km": 213.3,  "priority": 6},
    {"id": "NDV", "name": "Nordvik",   "side": "north", "subtype": "major_city", "x_km": 140.0,  "y_km": 323.3,  "priority": 6},
    {"id": "MER", "name": "Meridia",   "side": "south", "subtype": "capital",    "x_km": 1225.0, "y_km": 1208.3, "priority": 10},
    {"id": "CLH", "name": "Callhaven", "side": "south", "subtype": "major_city", "x_km": 96.7,   "y_km": 1150.0, "priority": 6},
    {"id": "SOL", "name": "Solano",    "side": "south", "subtype": "major_city", "x_km": 576.7,  "y_km": 1236.7, "priority": 6},
]

AIRCRAFT_PROFILES = {
    "fighter":     {"range_km": 700,  "speed_km_h": 1400, "fuel_burn_pct_per_km": 0.10},
    "interceptor": {"range_km": 500,  "speed_km_h": 1800, "fuel_burn_pct_per_km": 0.14},
    "drone":       {"range_km": 300,  "speed_km_h": 300,  "fuel_burn_pct_per_km": 0.04},
}

# ── Resource economics ────────────────────────────────────────────────────────
WEAPON_COSTS_USD = {
    "long_range_missile":  1_500_000,
    "short_range_missile":   300_000,
    "cannon":                  2_000,
    "armed_drone":            80_000,
    "air_defense":           500_000,
}

SORTIE_COSTS_USD = {
    "fighter":     45_000,
    "interceptor": 30_000,
    "drone":        3_000,
}

FUEL_CONSUMPTION_LITERS = {
    "fighter":     4_000,
    "interceptor": 3_000,
    "drone":         300,
}

# Session-level cost tracker (resets on server restart)
session_costs = {
    "total_usd": 0,
    "sorties": 0,
    "weapons_fired": {},
    "fuel_consumed_liters": 0,
}

def dist_km(x1, y1, x2, y2):
    return math.sqrt((x2 - x1) ** 2 + (y2 - y1) ** 2)

def response_time_min(bx, by, tx, ty, atype):
    d = dist_km(bx, by, tx, ty)
    return round(d / AIRCRAFT_PROFILES[atype]["speed_km_h"] * 60, 1)

def can_intercept(bx, by, tx, ty, atype, fuel_pct):
    d = dist_km(bx, by, tx, ty)
    effective_range = AIRCRAFT_PROFILES[atype]["range_km"] * (fuel_pct / 100)
    return d <= effective_range

world_state = {
    "bases": [
        {
            "id": "NVB", "name": "Northern Vanguard Base", "side": "north",
            "x_km": 198.3, "y_km": 335.0,
            "assets": [
                {"id": "NVB-F1", "type": "fighter",     "status": "available", "fuel_pct": 100, "weapons": ["long_range_missile", "short_range_missile"]},
                {"id": "NVB-F2", "type": "fighter",     "status": "available", "fuel_pct": 88,  "weapons": ["long_range_missile", "cannon"]},
                {"id": "NVB-I1", "type": "interceptor", "status": "available", "fuel_pct": 92,  "weapons": ["short_range_missile", "short_range_missile"]},
                {"id": "NVB-D1", "type": "drone",       "status": "available", "fuel_pct": 95,  "weapons": ["armed_drone"]},
            ],
            "weapons_inventory": {"long_range_missile": 4, "short_range_missile": 10, "cannon": 1, "armed_drone": 3},
            "fuel_stock_liters": 40_000,
            "ground_defense": {"type": "air_defense", "ammo": 8},
        },
        {
            "id": "HRC", "name": "Highridge Command", "side": "north",
            "x_km": 838.3, "y_km": 75.0,
            "assets": [
                {"id": "HRC-F1", "type": "fighter",     "status": "available", "fuel_pct": 100, "weapons": ["long_range_missile", "long_range_missile"]},
                {"id": "HRC-F2", "type": "fighter",     "status": "available", "fuel_pct": 95,  "weapons": ["long_range_missile", "short_range_missile"]},
                {"id": "HRC-F3", "type": "fighter",     "status": "available", "fuel_pct": 82,  "weapons": ["short_range_missile", "cannon"]},
                {"id": "HRC-I1", "type": "interceptor", "status": "available", "fuel_pct": 90,  "weapons": ["short_range_missile"]},
                {"id": "HRC-D1", "type": "drone",       "status": "available", "fuel_pct": 100, "weapons": ["armed_drone"]},
                {"id": "HRC-D2", "type": "drone",       "status": "available", "fuel_pct": 76,  "weapons": ["armed_drone"]},
            ],
            "weapons_inventory": {"long_range_missile": 8, "short_range_missile": 16, "cannon": 2, "armed_drone": 6},
            "fuel_stock_liters": 90_000,
            "ground_defense": {"type": "air_defense", "ammo": 12},
        },
        {
            "id": "BWP", "name": "Boreal Watch Post", "side": "north",
            "x_km": 1158.3, "y_km": 385.0,
            "assets": [
                {"id": "BWP-I1", "type": "interceptor", "status": "available", "fuel_pct": 94,  "weapons": ["short_range_missile", "short_range_missile"]},
                {"id": "BWP-D1", "type": "drone",       "status": "available", "fuel_pct": 100, "weapons": ["armed_drone"]},
                {"id": "BWP-D2", "type": "drone",       "status": "available", "fuel_pct": 85,  "weapons": ["armed_drone"]},
            ],
            "weapons_inventory": {"long_range_missile": 2, "short_range_missile": 6, "cannon": 1, "armed_drone": 4},
            "fuel_stock_liters": 18_000,
            "ground_defense": {"type": "air_defense", "ammo": 6},
        },
    ],
    "active_threats": [],
    "active_deployments": [],
    "resolved_threats": [],
}


def get_available_assets(base_id=None):
    result = []
    for base in world_state["bases"]:
        if base_id and base["id"] != base_id:
            continue
        for asset in base["assets"]:
            if asset["status"] == "available":
                result.append({**asset, "base_id": base["id"], "base_name": base["name"],
                                "base_x_km": base["x_km"], "base_y_km": base["y_km"]})
    return result


def deploy_asset(asset_id, threat_id, weapon):
    for base in world_state["bases"]:
        for asset in base["assets"]:
            if asset["id"] == asset_id:
                atype = asset["type"]
                asset["status"] = "deployed"
                asset["deployed_at"] = time.time()
                asset["mission_threat"] = threat_id

                # Deduct weapon from per-aircraft loadout
                if weapon in asset["weapons"]:
                    asset["weapons"].remove(weapon)

                # Deduct from base shared inventory
                inv = base.get("weapons_inventory", {})
                if weapon in inv and inv[weapon] > 0:
                    inv[weapon] -= 1

                # Deduct fuel
                fuel_used = FUEL_CONSUMPTION_LITERS.get(atype, 0)
                base["fuel_stock_liters"] = max(0, base.get("fuel_stock_liters", 0) - fuel_used)
                asset["fuel_pct"] = max(0, asset["fuel_pct"] - AIRCRAFT_PROFILES[atype]["fuel_burn_pct_per_km"] * 300)

                # Track session costs
                weapon_cost = WEAPON_COSTS_USD.get(weapon, 0)
                sortie_cost = SORTIE_COSTS_USD.get(atype, 0)
                total = weapon_cost + sortie_cost
                session_costs["total_usd"] += total
                session_costs["sorties"] += 1
                session_costs["fuel_consumed_liters"] += fuel_used
                session_costs["weapons_fired"][weapon] = session_costs["weapons_fired"].get(weapon, 0) + 1
                asset["last_mission_cost_usd"] = total

                world_state["active_deployments"].append({
                    "asset_id": asset_id, "base_id": base["id"],
                    "threat_id": threat_id, "weapon": weapon,
                    "deployed_at": time.time(),
                    "cost_usd": total,
                })
                return True
    return False


def return_asset(asset_id):
    for base in world_state["bases"]:
        for asset in base["assets"]:
            if asset["id"] == asset_id:
                asset["status"] = "available"
                asset.pop("deployed_at", None)
                asset.pop("mission_threat", None)
                asset["fuel_pct"] = max(20, asset["fuel_pct"] - 15)
                restore = {"fighter": "short_range_missile", "interceptor": "short_range_missile", "drone": "armed_drone"}
                asset["weapons"].append(restore.get(asset["type"], "short_range_missile"))
                world_state["active_deployments"] = [
                    d for d in world_state["active_deployments"] if d["asset_id"] != asset_id
                ]
                return True
    return False


def auto_return_assets(mission_duration_s=180):
    now = time.time()
    to_return = []
    for base in world_state["bases"]:
        for asset in base["assets"]:
            if asset["status"] == "deployed" and "deployed_at" in asset:
                if now - asset["deployed_at"] > mission_duration_s:
                    to_return.append(asset["id"])
    for aid in to_return:
        return_asset(aid)


def build_distance_matrix(tx, ty):
    matrix = {}
    for base in world_state["bases"]:
        d = dist_km(base["x_km"], base["y_km"], tx, ty)
        available = [a for a in base["assets"] if a["status"] == "available"]

        # Build per-type option — best (highest-fuel) asset of each type that can intercept
        by_type = {}
        for a in available:
            atype = a["type"]
            if can_intercept(base["x_km"], base["y_km"], tx, ty, atype, a["fuel_pct"]):
                rt = response_time_min(base["x_km"], base["y_km"], tx, ty, atype)
                if atype not in by_type or a["fuel_pct"] > by_type[atype]["fuel_pct"]:
                    by_type[atype] = {
                        "type": atype,
                        "response_min": rt,
                        "fuel_pct": a["fuel_pct"],
                        "range_km": AIRCRAFT_PROFILES[atype]["range_km"],
                        "speed_km_h": AIRCRAFT_PROFILES[atype]["speed_km_h"],
                        "weapons": a["weapons"],
                        "asset_id": a["id"],
                    }

        matrix[base["id"]] = {
            "name": base["name"],
            "distance_km": round(d, 1),
            "available_count": len(available),
            "can_intercept": len(by_type) > 0,
            "options": by_type,   # keyed by type: fighter / interceptor / drone
        }
    return matrix


def coverage_assessment(deploying_base_id=None):
    gaps, warnings = [], []
    for target in PROTECTION_TARGETS:
        if target["side"] != "north":
            continue
        covered_by = []
        for base in world_state["bases"]:
            avail = sum(1 for a in base["assets"]
                        if a["status"] == "available" and a["type"] in ("fighter", "interceptor"))
            if base["id"] == deploying_base_id:
                avail = max(0, avail - 1)
            if avail > 0 and dist_km(base["x_km"], base["y_km"], target["x_km"], target["y_km"]) <= 700:
                covered_by.append(base["id"])
        if not covered_by:
            gaps.append(target["name"])
        elif len(covered_by) == 1:
            warnings.append(f"{target['name']} has single-base coverage ({covered_by[0]})")
    return {"gaps": gaps, "warnings": warnings}


def get_resource_warnings(base: dict) -> list:
    """Flag low-stock resources at a base."""
    warnings = []
    inv = base.get("weapons_inventory", {})
    fuel = base.get("fuel_stock_liters", 0)
    max_fuel = {"NVB": 40_000, "HRC": 90_000, "BWP": 18_000}.get(base["id"], 40_000)
    if inv.get("long_range_missile", 0) <= 1:
        warnings.append("CRITICAL: long range missiles ≤ 1")
    elif inv.get("long_range_missile", 0) <= 2:
        warnings.append("LOW: long range missiles ≤ 2")
    if inv.get("short_range_missile", 0) <= 2:
        warnings.append("LOW: short range missiles ≤ 2")
    if fuel / max_fuel < 0.2:
        warnings.append(f"CRITICAL: fuel at {round(fuel/max_fuel*100)}%")
    elif fuel / max_fuel < 0.4:
        warnings.append(f"LOW: fuel at {round(fuel/max_fuel*100)}%")
    return warnings


def get_state_summary():
    auto_return_assets()
    bases = []
    for base in world_state["bases"]:
        available = [a for a in base["assets"] if a["status"] == "available"]
        deployed  = [a for a in base["assets"] if a["status"] == "deployed"]
        max_fuel  = {"NVB": 40_000, "HRC": 90_000, "BWP": 18_000}.get(base["id"], 40_000)
        fuel_pct  = round(base.get("fuel_stock_liters", 0) / max_fuel * 100)
        bases.append({
            "id": base["id"], "name": base["name"],
            "x_km": base["x_km"], "y_km": base["y_km"],
            "available_count": len(available),
            "deployed_count": len(deployed),
            "assets": [{"id": a["id"], "type": a["type"], "fuel_pct": a["fuel_pct"],
                        "weapons": a["weapons"],
                        "range_km": AIRCRAFT_PROFILES[a["type"]]["range_km"],
                        "speed_km_h": AIRCRAFT_PROFILES[a["type"]]["speed_km_h"],
                        "sortie_cost_usd": SORTIE_COSTS_USD[a["type"]]}
                       for a in available],
            "weapons_inventory": base.get("weapons_inventory", {}),
            "fuel_stock_liters": base.get("fuel_stock_liters", 0),
            "fuel_pct": fuel_pct,
            "ground_ammo": base["ground_defense"]["ammo"],
            "resource_warnings": get_resource_warnings(base),
        })
    return {
        "bases": bases,
        "active_deployments": len(world_state["active_deployments"]),
        "active_threats": len(world_state["active_threats"]),
        "protection_targets": PROTECTION_TARGETS,
        "session_costs": session_costs,
        "weapon_costs_usd": WEAPON_COSTS_USD,
        "sortie_costs_usd": SORTIE_COSTS_USD,
    }
