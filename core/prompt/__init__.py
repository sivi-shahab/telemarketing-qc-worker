"""Prompt package — OCR + verification prompt modules per document type.

Each ``ocr_<type>.py`` module defines the strict JSON schema (``PROPS`` /
``REQUIRED`` / ``SCHEMA``) and the base ``PROMPT`` for one document type, plus a
``build_prompt(reference)`` that injects the bank reference ("acuan") values
fetched from the CSVs at call time.
"""
