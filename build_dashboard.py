#!/usr/bin/env python3
"""
Builds a self-contained local dashboard.html from a Garmin export produced
by fetch_activities.py. No network, no external assets -- everything is
inlined so the file works fully offline, double-click to open.

Three views are produced from one payload:
  * Half marathon  -- race readiness, projected finish, the build plan
  * Fitness lab    -- every metric Garmin records, its trend, and where
                      six months of training would put it
  * Training log   -- volume, effort mix, the raw runs table

Usage:
    python3 build_dashboard.py <export.json> <output.html>
"""
import json
import math
import sys
from datetime import datetime, timedelta
from collections import defaultdict
from pathlib import Path

HALF_MARATHON_M = 21097.5
HALF_MARATHON_KM = 21.0975
MIN_KM_FOR_EFFICIENCY = 3.0   # short efforts distort meters-per-heartbeat

# A half marathon run comfortably wants roughly this much weekly volume
# behind it. Used only to scale the readiness meter, not as a hard rule.
HM_VOLUME_TARGET_KM = 45.0
HM_LONG_RUN_TARGET_KM = 18.0  # you don't need the full 21.1 in training
EASY_TARGET_PCT = 80


# ---------------------------------------------------------------- formatting

def pace_str(min_per_km):
    if min_per_km is None:
        return None
    m = int(min_per_km)
    s = int(round((min_per_km - m) * 60))
    if s == 60:
        m += 1
        s = 0
    return f"{m}:{s:02d}"


def secs_str(secs):
    if not secs:
        return None
    secs = int(round(secs))
    m, s = divmod(secs, 60)
    return f"{m}:{s:02d}"


def clock_str(secs):
    """h:mm:ss for race-length durations."""
    if not secs:
        return None
    secs = int(round(secs))
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


# ------------------------------------------------- physiology / race models

def _pct_vo2max(t_min):
    """Fraction of VO2max sustainable for t minutes (Daniels & Gilbert)."""
    return (0.8
            + 0.1894393 * math.exp(-0.012778 * t_min)
            + 0.2989558 * math.exp(-0.1932605 * t_min))


def _vo2_cost(v_m_per_min):
    """Oxygen cost of running at velocity v (Daniels & Gilbert)."""
    return -4.60 + 0.182258 * v_m_per_min + 0.000104 * v_m_per_min ** 2


def race_seconds_from_vdot(vdot, dist_m):
    """Solve for the time at which oxygen demand meets sustainable supply."""
    if not vdot or vdot <= 0:
        return None
    lo, hi = 1.0, 600.0
    for _ in range(120):
        mid = (lo + hi) / 2
        demand = _vo2_cost(dist_m / mid)
        supply = vdot * _pct_vo2max(mid)
        if demand > supply:
            lo = mid       # too ambitious, needs more time
        else:
            hi = mid
    return (lo + hi) / 2 * 60


def riegel_seconds(known_secs, known_m, target_m, exponent=1.06):
    """Extrapolate a race time to another distance from a demonstrated effort."""
    if not known_secs or not known_m:
        return None
    return known_secs * (target_m / known_m) ** exponent


def linear_trend(points):
    """Least-squares slope/intercept over (x, y). x in days."""
    pts = [(x, y) for x, y in points if y is not None]
    n = len(pts)
    if n < 3:
        return None
    mx = sum(p[0] for p in pts) / n
    my = sum(p[1] for p in pts) / n
    denom = sum((p[0] - mx) ** 2 for p in pts)
    if not denom:
        return None
    slope = sum((p[0] - mx) * (p[1] - my) for p in pts) / denom
    intercept = my - slope * mx
    # r^2 so we can say how much to trust it
    ss_tot = sum((p[1] - my) ** 2 for p in pts)
    ss_res = sum((p[1] - (slope * p[0] + intercept)) ** 2 for p in pts)
    r2 = 1 - ss_res / ss_tot if ss_tot else 0
    return {"slope_per_day": slope, "intercept": intercept, "r2": round(r2, 3), "n": n}


def project_vo2max(v0, monthly_rate, decay, months=6, ceiling=58.0):
    """
    Month-by-month projection with geometrically decaying gains.

    Aerobic gains do not continue in a straight line -- the closer you get to
    your genetic ceiling the smaller each month's improvement. `decay` is the
    share of this month's gain that carries into next month.
    """
    out = []
    v = v0
    rate = monthly_rate
    for m in range(1, months + 1):
        v = min(ceiling, v + rate)
        rate *= decay
        out.append({"month": m, "value": round(v, 2)})
    return out


# ------------------------------------------------------------------- build

