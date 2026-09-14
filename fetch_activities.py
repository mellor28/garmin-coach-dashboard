#!/usr/bin/env python3
"""
Fetch recent running data from Garmin Connect using the cached session
created by login_setup.py. Safe to run repeatedly -- it does NOT need your
password, only the local garmin_tokens/ cache created during setup.

Usage:
    python3 fetch_activities.py --days 14
    python3 fetch_activities.py --count 5

Writes:
    data/export_<timestamp>.json   (full detail, for Claude to analyze)
    data/latest_export.json        (always the most recent fetch)
    data/summary.csv               (quick spreadsheet-friendly overview)
"""
import argparse
import csv
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

from garminconnect import Garmin

BASE = Path(__file__).parent
TOKENSTORE = BASE / "garmin_tokens"
DATA_DIR = BASE / "data"


def connect():
    if not TOKENSTORE.exists():
        print("No cached Garmin session found in garmin_tokens/.", file=sys.stderr)
        print("Run login_setup.py yourself first (see README.md).", file=sys.stderr)
        sys.exit(1)
    garmin = Garmin()
    try:
        garmin.login(str(TOKENSTORE))
    except Exception as e:
        print(f"Could not resume cached Garmin session: {e}", file=sys.stderr)
        print("Your session may have expired. Run login_setup.py again.", file=sys.stderr)
        sys.exit(1)
    return garmin


def is_running(activity):
    t = ((activity.get("activityType") or {}).get("typeKey") or "").lower()
    return "running" in t


