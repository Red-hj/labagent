"""Public execution progress for the active web request."""
from contextlib import contextmanager
from contextvars import ContextVar

_sink = ContextVar("labagent_progress", default=None)


@contextmanager
def progress_to(callback):
    token = _sink.set(callback)
    try:
        yield
    finally:
        _sink.reset(token)


def emit_progress(stage, message):
    callback = _sink.get()
    if callback:
        callback({"type": "progress", "stage": stage, "message": message})
