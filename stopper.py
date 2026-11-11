"""
The STOP button. Every background loop remembers the stop number when it starts; when you press STOP the number
goes up, and every loop that started before that quits at its next check, including while it is waiting between messages.
"""
import time, threading

EPOCH = [0]
_tl = threading.local()

def stop(): EPOCH[0] += 1
def now(): return EPOCH[0]

def mark():
    """Call at the start of a background pass: this thread now belongs to the current stop number."""
    _tl.epoch = EPOCH[0]; return EPOCH[0]

def mine():
    """The stop number this thread started under (or the current one for a fresh request)."""
    return getattr(_tl, "epoch", EPOCH[0])

def stale(e=None):
    """True if STOP was pressed after this work started."""
    return (mine() if e is None else e) != EPOCH[0]

def sleep(seconds, e=None):
    """Sleep, but wake up at once if STOP is pressed. Returns True if stopped."""
    e = mine() if e is None else e
    end = time.time() + seconds
    while time.time() < end:
        if EPOCH[0] != e: return True
        time.sleep(min(1.0, max(0.0, end - time.time())))
    return EPOCH[0] != e
