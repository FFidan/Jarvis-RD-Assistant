#!/usr/bin/env python3
"""Verify that the active Python environment can import uvloop."""

from __future__ import annotations

import asyncio

try:
    import importlib.metadata

    import uvloop

    uvloop.install()
    loop_module = type(asyncio.new_event_loop()).__module__
    try:
        version = importlib.metadata.version("uvloop")
    except importlib.metadata.PackageNotFoundError:
        version = getattr(uvloop, "__version__", "unknown")
    print(f"uvloop {version} installed; current loop module: {loop_module}")
except ImportError as e:
    print(f"uvloop NOT available: {e}")
