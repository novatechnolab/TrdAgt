#!/usr/bin/env python3
"""
Utility script to automatically locate and delete all EOD snapshot JSON files
from the TradeSignal codebase.
"""
import glob
import os
import sys

def main():
    base_dir = os.path.dirname(os.path.abspath(__file__))
    backend_dir = os.path.join(base_dir, "app", "backend")

    patterns = [
        os.path.join(backend_dir, "*snapshot*.json"),
        os.path.join(backend_dir, "*_eod_*.json"),
        os.path.join(base_dir, "*snapshot*.json"),
    ]

    deleted_count = 0
    for pattern in patterns:
        for filepath in glob.glob(pattern):
            try:
                os.remove(filepath)
                print(f"[OK] Deleted snapshot file: {filepath}")
                deleted_count += 1
            except Exception as e:
                print(f"[ERROR] Could not delete {filepath}: {e}")

    if deleted_count == 0:
        print("[INFO] No snapshot files found. System is clean!")

if __name__ == "__main__":
    main()
