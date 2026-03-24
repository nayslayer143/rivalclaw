#!/usr/bin/env python3
"""RivalClaw CLI — entry point for cron and manual runs."""
import os
import sys
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
    args = parser.parse_args()

    if args.migrate:
        simulator.migrate()
    elif args.run:
        simulator.run_loop()
    elif args.tune:
        import self_tuner
        self_tuner.run_tuning()
        import hourly_report
        hourly_report.generate()
    elif args.report:
        import hourly_report
        hourly_report.generate()
    else:
        parser.print_help()
