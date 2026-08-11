#!/usr/bin/env python3
"""
scan_short_covering.py — Quick CLI launcher for F&O Short Covering Scanner

Run anytime:
  python scan_short_covering.py
  python scan_short_covering.py --csv custom_output.csv --min-strikes 1
"""

import os
import sys

# Ensure app/backend is on sys.path
backend_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app", "backend")
if backend_path not in sys.path:
    sys.path.insert(0, backend_path)

from fno_short_covering_scanner import main

if __name__ == "__main__":
    main()
