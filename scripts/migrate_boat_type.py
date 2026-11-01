#!/usr/bin/env python3
"""Give existing runs a boat class (and optionally a boat profile) in interval_meta.

Runs recorded before the app sent a boat class have none; the charts then draw them with the
fallback class. This writes `boat` (and `profile` if given) for every live interval that has no
boat yet. Dry run by default: prints what it would write.

    .venv/bin/python scripts/migrate_boat_type.py --boat 2x [--profile <id>] [--apply]

Run it after the cleanup of runs that are going to be deleted, so only kept runs are migrated.
Uses the settings from .env (local clone) unless INFLUX_* variables point elsewhere.
"""
import argparse
import sys

sys.path.insert(0, ".")
from srow import boats
from srow.config import load_settings
from srow.services import InfluxService


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--boat", required=True, choices=boats.CLASSES)
    ap.add_argument("--profile", default="", help="profile id to assign as well (must exist)")
    ap.add_argument("--apply", action="store_true", help="write; without it only print")
    a = ap.parse_args()
    svc = InfluxService(load_settings())
    meta = svc.fetch_interval_meta()
    profiles, _ = svc.fetch_boat_profiles()
    if a.profile and a.profile not in profiles:
        print(f"profile {a.profile!r} does not exist"); return 2
    todo = [iv["value"] for iv in svc.fetch_interval_tags() if not meta.get(iv["value"], {}).get("boat")]
    print(f"{len(todo)} interval(s) without a boat class (bucket {svc.settings.bucket})")
    for iv in todo:
        name = meta.get(iv, {}).get("name", "")
        print(f"  {iv}  {name}  -> boat={a.boat}" + (f" profile={a.profile}" if a.profile else ""))
        if a.apply:
            svc.set_interval_meta(iv, boat=a.boat, profile=(a.profile or None))
    print("written" if a.apply else "dry run, nothing written (add --apply)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
