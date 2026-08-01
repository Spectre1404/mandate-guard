"""ULID-shaped identifiers matching what the real sandbox returned.

Observed in the Jul 31 spike: `ses_01KYWNEA6FF5HXWRN7N3KBBBHV`,
`ord_01KYWNEA6FF5HXWRN7N3KBBBHW`, `txn_01KYWNHG5R0MHJVXC99YEDZB09`,
`tli_01KYWNHG5R0MHJVXC99YEDZB09` — a prefix plus a 26-character Crockford
base32 ULID. (The docs show `sess_`; the live API does not. We match the API.)

Ordering matters: the executor picks the newest transaction by sorting `txn_id`
lexicographically, so these must be monotonically increasing even when two are
minted inside the same millisecond. A counter in the high random bits guarantees
that, which a naive random ULID would not.
"""

import os
import time
from threading import Lock

CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_lock = Lock()
_last_ms = 0
_counter = 0


def _encode(value, length=26):
    chars = []
    for _ in range(length):
        value, remainder = divmod(value, 32)
        chars.append(CROCKFORD[remainder])
    return "".join(reversed(chars))


def ulid():
    """A 26-char Crockford base32 ULID, strictly increasing across calls."""
    global _last_ms, _counter
    with _lock:
        now_ms = int(time.time() * 1000)
        if now_ms == _last_ms:
            _counter += 1
        else:
            _last_ms, _counter = now_ms, 0
        counter = _counter
    randomness = int.from_bytes(os.urandom(8), "big")
    return _encode((now_ms << 80) | ((counter & 0xFFFF) << 64) | randomness)


def new_id(prefix):
    return f"{prefix}_{ulid()}"
