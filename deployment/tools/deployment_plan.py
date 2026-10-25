#!/usr/bin/env python3
"""Build the project-server names from the selected count. No AWS calls."""
import argparse
import json
from pathlib import Path
import re


def server_names(settings):
    count = settings.get("ec2_instance_count", 1)
    base = settings.get("instance_name")
    if type(count) is not int or not 1 <= count <= 20:
        raise ValueError("Choose 1–20 project servers; the controller is counted separately.")
    if not isinstance(base, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,127}", base):
        raise ValueError("Choose a valid project-server base name.")
    return [base if index == 1 else f"{base}-{index:02d}" for index in range(1, count + 1)]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    try:
        with Path(args.config).open(encoding="utf-8-sig") as stream:
            names = server_names(json.load(stream))
    except (ValueError, OSError):
        parser.exit(2, "Check the settings file, instance_name and ec2_instance_count (1–20).\n")
    print(json.dumps({"server_names": names, "project_server_count": len(names), "controller_count": 1}))
