#!/usr/bin/env python3
"""CLI entry point for the resumable raw telemetry exporter (schema v2).

Legacy raw directories are preserved. Use --raw-subdir raw-v2 for a
separate export generation; downstream transformation migration is a
separate repair stage. See docs/TELEMETRY_EXPORT.md.
"""
from telemetry_export import main

if __name__ == "__main__":
    raise SystemExit(main())
