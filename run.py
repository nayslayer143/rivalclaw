#!/usr/bin/env python3
"""RivalClaw CLI — entry point for cron and manual runs."""
import os
import sys
import fcntl
import argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

# Load .env if present
env_file = Path(__file__).parent / ".env"
if env_file.exists():
    for line in env_file.read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())

import simulator

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="RivalClaw arb-only paper trader")
    parser.add_argument("--migrate", action="store_true", help="Create DB tables")
    parser.add_argument("--run", action="store_true", help="Run simulation loop")
    parser.add_argument("--tune", action="store_true", help="Run self-tuning cycle")
    parser.add_argument("--report", action="store_true", help="Generate hourly report")
    parser.add_argument("--ping", action="store_true", help="Send 15-min status ping")
    args = parser.parse_args()

    if args.migrate:
        simulator.migrate()
    elif args.run:
        # Acquire exclusive file lock to prevent concurrent cycles (P0-001 Fix 1).
        # When cron fires every minute but a cycle takes >1 min, overlapping
        # processes stack and cascade. flock(LOCK_EX | LOCK_NB) makes the second
        # invocation fail immediately instead of waiting.
        lock_path = Path(__file__).parent / ".rivalclaw-run.lock"
        lock_fd = open(lock_path, "w")
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            print("[rivalclaw] Another run_loop is already running — exiting.")
            lock_fd.close()
            sys.exit(0)
        try:
            simulator.run_loop()
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            lock_fd.close()
    elif args.tune:
        import self_tuner
        self_tuner.run_tuning()
        import hourly_report
        hourly_report.generate()
        import auto_changelog
        auto_changelog.append_hourly_entry()
        import notify
        notify.send_hourly_report()
    elif args.report:
        import hourly_report
        hourly_report.generate()
    elif args.ping:
        import status_ping
        status_ping.ping()
    else:
        parser.print_help()