def fetch(days, count, wellness_days):
    garmin = connect()
    DATA_DIR.mkdir(exist_ok=True)

    cutoff = datetime.now() - timedelta(days=days) if days else None

    # Page through activities (Garmin returns newest-first) until we've
    # covered the requested date range / count, or hit a safety cap.
    selected = []
    batch_size = 200
    safety_cap = 4000
    start = 0
    while start < safety_cap:
        batch = garmin.get_activities(start, batch_size)
        if not batch:
            break
        stop = False
        for act in batch:
            if not is_running(act):
                continue
            act_start = act.get("startTimeLocal")
            if cutoff and act_start:
                try:
                    dt = datetime.strptime(act_start, "%Y-%m-%d %H:%M:%S")
                    if dt < cutoff:
                        stop = True
                        break
                except ValueError:
                    pass
            selected.append(act)
            if count and len(selected) >= count:
                stop = True
                break
        start += batch_size
        if stop or len(batch) < batch_size:
            break

    enriched = []
    for act in selected:
        activity_id = act.get("activityId")
        splits = {}
        try:
            splits = garmin.get_activity_splits(activity_id)
        except Exception as e:
            print(f"  warning: no splits for activity {activity_id}: {e}", file=sys.stderr)

        enriched.append({"summary": act, "splits": splits})
        print(f"Fetched: {act.get('activityName')} on {act.get('startTimeLocal')}")

    # Best-effort recent wellness context (sleep, resting HR, body battery).
    wellness = []
    today = datetime.now().date()
    for i in range(wellness_days):
        d = today - timedelta(days=i)
        entry = {"date": d.isoformat()}
        try:
            sleep = garmin.get_sleep_data(d.isoformat()) or {}
            dto = sleep.get("dailySleepDTO") or {}
            if dto:
                scores = dto.get("sleepScores") or {}
                overall = scores.get("overall") or {}
                entry["sleep"] = {
                    "sleepTimeSeconds": dto.get("sleepTimeSeconds"),
                    "deepSleepSeconds": dto.get("deepSleepSeconds"),
                    "lightSleepSeconds": dto.get("lightSleepSeconds"),
                    "remSleepSeconds": dto.get("remSleepSeconds"),
                    "awakeSleepSeconds": dto.get("awakeSleepSeconds"),
                    "sleepScore": overall.get("value"),
                    "avgSleepHRV": dto.get("avgSleepHRV"),
                    "averageSpO2": dto.get("averageSpO2Value") or dto.get("avgSpO2"),
                    "averageRespiration": dto.get("averageRespirationValue"),
                    "avgSleepStress": dto.get("avgSleepStress"),
                }
        except Exception:
            pass
        try:
            stats = garmin.get_stats(d.isoformat()) or {}
            entry["stats"] = {
                "restingHeartRate": stats.get("restingHeartRate"),
                "bodyBatteryMostRecentValue": stats.get("bodyBatteryMostRecentValue"),
                "bodyBatteryHighestValue": stats.get("bodyBatteryHighestValue"),
                "bodyBatteryLowestValue": stats.get("bodyBatteryLowestValue"),
                "bodyBatteryChargedValue": stats.get("bodyBatteryChargedValue"),
                "bodyBatteryDrainedValue": stats.get("bodyBatteryDrainedValue"),
                "averageStressLevel": stats.get("averageStressLevel") or stats.get("avgStressLevel"),
                "maxStressLevel": stats.get("maxStressLevel"),
                "totalSteps": stats.get("totalSteps"),
                "totalKilocalories": stats.get("totalKilocalories"),
                "vigorousIntensityMinutes": stats.get("vigorousIntensityMinutes"),
                "moderateIntensityMinutes": stats.get("moderateIntensityMinutes"),
            }
        except Exception:
            pass
        try:
            hrv = garmin.get_hrv_data(d.isoformat()) or {}
            summary = hrv.get("hrvSummary") or {}
            if summary:
                entry["hrv"] = {
                    "lastNightAvg": summary.get("lastNightAvg"),
                    "weeklyAvg": summary.get("weeklyAvg"),
                    "lastNight5MinHigh": summary.get("lastNight5MinHigh"),
                    "status": summary.get("status"),
                }
        except Exception:
            pass
        try:
            readiness_raw = garmin.get_training_readiness(d.isoformat()) or []
            if isinstance(readiness_raw, dict):
                readiness = readiness_raw
            elif readiness_raw:
                readiness = max(
                    readiness_raw,
                    key=lambda item: item.get("timestamp") or item.get("timestampLocal") or "",
                )
            else:
                readiness = {}
            if readiness.get("score") is not None:
                entry["readiness"] = {
                    "score": readiness.get("score"),
                    "level": readiness.get("level"),
                    "feedbackShort": readiness.get("feedbackShort"),
                    "recoveryTime": readiness.get("recoveryTime"),
                }
        except Exception:
            pass
        wellness.append(entry)

    # Training status is a current snapshot, so one call per refresh is enough.
    training_status = {}
    try:
        raw_status = garmin.get_training_status(today.isoformat()) or {}
        vo2 = ((raw_status.get("mostRecentVO2Max") or {}).get("generic")) or {}
        status_map = (((raw_status.get("mostRecentTrainingStatus") or {})
                       .get("latestTrainingStatusData")) or {})
        status_entry = next(iter(status_map.values()), {}) if isinstance(status_map, dict) else {}
        load = status_entry.get("acuteTrainingLoadDTO") or {}
        training_status = {
            "vo2max": vo2.get("vo2MaxPreciseValue") or vo2.get("vo2MaxValue"),
            "fitnessAge": vo2.get("fitnessAge"),
            "status": status_entry.get("trainingStatus"),
            "feedback": status_entry.get("trainingStatusFeedbackPhrase"),
            "fitnessTrend": status_entry.get("fitnessTrend"),
            "acwr": load.get("dailyAcuteChronicWorkloadRatio"),
            "acuteLoad": load.get("dailyTrainingLoadAcute"),
            "chronicLoad": load.get("dailyTrainingLoadChronic"),
            "acwrStatus": load.get("acwrStatus"),
        }
    except Exception:
        pass

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    payload = {
        "fetched_at": datetime.now().isoformat(),
        "activities": enriched,
        "wellness": wellness,
        "training_status": training_status,
    }

    out_path = DATA_DIR / f"export_{timestamp}.json"
    latest_path = DATA_DIR / "latest_export.json"
    for p in (out_path, latest_path):
        with open(p, "w") as f:
            json.dump(payload, f, indent=2, default=str)

    csv_path = DATA_DIR / "summary.csv"
    with open(csv_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["date", "name", "distance_km", "duration_min", "avg_pace_min_per_km",
                          "avg_hr", "max_hr", "cadence", "elevation_gain_m",
                          "aerobic_te", "anaerobic_te"])
        for item in enriched:
            a = item["summary"]
            dist_km = (a.get("distance") or 0) / 1000
            dur_min = (a.get("duration") or 0) / 60
            pace = (dur_min / dist_km) if dist_km else None
            writer.writerow([
                a.get("startTimeLocal"), a.get("activityName"),
                round(dist_km, 2), round(dur_min, 1),
                round(pace, 2) if pace else "",
                a.get("averageHR"), a.get("maxHR"),
                a.get("averageRunningCadenceInStepsPerMinute"),
                a.get("elevationGain"), a.get("aerobicTrainingEffect"),
                a.get("anaerobicTrainingEffect"),
            ])

    print(f"\nWrote {len(enriched)} running activities to:")
    print(f"  {out_path}")
    print(f"  {latest_path}")
    print(f"  {csv_path}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=14, help="How many days back to fetch (default 14; use a large number like 3650 for full history)")
    parser.add_argument("--count", type=int, default=0, help="Max number of activities (0 = no cap besides --days)")
    parser.add_argument("--wellness-days", type=int, default=30, help="How many recent days of sleep/recovery data to include (default 30, independent of --days)")
    args = parser.parse_args()
    fetch(args.days, args.count, args.wellness_days)


if __name__ == "__main__":
    main()
