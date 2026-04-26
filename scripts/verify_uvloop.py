#!/usr/bin/env python3
"""Verify that the active Python environment can import uvloop."""

from __future__ import annotations

import uvloop

print(f"uvloop import ok: {uvloop.__version__}")
