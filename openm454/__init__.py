"""openm454: driver and CLI for the DreamSourceLab DSCope M454 (USB 2A0E:0080, "v3" protocol).

Quick use:
    from openm454 import Scope
    with Scope() as s:
        cap = s.capture(channels="A1,D0", timebase=500e-6)
"""
__version__ = "0.5.0"
from .errors import DscopeError  # noqa: F401


def __getattr__(name):
    if name == "Scope":
        from .scope import Scope
        return Scope
    if name == "load_capture":
        from .capture import load_capture
        return load_capture
    raise AttributeError(name)