def build(export_path, out_path):
    with open(export_path) as f:
        data = json.load(f)

    acts = data.get("activities", [])
    wellness_raw = data.get("wellness", [])
    training_status = data.get("training_status") or {}

    runs = []
    for a in acts:
        s = a["summary"]
        dist_km = (s.get("distance") or 0) / 1000
        dur_min = (s.get("duration") or 0) / 60
        pace = dur_min / dist_km if dist_km else None
        try:
            dt = datetime.strptime(s["startTimeLocal"], "%Y-%m-%d %H:%M:%S")
        except Exception:
            continue

        laps = ((a.get("splits") or {}).get("lapDTOs")) or []
        lap_paces = []
        lap_profile = []
        for index, lap in enumerate(laps, start=1):
            ld = (lap.get("distance") or 0) / 1000
            ldur = (lap.get("duration") or 0) / 60
            if ld > 0.2:
                lap_paces.append(ldur / ld)
                lap_profile.append({
                    "lap": index,
                    "distance_km": round(ld, 2),
                    "pace": round(ldur / ld, 3),
                    "cadence": lap.get("averageRunCadence"),
                    "avg_hr": lap.get("averageHR"),
                    "gct": lap.get("groundContactTime"),
                    "vert_ratio": lap.get("verticalRatio"),
                    "vert_osc": lap.get("verticalOscillation"),
                    "stride_len": lap.get("strideLength"),
                    "power": lap.get("averagePower"),
                })

        split_trend = None
        decoupling = None
        if len(lap_paces) >= 4:
            half = len(lap_paces) // 2
            first = sum(lap_paces[:half]) / half
            second = sum(lap_paces[half:]) / (len(lap_paces) - half)
            split_trend = "negative" if second < first else "positive"
            decoupling = round(100 * (second - first) / first, 1)

        zones = [s.get(f"hrTimeInZone_{i}") or 0 for i in range(1, 6)]
        pzones = [s.get(f"powerTimeInZone_{i}") or 0 for i in range(1, 6)]

        avg_hr = s.get("averageHR")
        cadence_val = s.get("averageRunningCadenceInStepsPerMinute")
        # Flag activities that look like a walk/warm-up mislabeled as a run
        # (very low HR + very slow pace, or very low cadence) so they don't
        # skew the trend charts. They still show in the table.
        trend_eligible = True
        if pace and pace > 11 and avg_hr and avg_hr < 110:
            trend_eligible = False
        if cadence_val and cadence_val < 120:
            trend_eligible = False

        # Aerobic efficiency: metres travelled per heartbeat. Rising over
        # time = the same pace is costing fewer beats, i.e. easier running.
        efficiency = None
        if trend_eligible and dist_km >= MIN_KM_FOR_EFFICIENCY and dur_min and avg_hr:
            efficiency = round((dist_km * 1000 / dur_min) / avg_hr, 4)

        gap_speed = s.get("avgGradeAdjustedSpeed")
        gap_pace = (1000 / gap_speed) / 60 if gap_speed else None

        runs.append({
            "date": dt.strftime("%Y-%m-%d"),
            "datetime": dt.isoformat(),
            "name": s.get("activityName"),
            "dist_km": round(dist_km, 2),
            "dur_min": round(dur_min, 1),
            "pace": round(pace, 3) if pace else None,
            "pace_str": pace_str(pace),
            "gap_pace": round(gap_pace, 3) if gap_pace else None,
            "avg_hr": avg_hr,
            "max_hr": s.get("maxHR"),
            "cadence": round(cadence_val or 0, 1) or None,
            "max_cadence": s.get("maxRunningCadenceInStepsPerMinute"),
            "elev_gain": s.get("elevationGain"),
            "aerobic_te": s.get("aerobicTrainingEffect"),
            "anaerobic_te": s.get("anaerobicTrainingEffect"),
            "te_label": s.get("trainingEffectLabel"),
            "vo2max": s.get("vO2MaxValue"),
            "hr_zones": zones,
            "power_zones": pzones,
            "training_load": round(s.get("activityTrainingLoad") or 0) or None,
            "split_trend": split_trend,
            "decoupling": decoupling,
            "trend_eligible": trend_eligible,
            "efficiency": efficiency,
            "fastest_1k": s.get("fastestSplit_1000"),
            "fastest_mile": s.get("fastestSplit_1609"),
            "fastest_5k": s.get("fastestSplit_5000"),
            # --- running dynamics ---
            "gct": s.get("avgGroundContactTime"),
            "gct_balance": s.get("avgGroundContactBalance"),
            "vert_osc": s.get("avgVerticalOscillation"),
            "vert_ratio": s.get("avgVerticalRatio"),
            "stride_len": s.get("avgStrideLength"),
            "step_loss_pct": s.get("avgStepSpeedLossPercent"),
            # --- power & physiology ---
            "avg_power": s.get("avgPower"),
            "max_power": s.get("maxPower"),
            "norm_power": s.get("normPower"),
            "avg_resp": s.get("avgRespirationRate"),
            "max_resp": s.get("maxRespirationRate"),
            "calories": s.get("calories"),
            "steps": s.get("steps"),
            "bb_drain": s.get("differenceBodyBattery"),
            "vig_minutes": s.get("vigorousIntensityMinutes"),
            "laps": lap_profile,
        })

    runs.sort(key=lambda r: r["datetime"])
    if not runs:
        print("No runs found in export.")
        return

    # Identify the current training block -- the last continuous stretch where
    # no two runs sit more than BLOCK_GAP_DAYS apart. Garmin history reaches
    # back years for most people; regressing VO2max across a three-year gap
    # would describe a different athlete.
    BLOCK_GAP_DAYS = 60
    block_start_idx = 0
    for i in range(len(runs) - 1, 0, -1):
        prev = datetime.fromisoformat(runs[i - 1]["datetime"])
        cur = datetime.fromisoformat(runs[i]["datetime"])
        if (cur - prev).days > BLOCK_GAP_DAYS:
            block_start_idx = i
            break
    block_runs = runs[block_start_idx:]
    block_start = block_runs[0]["date"]
    for i, r in enumerate(runs):
        r["in_block"] = i >= block_start_idx

    first_dt = datetime.fromisoformat(block_runs[0]["datetime"])
    day_index = lambda r: (datetime.fromisoformat(r["datetime"]) - first_dt).days

    # ---------- weekly aggregation ----------
    weekly_map = defaultdict(lambda: {"km": 0.0, "runs": 0, "paces": [], "hrs": [],
                                      "zones": [0, 0, 0, 0, 0], "long": 0.0, "load": 0,
                                      "minutes": 0.0})
    for r in runs:
        dt = datetime.fromisoformat(r["datetime"])
        monday = dt - timedelta(days=dt.weekday())
        wk = monday.strftime("%Y-%m-%d")
        w = weekly_map[wk]
        w["km"] += r["dist_km"]
        w["runs"] += 1
        w["minutes"] += r["dur_min"]
        w["long"] = max(w["long"], r["dist_km"])
        w["load"] += r["training_load"] or 0
        if r["pace"]:
            w["paces"].append(r["pace"])
        if r["avg_hr"]:
            w["hrs"].append(r["avg_hr"])
        for i, z in enumerate(r["hr_zones"]):
            w["zones"][i] += z or 0

    # Fill gap weeks with zeros so the volume chart tells the truth about weeks
    # with no running -- but only across short gaps. Garmin history often has
    # multi-year holes (an old race, then nothing), and filling those would
    # bury the current block under hundreds of empty weeks.
    MAX_GAP_WEEKS = 6
    real_weeks = sorted(weekly_map.keys())
    for a, b in zip(real_weeks, real_weeks[1:]):
        da = datetime.strptime(a, "%Y-%m-%d")
        db = datetime.strptime(b, "%Y-%m-%d")
        gap = int((db - da).days / 7)
        if gap <= MAX_GAP_WEEKS:
            cur = da + timedelta(days=7)
            while cur < db:
                weekly_map[cur.strftime("%Y-%m-%d")]  # touch -> default
                cur += timedelta(days=7)

    weekly = []
    for wk in sorted(weekly_map.keys()):
        w = weekly_map[wk]
        z = w["zones"]
        total_z = sum(z)
        weekly.append({
            "week": wk,
            "km": round(w["km"], 1),
            "runs": w["runs"],
            "minutes": round(w["minutes"]),
            "long": round(w["long"], 1),
            "load": w["load"],
            "avg_pace": round(sum(w["paces"]) / len(w["paces"]), 2) if w["paces"] else None,
            "avg_hr": round(sum(w["hrs"]) / len(w["hrs"])) if w["hrs"] else None,
            "zones": z,
            # ordered intensity bands: easy (Z1-2) / moderate (Z3) / hard (Z4-5)
            "bands": [z[0] + z[1], z[2], z[3] + z[4]] if total_z else None,
        })

    # ---------- wellness ----------
    wellness = []
    for w in sorted(wellness_raw, key=lambda x: x.get("date") or ""):
        sleep = w.get("sleep") or {}
        stats = w.get("stats") or {}
        hrv = w.get("hrv") or {}
        readiness = w.get("readiness") or {}
        sleep_h = (sleep.get("sleepTimeSeconds") or 0) / 3600 if sleep else None
        wellness.append({
            "date": w.get("date"),
            "sleep_hours": round(sleep_h, 1) if sleep_h else None,
            "sleep_score": sleep.get("sleepScore"),
            "deep_min": round((sleep.get("deepSleepSeconds") or 0) / 60) or None,
            "rem_min": round((sleep.get("remSleepSeconds") or 0) / 60) or None,
            "resting_hr": stats.get("restingHeartRate"),
            "body_battery": stats.get("bodyBatteryMostRecentValue"),
            "body_battery_high": stats.get("bodyBatteryHighestValue"),
            "body_battery_low": stats.get("bodyBatteryLowestValue"),
            "body_battery_charged": stats.get("bodyBatteryChargedValue"),
            "body_battery_drained": stats.get("bodyBatteryDrainedValue"),
            "stress": stats.get("averageStressLevel"),
            "steps": stats.get("totalSteps"),
            "sleep_hrv": sleep.get("avgSleepHRV"),
            "hrv": hrv.get("lastNightAvg"),
            "hrv_weekly": hrv.get("weeklyAvg"),
            "hrv_status": hrv.get("status"),
            "readiness": readiness.get("score"),
            "readiness_level": readiness.get("level"),
            "recovery_time": readiness.get("recoveryTime"),
            "spo2": sleep.get("averageSpO2"),
            "respiration": sleep.get("averageRespiration"),
        })

    # ---------- personal bests ----------
    def best(field, label, fmt):
        candidates = [(r[field], r["date"]) for r in runs if r.get(field)]
        if not candidates:
            return None
        val, date = min(candidates, key=lambda x: x[0])
        return {"label": label, "value": fmt(val), "date": date, "raw": val}

    longest = max(runs, key=lambda r: r["dist_km"])
    biggest_week = max(weekly, key=lambda w: w["km"]) if weekly else None

    b1k = best("fastest_1k", "Fastest 1 km", secs_str)
    bmile = best("fastest_mile", "Fastest mile", secs_str)
    b5k = best("fastest_5k", "Fastest 5 km", clock_str)

    records = [r for r in [b1k, bmile, b5k] if r]
    records.append({"label": "Longest run", "value": f"{longest['dist_km']:.2f} km",
                    "date": longest["date"]})
    if biggest_week:
        records.append({"label": "Biggest week", "value": f"{biggest_week['km']:.1f} km",
                        "date": "week of " + biggest_week["week"]})

    # ---------- VO2max trend & projection ----------
    vo2_points = [(day_index(r), r["vo2max"]) for r in block_runs if r["vo2max"]]
    vo2_hist = [{"date": r["date"], "t": r["datetime"], "v": r["vo2max"]}
                for r in block_runs if r["vo2max"]]
    current_vo2 = vo2_hist[-1]["v"] if vo2_hist else None
    vo2_trend = linear_trend(vo2_points)
    observed_monthly = (vo2_trend["slope_per_day"] * 30.44) if vo2_trend else 0.0

    # What a *demonstrated* effort says, versus what VO2max claims is possible.
    best_effort = None
    if b5k:
        best_effort = (b5k["raw"], 5000, "your fastest 5 km split")
    elif bmile:
        best_effort = (bmile["raw"], 1609, "your fastest mile")
    elif b1k:
        best_effort = (b1k["raw"], 1000, "your fastest kilometre")

    # Garmin's VO2max describes capacity; it does not know whether you have
    # ever converted that capacity into a race. For a runner who has never
    # raced hard the two disagree badly, so every forward-looking race time is
    # scaled by how far apart they currently sit. The gap narrows as training
    # gets more specific -- it does not vanish on its own.
    calibration = 1.0
    if current_vo2 and best_effort:
        v_hm = race_seconds_from_vdot(current_vo2, HALF_MARATHON_M)
        r_hm = riegel_seconds(best_effort[0], best_effort[1], HALF_MARATHON_M)
        if v_hm and r_hm:
            calibration = r_hm / v_hm

    scenarios = []
    if current_vo2:
        # Three honest futures. `mult` scales the observed rate by how much
        # stimulus each scenario supplies; `closes` is how much of the current
        # capacity-to-performance gap that scenario actually closes.
        for key, label, mult, decay, closes, blurb in [
            ("maintain", "If nothing changes", 0.40, 0.78, 0.10,
             "Current pattern continues -- same volume, same inconsistency, no structured quality work."),
            ("plan", "Consistent + one quality session", 0.85, 0.88, 0.55,
             "Steady weekly volume, 80% easy running, one tempo or interval session each week."),
            ("push", "Structured build", 1.10, 0.91, 0.75,
             "Volume grows toward 40+ km/week with alternating tempo and interval work."),
        ]:
            proj = project_vo2max(current_vo2, max(observed_monthly * mult, 0.05), decay)
            end = proj[-1]["value"]
            raw_hm = race_seconds_from_vdot(end, HALF_MARATHON_M)
            # scenario calibration: the gap shrinks toward 1.0 by `closes`
            scen_calib = 1.0 + (calibration - 1.0) * (1 - closes)
            realistic = raw_hm * scen_calib if raw_hm else None
            scenarios.append({
                "key": key, "label": label, "blurb": blurb,
                "months": proj,
                "end_value": end,
                "delta": round(end - current_vo2, 1),
                "hm_optimistic": raw_hm,
                "hm_optimistic_str": clock_str(raw_hm),
                "hm_seconds": realistic,
                "hm_str": clock_str(realistic),
                "hm_pace": pace_str((realistic / 60) / HALF_MARATHON_KM) if realistic else None,
            })

    # ---------- race predictions ----------
    # Two independent estimates that deliberately disagree:
    #  * VDOT  -- what Garmin's VO2max implies you *could* run
    #  * Riegel -- what your demonstrated efforts say you *have* run
    # The spread between them is the honest uncertainty band.
    predictions = []
    dists = [("5 km", 5000), ("10 km", 10000), ("Half marathon", HALF_MARATHON_M),
             ("Marathon", 42195)]
    for label, d in dists:
        vdot_s = race_seconds_from_vdot(current_vo2, d) if current_vo2 else None
        rieg_s = riegel_seconds(best_effort[0], best_effort[1], d) if best_effort else None
        pair = [x for x in (vdot_s, rieg_s) if x]
        predictions.append({
            "label": label,
            "dist_m": d,
            "vdot_secs": vdot_s,
            "vdot_str": clock_str(vdot_s),
            "vdot_pace": pace_str((vdot_s / 60) / (d / 1000)) if vdot_s else None,
            "riegel_secs": rieg_s,
            "riegel_str": clock_str(rieg_s),
            "riegel_pace": pace_str((rieg_s / 60) / (d / 1000)) if rieg_s else None,
            "fast_secs": min(pair) if pair else None,
            "slow_secs": max(pair) if pair else None,
        })

    hm_pred = next(p for p in predictions if p["dist_m"] == HALF_MARATHON_M)

    # ---------- training load / injury risk ----------
    today = datetime.now()
    def load_between(days_back_from, days_back_to):
        lo = today - timedelta(days=days_back_from)
        hi = today - timedelta(days=days_back_to)
        return sum(r["training_load"] or 0 for r in block_runs
                   if lo <= datetime.fromisoformat(r["datetime"]) < hi)

    acute = load_between(7, 0)
    chronic_total = load_between(28, 0)
    chronic_weekly = chronic_total / 4 if chronic_total else 0
    acwr = round(acute / chronic_weekly, 2) if chronic_weekly else None
    if acwr is None:
        acwr_state, acwr_note = "unknown", "Not enough recent training load to judge."
    elif acwr < 0.8:
        acwr_state, acwr_note = "detraining", "Recent load is well below your established base -- fitness is drifting down rather than building."
    elif acwr <= 1.3:
        acwr_state, acwr_note = "good", "Recent load sits in the productive range relative to your established base."
    elif acwr <= 1.5:
        acwr_state, acwr_note = "warning", "You have ramped up faster than your base supports. Hold this week flat rather than adding more."
    else:
        acwr_state, acwr_note = "serious", "A sharp spike relative to your four-week base -- the range most associated with injury. Back off this week."

    # ---------- consistency ----------
    recent_weeks = weekly[-8:] if len(weekly) >= 8 else weekly
    weeks_with_3 = sum(1 for w in recent_weeks if w["runs"] >= 3)
    consistency_pct = round(100 * weeks_with_3 / len(recent_weeks)) if recent_weeks else 0
    avg_4wk_km = round(sum(w["km"] for w in weekly[-4:]) / max(1, len(weekly[-4:])), 1)

    # ---------- zone distribution ----------
    zone_totals = [0, 0, 0, 0, 0]
    for r in block_runs:
        for i, z in enumerate(r["hr_zones"]):
            zone_totals[i] += z or 0
    total_z = sum(zone_totals)
    easy_pct = 100 * (zone_totals[0] + zone_totals[1]) / total_z if total_z else 0
    mod_pct = 100 * zone_totals[2] / total_z if total_z else 0
    hard_pct = 100 * (zone_totals[3] + zone_totals[4]) / total_z if total_z else 0

    # ---------- half marathon readiness ----------
    longest_km = longest["dist_km"]
    comp = []
    comp.append({
        "label": "Long run",
        "detail": f"{longest_km:.1f} km of an {HM_LONG_RUN_TARGET_KM:.0f} km training target "
                  f"(the race itself is {HALF_MARATHON_KM:.1f} km -- plans peak short of it)",
        "pct": min(100, round(100 * longest_km / HM_LONG_RUN_TARGET_KM)),
        "weight": 35,
    })
    comp.append({
        "label": "Weekly volume",
        "detail": f"{avg_4wk_km:.0f} km/wk against {HM_VOLUME_TARGET_KM:.0f} km/wk",
        "pct": min(100, round(100 * avg_4wk_km / HM_VOLUME_TARGET_KM)),
        "weight": 25,
    })
    comp.append({
        "label": "Consistency",
        "detail": f"{weeks_with_3} of the last {len(recent_weeks)} weeks had 3+ runs",
        "pct": consistency_pct,
        "weight": 20,
    })
    comp.append({
        "label": "Aerobic base",
        "detail": f"{easy_pct:.0f}% of time easy against an {EASY_TARGET_PCT}% target",
        "pct": min(100, round(100 * easy_pct / EASY_TARGET_PCT)),
        "weight": 20,
    })
    readiness = round(sum(c["pct"] * c["weight"] for c in comp) / 100)
    if readiness >= 80:
        readiness_state = "good"
        readiness_line = "Race-ready. The work now is sharpening, not building."
    elif readiness >= 55:
        readiness_state = "warning"
        readiness_line = "The engine is there; the long run and weekly volume still need building."
    else:
        readiness_state = "serious"
        readiness_line = "Early days. Consistent weekly volume is the thing standing between you and the distance."

    # ---------- long run build plan ----------
    # ~1 km per week with every third week cut back, which is what the
    # connective tissue -- not the heart -- can actually absorb.
    plan = []
    cur_long = longest_km
    wk_no = 0
    week_cursor = datetime.now()
    while cur_long < HM_LONG_RUN_TARGET_KM and wk_no < 40:
        wk_no += 1
        week_cursor += timedelta(days=7)
        if wk_no % 3 == 0:
            target = round(cur_long * 0.7, 1)   # cutback week
            kind = "cutback"
        else:
            target = round(min(cur_long + 1.2, cur_long * 1.12), 1)
            cur_long = target
            kind = "build"
        plan.append({"week": wk_no, "date": week_cursor.strftime("%Y-%m-%d"),
                     "km": target, "kind": kind})
    weeks_to_ready = wk_no if wk_no < 40 else None
    race_ready_date = (datetime.now() + timedelta(weeks=weeks_to_ready + 3)).strftime("%B %Y") \
        if weeks_to_ready else None

    # ---------- race pacing card ----------
    pacing = None
    if hm_pred["slow_secs"]:
        target_secs = hm_pred["slow_secs"]
        target_pace_min = (target_secs / 60) / HALF_MARATHON_KM
        pacing = {
            "target_str": clock_str(target_secs),
            "pace_str": pace_str(target_pace_min),
            "splits": [
                {"label": "km 1-5", "pace": pace_str(target_pace_min + 0.12),
                 "note": "deliberately slower than goal -- bank patience, not time"},
                {"label": "km 6-15", "pace": pace_str(target_pace_min),
                 "note": "settle onto goal pace and hold it"},
                {"label": "km 16-21.1", "pace": pace_str(target_pace_min - 0.08),
                 "note": "if anything is left, this is where it goes"},
            ],
        }

    # ---------- running dynamics with benchmarks ----------
    def recent_avg(field, n=10, elig=True):
        vals = [r[field] for r in block_runs[-n:] if r.get(field) and (r["trend_eligible"] or not elig)]
        return sum(vals) / len(vals) if vals else None

    def dyn_trend(field):
        pts = [(day_index(r), r[field]) for r in block_runs if r.get(field) and r["trend_eligible"]]
        return linear_trend(pts)

    def band_of(value, bands):
        """bands: list of (upper_bound_or_None, label, state); first match wins."""
        if value is None:
            return None, None
        for upper, label, state in bands:
            if upper is None or value <= upper:
                return label, state
        return None, None

    # A projection is only shown when the underlying trend is actually
    # trustworthy. Extrapolating a noisy slope six months forward is how you
    # end up predicting negative braking loss.
    MIN_R2 = 0.20
    MIN_N = 8
    DAMPING = 0.6   # ~15 weeks of data does not justify a full-strength
                    # 6-month linear extrapolation

    dynamics = []
    for field, label, unit, fmt, lower_better, bands, limits, why in [
        ("cadence", "Cadence", "spm", lambda v: f"{v:.0f}", False,
         [(160, "Low", "serious"), (170, "Developing", "warning"),
          (180, "Good", "good"), (None, "High", "good")], (150, 190),
         "Steps per minute. Higher turnover shortens your stride, which cuts braking force and the load on each footstrike."),
        ("gct", "Ground contact time", "ms", lambda v: f"{v:.0f}", True,
         [(218, "Elite", "good"), (248, "Good", "good"),
          (270, "Average", "warning"), (None, "Long", "serious")], (190, 340),
         "How long each foot stays down. Shorter contact means less time braking and more time travelling."),
        ("vert_ratio", "Vertical ratio", "%", lambda v: f"{v:.1f}", True,
         [(6.1, "Elite", "good"), (7.4, "Good", "good"),
          (8.6, "Average", "warning"), (None, "High", "serious")], (5.5, 12.0),
         "Bounce measured against stride length -- the single best economy number Garmin gives you. Low means your energy goes forward, not upward."),
        ("vert_osc", "Vertical oscillation", "cm", lambda v: f"{v:.1f}", True,
         [(6.7, "Elite", "good"), (8.0, "Good", "good"),
          (9.5, "Average", "warning"), (None, "High", "serious")], (5.5, 12.0),
         "How far your centre of mass rises each step. Every centimetre up is energy not spent going forward."),
        ("stride_len", "Stride length", "cm", lambda v: f"{v:.0f}", False,
         [(90, "Short", "warning"), (110, "Moderate", "good"),
          (None, "Long", "good")], (85, 135),
         "Distance covered per step. Grows naturally with fitness -- chasing it directly causes overstriding."),
        ("step_loss_pct", "Braking loss", "%", lambda v: f"{v:.1f}", True,
         [(5.0, "Low", "good"), (7.5, "Moderate", "warning"),
          (None, "High", "serious")], (2.0, 12.0),
         "Speed lost at each footstrike. Largely a consequence of overstriding, so it falls as cadence rises."),
        ("gct_balance", "L/R balance", "%", lambda v: f"{v:.1f}", False,
         [(48.5, "Asymmetric", "warning"), (51.5, "Balanced", "good"),
          (None, "Asymmetric", "warning")], (46.0, 54.0),
         "Share of ground contact on the left foot. Persistent drift from 50% can flag a niggle before you feel it."),
        ("avg_resp", "Respiration rate", "brpm", lambda v: f"{v:.0f}", True,
         [(25, "Low", "good"), (32, "Moderate", "warning"),
          (None, "High", "serious")], (16, 48),
         "Breaths per minute while running. Falls at a given pace as aerobic fitness improves."),
        ("avg_power", "Running power", "W", lambda v: f"{v:.0f}", False,
         [(None, "Recorded", "good")], (180, 460),
         "Mechanical work rate. Unlike pace it is immune to hills and wind, which makes it the fairest effort comparison across routes."),
    ]:
        cur = recent_avg(field)
        if cur is None:
            continue
        tr = dyn_trend(field)
        label_band, state = band_of(cur, bands)

        trustworthy = bool(tr and tr["r2"] >= MIN_R2 and tr["n"] >= MIN_N)
        slope6 = projected = improving = None
        if trustworthy:
            slope6 = tr["slope_per_day"] * 182.5 * DAMPING
            lo, hi = limits
            projected_raw = max(lo, min(hi, cur + slope6))
            slope6 = projected_raw - cur          # report the clamped movement
            projected = fmt(projected_raw)
            if abs(slope6) > 1e-9:
                improving = (slope6 < 0) if lower_better else (slope6 > 0)

        dynamics.append({
            "field": field, "label": label, "unit": unit,
            "value": fmt(cur), "raw": round(cur, 2),
            "band": label_band, "state": state, "why": why,
            "trend_per_6mo": round(slope6, 2) if slope6 is not None else None,
            "projected": projected,
            "improving": improving,
            "trustworthy": trustworthy,
            "r2": tr["r2"] if tr else None,
            "n": tr["n"] if tr else 0,
            "lower_better": lower_better,
        })

    # ---------- efficiency trend ----------
    eff_points = [(day_index(r), r["efficiency"]) for r in block_runs if r["efficiency"]]
    eff_trend = linear_trend(eff_points)
    eff_runs = [r for r in block_runs if r["efficiency"]]

    # ---------- insights ----------
    insights = []

    if total_z > 0 and easy_pct < 50:
        insights.append({
            "type": "warning", "tab": "log",
            "title": "Almost none of your running is truly easy",
            "body": f"Only {easy_pct:.0f}% of your heart-rate time is in zones 1-2, while "
                    f"{mod_pct:.0f}% sits in zone 3 and {hard_pct:.0f}% in zones 4-5. Endurance builds "
                    f"fastest when the large majority of running is easy enough to hold a conversation, "
                    f"with one deliberately harder session a week. This is the single biggest lever you have."
        })
    elif total_z > 0:
        insights.append({
            "type": "good", "tab": "log",
            "title": "Healthy intensity balance",
            "body": f"{easy_pct:.0f}% of your training time is easy (zones 1-2), which gives your harder "
                    f"efforts room to actually count."
        })

    if len(eff_runs) >= 6:
        early = eff_runs[:3]
        recent = eff_runs[-3:]
        early_avg = sum(r["efficiency"] for r in early) / len(early)
        recent_avg_eff = sum(r["efficiency"] for r in recent) / len(recent)
        change = 100 * (recent_avg_eff - early_avg) / early_avg
        if change > 2:
            insights.append({
                "type": "good", "tab": "fitness",
                "title": f"Your running is getting measurably easier (+{change:.0f}%)",
                "body": f"You now cover {recent_avg_eff:.2f} m per heartbeat versus {early_avg:.2f} when you "
                        f"started -- the same pace simply costs you fewer beats than it used to."
            })
        elif change < -2:
            insights.append({
                "type": "info", "tab": "fitness",
                "title": f"Aerobic efficiency has dipped ({change:.0f}%)",
                "body": "Recent runs cost more heartbeats per metre than your early ones. A short spell of "
                        "this is normal after a break, heat, or hard weeks."
            })

    # the VDOT vs demonstrated gap -- the most useful thing in the whole build
    if current_vo2 and best_effort and hm_pred["vdot_secs"] and hm_pred["riegel_secs"]:
        gap_min = (hm_pred["riegel_secs"] - hm_pred["vdot_secs"]) / 60
        if gap_min > 12:
            insights.append({
                "type": "info", "tab": "fitness",
                "title": "Your engine is ahead of your race results",
                "body": f"Garmin's VO2max of {current_vo2:.0f} implies a {hm_pred['vdot_str']} half marathon, "
                        f"while extrapolating {best_effort[2]} gives {hm_pred['riegel_str']} -- a "
                        f"{gap_min:.0f} minute spread. That gap is not a contradiction: VO2max measures "
                        f"capacity, and your best efforts were run inside easy training, never as a hard "
                        f"test. Race a 5 km properly and the true number will land between the two."
            })

    hard_sessions = sum(1 for r in block_runs if (r["anaerobic_te"] or 0) >= 1.5)
    if hard_sessions <= 3 and len(block_runs) >= 15:
        insights.append({
            "type": "info", "tab": "fitness",
            "title": f"Only {hard_sessions} structured hard sessions in {len(block_runs)} runs",
            "body": "Nearly all of your fitness so far has come from easy aerobic volume, which means the "
                    "quality-work lever is still almost entirely untouched. Adding one tempo or interval "
                    "session a week should produce faster gains than more easy volume alone."
        })

    if acwr and acwr_state in ("warning", "serious"):
        insights.append({
            "type": "warning" if acwr_state == "warning" else "warning",
            "tab": "fitness",
            "title": f"Training load ratio at {acwr:.2f}",
            "body": acwr_note,
        })

    positive_splits = sum(1 for r in block_runs if r["split_trend"] == "positive")
    negative_splits = sum(1 for r in block_runs if r["split_trend"] == "negative")
    scored = positive_splits + negative_splits
    if scored >= 5 and positive_splits / scored > 0.6:
        insights.append({
            "type": "warning", "tab": "hm",
            "title": "Most runs fade in the second half",
            "body": f"{positive_splits} of {scored} runs with lap data got slower over the back half. Starting "
                    f"10-15 sec/km slower than feels natural usually flips this, and it is exactly the habit "
                    f"that decides whether the last 5 km of a half marathon holds together."
        })

    if consistency_pct < 70 and len(recent_weeks) >= 4:
        insights.append({
            "type": "warning", "tab": "hm",
            "title": "Week-to-week consistency is the weak link",
            "body": f"Only {weeks_with_3} of your last {len(recent_weeks)} weeks had three or more runs, and "
                    f"weekly volume has swung between {min(w['km'] for w in recent_weeks):.0f} and "
                    f"{max(w['km'] for w in recent_weeks):.0f} km. Aerobic adaptation responds to repetition far "
                    f"more than to any single big week."
        })

    # ---------- latest-run snapshot ----------
    # The dashboard is a form/economy snapshot, so its headline numbers must
    # describe the latest run. Recent averages are context, never substitutes
    # for the measured value shown.
    latest = block_runs[-1]
    previous = next((r for r in reversed(block_runs[:-1]) if r["trend_eligible"]), None)

    def avg_before_latest(field, n=10):
        values = [r.get(field) for r in block_runs[:-1]
                  if r.get(field) is not None and r["trend_eligible"]]
        values = values[-n:]
        return sum(values) / len(values) if values else None

    def metric_snapshot(field, label, unit, digits=1, lower_better=None):
        value = latest.get(field)
        baseline = avg_before_latest(field)
        delta = value - baseline if value is not None and baseline is not None else None
        return {
            "field": field,
            "label": label,
            "unit": unit,
            "value": round(value, digits) if value is not None else None,
            "baseline": round(baseline, digits) if baseline is not None else None,
            "delta": round(delta, digits) if delta is not None else None,
            "lower_better": lower_better,
        }

    cadence = metric_snapshot("cadence", "Cadence", "spm", 1, None)
    if cadence["value"] is None:
        cadence_state, cadence_band = "neutral", "Not recorded"
    elif cadence["value"] < 160:
        cadence_state, cadence_band = "neutral", "Relaxed turnover"
    elif cadence["value"] < 170:
        cadence_state, cadence_band = "good", "Quick turnover"
    else:
        cadence_state, cadence_band = "good", "Fast turnover"
    cadence.update({"state": cadence_state, "band": cadence_band})
    if previous and latest.get("cadence") is not None and previous.get("cadence") is not None:
        cadence["delta_previous"] = round(latest["cadence"] - previous["cadence"], 1)
    else:
        cadence["delta_previous"] = None

    economy_metrics = [
        metric_snapshot("vert_ratio", "Vertical ratio", "%", 1, True),
        metric_snapshot("gct", "Ground contact", "ms", 0, True),
        metric_snapshot("efficiency", "Aerobic efficiency", "m/beat", 2, False),
        metric_snapshot("vert_osc", "Vertical motion", "cm", 1, True),
        metric_snapshot("stride_len", "Stride length", "cm", 0, None),
        metric_snapshot("avg_power", "Running power", "W", 0, None),
    ]

    snapshot = {
        "run": latest,
        "previous_run": previous,
        "cadence": cadence,
        "economy": economy_metrics,
        "recent_runs": list(reversed(block_runs[-8:])),
    }

    payload = {
        "generated_at": datetime.now().isoformat(),
        "runs": runs,
        "weekly": weekly,
        "wellness": wellness,
        "training_status": training_status,
        "insights": insights,
        "records": records,
        "zone_totals": zone_totals,
        "block_start": block_start,
        "block_run_count": len(block_runs),
        "bands_all": [easy_pct, mod_pct, hard_pct],
        "snapshot": snapshot,
        "hm": {
            "target_km": HALF_MARATHON_KM,
            "longest_km": round(longest_km, 2),
            "pct": round(100 * longest_km / HALF_MARATHON_KM, 1),
            "readiness": readiness,
            "readiness_state": readiness_state,
            "readiness_line": readiness_line,
            "components": comp,
            "plan": plan,
            "weeks_to_ready": weeks_to_ready,
            "race_ready_date": race_ready_date,
            "prediction": hm_pred,
            "pacing": pacing,
            "long_run_target": HM_LONG_RUN_TARGET_KM,
            "volume_target": HM_VOLUME_TARGET_KM,
            "avg_4wk_km": avg_4wk_km,
        },
        "fitness": {
            "vo2max": current_vo2,
            "vo2_history": vo2_hist,
            "vo2_trend": vo2_trend,
            "vo2_monthly": round(observed_monthly, 3),
            "scenarios": scenarios,
            "predictions": predictions,
            "best_effort_label": best_effort[2] if best_effort else None,
            "dynamics": dynamics,
            "acwr": acwr,
            "acwr_state": acwr_state,
            "acwr_note": acwr_note,
            "acute_load": round(acute),
            "chronic_load": round(chronic_weekly),
            "consistency_pct": consistency_pct,
            "avg_4wk_km": avg_4wk_km,
            "eff_trend": eff_trend,
            "calibration": round(calibration, 3),
            "hard_sessions": hard_sessions,
        },
    }

    template_path = Path(__file__).parent / "dashboard_template.html"
    template = template_path.read_text()
    html = template.replace("__DATA_JSON__", json.dumps(payload, default=str))

    Path(out_path).write_text(html)
    print(f"Wrote {out_path} ({len(runs)} runs, {len(weekly)} weeks, "
          f"{len(dynamics)} dynamics metrics, {len(scenarios)} projection scenarios)")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: python3 build_dashboard.py <export.json> <output.html>")
        sys.exit(1)
    build(sys.argv[1], sys.argv[2])
