"""Acme Freight's test mail catcher.

Every email the demo site sends lands here instead of leaving the network --
the job MailHog or Mailpit does in a real test environment, and on an air gap
the only kind of mail there is. The Mailbox page (/demo/freight/mailbox/)
shows it, so a test can do what a person does: ask for a password reset,
wait for the email, open it, follow the link.

Mail is not instant, here as anywhere: each message is delivered a few
seconds after it is sent (a queue, a relay), so tests have to WAIT for it --
the part of email testing that fixed sleeps get wrong.

Kept in memory (the newest 200): a restart empties the mailbox.
"""
import itertools
import random
import threading
import time
from collections import deque, namedtuple

from freight import jobs

SENDER = "Acme Freight <no-reply@acme-freight.example>"
KEEP = 200
DELAY_S = (2.0, 6.0)

Message = namedtuple("Message", "id to subject text sent_at deliver_at")

_lock = threading.Lock()
_box = deque(maxlen=KEEP)
_ids = itertools.count(1)


def send(to, subject, text, delay_s=None):
    """Queue a message; it shows up in the mailbox after the delay."""
    if delay_s is None:
        delay_s = random.uniform(*DELAY_S)
    now = time.time()
    with _lock:
        msg = Message(next(_ids), to.lower(), subject, text, now,
                      now + delay_s * jobs.TIME_SCALE)
        _box.append(msg)
    return msg


def delivered(to=None, now=None):
    """Delivered messages, newest first; `to` filters by recipient."""
    now = time.time() if now is None else now
    to = (to or "").strip().lower()
    with _lock:
        msgs = [m for m in _box if m.deliver_at <= now and (not to or m.to == to)]
    return sorted(msgs, key=lambda m: m.id, reverse=True)


def get(msg_id, now=None):
    for m in delivered(now=now):
        if m.id == msg_id:
            return m
    return None


def clear():
    with _lock:
        _box.clear()
