"""ALLOW_LIVE gate (v3 Hard Rule #2).

Pure functions, no MT5 import: the bot must refuse to start on a
non-demo account unless the owner explicitly set ALLOW_LIVE=true.

Demo detection is by server name (brokers label demo servers with
'demo' or 'trial', e.g. 'Exness-MT5Trial6'). An empty/unknown server
name is treated as NOT demo: when in doubt, refuse.
"""
from __future__ import annotations

from dataclasses import dataclass

DEMO_MARKERS = ("demo", "trial")


def is_demo_server(server) -> bool:
    """True only for a non-empty string server name that looks demo."""
    if not isinstance(server, str):
        return False
    s = server.strip().lower()
    if not s:
        return False
    return any(m in s for m in DEMO_MARKERS)


@dataclass(frozen=True)
class GateDecision:
    allowed: bool
    reason: str


def check_live_start(server, allow_live: bool) -> GateDecision:
    """Decide whether the bot may start against `server`.

    - demo server            -> allowed
    - non-demo + ALLOW_LIVE  -> allowed (owner's explicit choice)
    - non-demo, no flag      -> REFUSED
    - unknown/empty server   -> REFUSED unless ALLOW_LIVE (cannot prove demo)
    """
    if is_demo_server(server):
        return GateDecision(True, "demo server")
    if allow_live:
        return GateDecision(True, "non-demo server but ALLOW_LIVE=true (owner-set)")
    return GateDecision(
        False,
        "refusing to start: server %r does not look like a demo account "
        "and ALLOW_LIVE=true is not set" % (server,),
    )
