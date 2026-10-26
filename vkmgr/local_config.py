"""Strict and shared local loopback bind configuration.

Change port only by explicitly setting VKCM_PORT before starting Python.
Never silently take over another application's TCP listener.
"""
from __future__ import annotations

import os
import re

DEFAULT_PORT = 8765
PORT_ENV = "VKCM_PORT"


def configured_port(environ=None):
    environ = os.environ if environ is None else environ
    value = environ.get(PORT_ENV, str(DEFAULT_PORT))
    if not isinstance(value, str) or re.fullmatch(r"[0-9]{4,5}", value) is None:
        raise ValueError("VKCM_PORT must be an integer between 1024 and 65535")
    port = int(value)
    if not 1024 <= port <= 65535:
        raise ValueError("VKCM_PORT must be an integer between 1024 and 65535")
    return port


PORT = configured_port()
BASE = "http://127.0.0.1:" + str(PORT)
