"""Phase -1: perishable-data capture.

Capture and freeze pre-lock snapshots of purchased projections, ownership,
salaries, odds, and weather at fixed times; hash and timestamp every file.
A folder of hashed, timestamped files is sufficient; the database ingests
them retroactively. See design doc section 9.0.
"""
