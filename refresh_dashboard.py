#!/usr/bin/env python3
"""
Refresh the local Garmin dashboard without an AI assistant.

The first run fetches full history. Later runs fetch a recent window and merge
it into the existing export, replacing activities Garmin has updated.

Usage:
    ./venv/bin/python refresh_dashboard.py
    ./venv/bin/python refresh_dashboard.py --full
    ./venv/bin/python refresh_dashboard.py --no-open
"""
import argparse
import csv
import json
import math
import sys
import webbrowser
from datetime import datetime
from pathlib import Path

from build_dashboard import build
from fetch_activities import fetch
from publish_dashboard import is_pages_configured, publish


BASE = Path(__file__).resolve().parent
DATA_PATH = BASE / "data" / "latest_export.json"
DASHBOARD_PATH = BASE / "dashboard.html"
SUMMARY_PATH = BASE / "data" / "summary.csv"


def activity_key(item):
    summary = item.get("summary") or {}
    activity_id = summary.get("activityId")
    if activity_id is not None:
        return f"id:{activity_id}"
    return "fallback:{date}:{name}".format(
        date=summary.get("startTimeLocal"),
        name=summary.get("activityName"),
    )


def merge_exports(existing, fresh):
    """Merge a recent fetch into history, preferring freshly fetched records."""
    activities = {
        activity_key(item): item
        for item in existing.get("activities", [])
    }
    activities.update({
        activity_key(item): item
        for item in fresh.get("activities", [])
    })

    wellness = {
        item.get("date"): item
        for item in existing.get("wellness", [])
        if item.get("date")
    }
    wellness.update({
        item.get("date"): item
        for item in fresh.get("wellness", [])
        if item.get("date")
    })

    merged_activities = sorted(
        activities.values(),
        key=lambda item: (item.get("summary") or {}).get("startTimeLocal") or "",
        reverse=True,
    )
    merged_wellness = sorted(
        wellness.values(),
        key=lambda item: item.get("date") or "",
        reverse=True,
    )
    return {
        "fetched_at": fresh.get("fetched_at"),
        "activities": merged_activities,
        "wellness": merged_wellness,
        "training_status": fresh.get("training_status") or existing.get("training_status"),
    }


def write_json_atomic(path, payload):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, default=str))
    temporary.replace(path)


def write_summary_csv(payload):
    """Keep the spreadsheet export aligned with the merged dashboard history."""
    with SUMMARY_PATH.open("w", newline="") as file:
        writer = csv.writer(file)
        writer.writerow([
            "date", "name", "distance_km", "duration_min",
            "avg_pace_min_per_km", "avg_hr", "max_hr", "cadence",
            "elevation_gain_m", "aerobic_te", "anaerobic_te",
        ])
        for item in payload.get("activities", []):
            activity = item.get("summary") or {}
            distance_km = (activity.get("distance") or 0) / 1000
            duration_min = (activity.get("duration") or 0) / 60
            pace = duration_min / distance_km if distance_km else None
            writer.writerow([
                activity.get("startTimeLocal"),
                activity.get("activityName"),
                round(distance_km, 2),
                round(duration_min, 1),
                round(pace, 2) if pace else "",
                activity.get("averageHR"),
                activity.get("maxHR"),
                activity.get("averageRunningCadenceInStepsPerMinute"),
                activity.get("elevationGain"),
                activity.get("aerobicTrainingEffect"),
                activity.get("anaerobicTrainingEffect"),
            ])


def validate_dashboard():
    if not DASHBOARD_PATH.exists():
        raise RuntimeError("dashboard.html was not generated")
    html = DASHBOARD_PATH.read_text()
    if "__DATA_JSON__" in html:
        raise RuntimeError("dashboard data placeholder was not replaced")
    if 'id="dashboard-data"' not in html:
        raise RuntimeError("dashboard data block is missing")


def adaptive_fetch_days(existing, minimum_days):
    """Cover the time since the last sync, plus a week of overlap."""
    if not existing or not existing.get("fetched_at"):
        return 3650
    try:
        last_sync = datetime.fromisoformat(existing["fetched_at"])
        if last_sync.tzinfo is not None:
            last_sync = last_sync.astimezone().replace(tzinfo=None)
        elapsed_days = max(0, math.ceil((datetime.now() - last_sync).total_seconds() / 86400))
    except (TypeError, ValueError):
        return 3650
    return min(3650, max(minimum_days, elapsed_days + 7))


def refresh(recent_days, wellness_days, full, open_dashboard):
    pages_enabled = is_pages_configured()
    total_steps = 4 if pages_enabled else 3
    existing = None
    if DATA_PATH.exists():
        try:
            existing = json.loads(DATA_PATH.read_text())
        except (OSError, json.JSONDecodeError) as error:
            print(f"Existing export cannot be reused: {error}", file=sys.stderr)

    fetch_days = 3650 if full else adaptive_fetch_days(existing, recent_days)
    mode = "full history" if fetch_days == 3650 else f"latest {fetch_days} days"
    print(f"[1/{total_steps}] Connecting to Garmin and fetching {mode}...")
    fetch(fetch_days, count=0, wellness_days=wellness_days)

    fresh = json.loads(DATA_PATH.read_text())
    if existing is not None and not full:
        print(f"[2/{total_steps}] Merging fresh records into local history...")
        payload = merge_exports(existing, fresh)
        write_json_atomic(DATA_PATH, payload)
    else:
        print(f"[2/{total_steps}] Full local history is up to date...")
        payload = fresh

    write_summary_csv(payload)
    print(f"[3/{total_steps}] Rebuilding the running form dashboard...")
    build(DATA_PATH, DASHBOARD_PATH)
    validate_dashboard()

    if pages_enabled:
        print(f"[4/{total_steps}] Encrypting and publishing GitHub Pages...")
        publish(push=True)

    run_count = len(payload.get("activities", []))
    wellness_count = len(payload.get("wellness", []))
    print(f"\nDone: {run_count} runs and {wellness_count} wellness days.")
    print(f"Dashboard: {DASHBOARD_PATH}")

    if open_dashboard:
        webbrowser.open(DASHBOARD_PATH.as_uri())


def main():
    parser = argparse.ArgumentParser(
        description="Fetch Garmin data and rebuild the local dashboard."
    )
    parser.add_argument(
        "--recent-days",
        type=int,
        default=45,
        help="Minimum activity overlap on normal refreshes (default: 45)",
    )
    parser.add_argument(
        "--wellness-days",
        type=int,
        default=30,
        help="Recent sleep/recovery window (default: 30)",
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help="Re-fetch the complete ten-year activity history",
    )
    parser.add_argument(
        "--no-open",
        action="store_true",
        help="Do not open dashboard.html after rebuilding it",
    )
    args = parser.parse_args()

    if args.recent_days < 1 or args.wellness_days < 0:
        parser.error("day ranges must be positive")

    try:
        refresh(
            recent_days=args.recent_days,
            wellness_days=args.wellness_days,
            full=args.full,
            open_dashboard=not args.no_open,
        )
    except KeyboardInterrupt:
        print("\nRefresh cancelled.", file=sys.stderr)
        sys.exit(130)
    except Exception as error:
        print(f"\nRefresh failed: {error}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
