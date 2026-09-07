"""Master Error Code catalog for the Manual Check "New Error Code" dropdown.

Generated once from ``compliance/error_reasons.json`` (converted from the business
spreadsheet "Error Reason - Telemarketing QC"). Loaded at import so the .xlsx is
never read at runtime. Re-run the conversion and overwrite the JSON to refresh.
"""
import json
import os

_PATH = os.path.join(os.path.dirname(__file__), "error_reasons.json")

try:
    with open(_PATH, encoding="utf-8") as _f:
        ERROR_REASONS = json.load(_f)
except (OSError, ValueError):
    ERROR_REASONS = []
