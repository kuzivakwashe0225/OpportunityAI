"""A brake on guessing, on the endpoints where guessing pays.

The test that prompted this: twelve wrong passwords accepted in 3.7 seconds,
no lockout, no delay, no alert. bcrypt's own cost was the only brake, and that
throttles one attacker on one connection - not a hundred parallel ones. Fine
while the only account was the owner's; not fine the moment strangers can
reach the sign-in page.

Three deliberate choices:

**In-process, not Valkey.** The API runs as one uvicorn process in one
container, so a dict here sees every request the deployment receives. Valkey
is running and would be the right answer the day this runs behind more than
one worker - but reaching for it now would buy a network hop and a failure
mode in exchange for nothing. If you add `--workers`, move this to Valkey;
the interface below is deliberately small enough to swap.

**Two keys per attempt, not one.** The account and the caller's address are
counted separately, because they are different attacks. Hammering one account
is someone who wants *that* account; spraying one password across many
accounts is someone who wants *any* account, and a per-account limit alone
never sees it.

**Failures only.** A correct password clears the count. Someone who mistypes
twice and then gets it right is not an attacker and must not be punished a
minute later, which is what counting all attempts would do.

The lockout is not a fixed wall: it widens with each burst. Five wrong
passwords costs a minute, and someone still going after twenty is waiting an
hour. That shape matters - a legitimate user who has genuinely forgotten
their password hits the one-minute version and reads the message, while a
script grinding through a wordlist is stopped at four attempts a minute
within a few rounds.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class Policy:
    """How much guessing is allowed before the door closes."""

    # Failures tolerated inside `window_seconds` before a lockout starts.
    allowance: int
    window_seconds: float
    # The first lockout. Each further burst doubles it, up to max_lock_seconds.
    base_lock_seconds: float
    max_lock_seconds: float


# One account being hammered. Deliberately tight: a real person who has
# forgotten their password tries three or four times, not six.
ACCOUNT_POLICY = Policy(
    allowance=5, window_seconds=900,
    base_lock_seconds=60, max_lock_seconds=3600,
)

# One address, across every account it touches. Looser, because a household,
# an office or a campus can legitimately share an address - but still far
# below what spraying a password across a user list needs.
ADDRESS_POLICY = Policy(
    allowance=20, window_seconds=900,
    base_lock_seconds=60, max_lock_seconds=3600,
)

# Sending mail to an address someone else owns is its own abuse: it costs the
# deployment its sending reputation and the recipient their patience. Counted
# per recipient, so five messages an hour to one mailbox is the ceiling
# however many people ask for them.
MAIL_RECIPIENT_POLICY = Policy(
    allowance=5, window_seconds=3600,
    base_lock_seconds=300, max_lock_seconds=3600,
)

# The same volume counted per caller instead. Looser on purpose: a university
# lab, an office or a shared mobile gateway is one address to us, and several
# people signing up from it in an afternoon is the product working, not an
# attack. Tight enough that nobody mails a list through us.
MAIL_SENDER_POLICY = Policy(
    allowance=20, window_seconds=3600,
    base_lock_seconds=300, max_lock_seconds=3600,
)

# The client-side issue reporter (POST /issues/client) is deliberately public
# - a JS error can happen before anyone has signed in, and those are worth
# seeing too - which makes it the one endpoint here with no account to blame
# a lockout on. Generous enough that a real page genuinely throwing a burst
# of errors during a beta test still gets through and shows up in the admin
# feed; tight enough that it is not a free way to write to this database from
# outside.
ISSUE_REPORT_POLICY = Policy(
    allowance=30, window_seconds=300,
    base_lock_seconds=60, max_lock_seconds=1800,
)

# A dict that only ever grows is a slow memory leak wearing a hat. Entries are
# pruned as they age out; this caps the pathological case where a flood of
# distinct keys arrives faster than they expire.
_MAX_TRACKED_KEYS = 50_000


@dataclass
class _Record:
    failures: list[float] = field(default_factory=list)
    locked_until: float = 0.0
    # How many times this key has been locked. Drives the widening.
    lock_count: int = 0


class Throttle:
    """Counts failures per key and says when a key has had enough.

    Not a decorator and not middleware, on purpose: the caller decides what
    counts as a failure. An endpoint that cannot tell success from failure
    until it has done its work needs to report the outcome afterwards, which
    is what `record_failure` and `clear` are for.
    """

    def __init__(self, policy: Policy, *, now=time.monotonic) -> None:
        self._policy = policy
        self._now = now
        self._records: dict[str, _Record] = {}

    def retry_after(self, key: str) -> int:
        """Seconds the caller must wait, or 0 if they may proceed now."""
        record = self._records.get(key)
        if record is None:
            return 0
        remaining = record.locked_until - self._now()
        return int(remaining) + 1 if remaining > 0 else 0

    def record_failure(self, key: str) -> int:
        """Count one failed attempt. Returns the wait it has just earned."""
        now = self._now()
        self._prune(now)
        record = self._records.setdefault(key, _Record())

        cutoff = now - self._policy.window_seconds
        record.failures = [t for t in record.failures if t > cutoff]
        record.failures.append(now)

        if len(record.failures) >= self._policy.allowance:
            lock = min(
                self._policy.base_lock_seconds * (2 ** record.lock_count),
                self._policy.max_lock_seconds,
            )
            record.locked_until = now + lock
            record.lock_count += 1
            # Start the next burst from nothing, so the count that triggered
            # this lockout cannot immediately trigger the next one too.
            record.failures = []
            return int(lock)
        return 0

    def clear(self, key: str) -> None:
        """Succeeded. Forget everything about this key.

        The widening lock_count goes with it: someone who fumbled their
        password last week and got in should not start today halfway to an
        hour-long lockout.
        """
        self._records.pop(key, None)

    def _prune(self, now: float) -> None:
        if len(self._records) < _MAX_TRACKED_KEYS:
            return
        cutoff = now - self._policy.window_seconds
        self._records = {
            key: record
            for key, record in self._records.items()
            if record.locked_until > now or any(t > cutoff for t in record.failures)
        }


def client_address(request) -> str:
    """The caller's address, as far as it can be trusted.

    Behind Caddy every request arrives from the loopback, so the real address
    is only in X-Forwarded-For. That header is forgeable by anyone talking to
    the app directly - which is precisely why port 8000 is bound to the
    loopback in docker-compose.yml and the only way in is through the proxy.
    Those two facts hold each other up; changing either one alone breaks this.

    The leftmost entry is the original client. Caddy appends, so a forged
    header from outside would still have the real address appended after it -
    but since outside cannot reach the app at all, the leftmost is honest.
    """
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    client = getattr(request, "client", None)
    return getattr(client, "host", None) or "unknown"


# The instances the API uses. Module-level so the count survives between
# requests, and importable so tests can reset them.
sign_in_by_account = Throttle(ACCOUNT_POLICY)
sign_in_by_address = Throttle(ADDRESS_POLICY)
mail_by_recipient = Throttle(MAIL_RECIPIENT_POLICY)
mail_by_sender = Throttle(MAIL_SENDER_POLICY)
issue_report_by_address = Throttle(ISSUE_REPORT_POLICY)

_ALL = (sign_in_by_account, sign_in_by_address, mail_by_recipient, mail_by_sender,
        issue_report_by_address)


def reset_all() -> None:
    """Forget every count.

    Used by the test suite between tests - without it the suite's own 287
    registrations would trip the mail limit and every test after that would
    fail for the wrong reason - and available to an operator who has locked
    themselves out and can reach the process.
    """
    for throttle in _ALL:
        throttle._records.clear()
