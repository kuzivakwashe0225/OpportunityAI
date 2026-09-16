"""The brake itself, driven by a clock the test controls.

Tested here rather than through the API because the interesting behaviour is
all about time - a lockout that widens, a window that expires, a count that
clears - and proving any of that through real HTTP would mean a test suite
that sleeps for an hour.
"""

import pytest

from opportunity_agent.throttle import Policy, Throttle, client_address


class Clock:
    """A clock that only moves when the test says so."""

    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


FIVE_IN_A_MINUTE = Policy(
    allowance=5, window_seconds=60,
    base_lock_seconds=60, max_lock_seconds=600,
)


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def throttle(clock):
    return Throttle(FIVE_IN_A_MINUTE, now=clock)


def test_a_key_nobody_has_failed_on_is_free(throttle):
    assert throttle.retry_after("someone") == 0


def test_attempts_below_the_allowance_cost_nothing(throttle):
    for _ in range(4):
        throttle.record_failure("someone")

    assert throttle.retry_after("someone") == 0


def test_the_door_closes_on_the_fifth_failure(throttle):
    for _ in range(5):
        throttle.record_failure("someone")

    assert throttle.retry_after("someone") > 0


def test_the_lock_lifts_when_its_time_is_up(throttle, clock):
    for _ in range(5):
        throttle.record_failure("someone")
    assert throttle.retry_after("someone") > 0

    clock.advance(61)

    assert throttle.retry_after("someone") == 0


def test_each_burst_costs_more_than_the_last(throttle, clock):
    """The shape that matters: a person who has forgotten their password
    meets the one-minute version and reads the message, while a script
    grinding a wordlist is down to a handful of tries an hour."""
    waits = []
    for _ in range(4):
        for _ in range(5):
            throttle.record_failure("grinder")
        waits.append(throttle.retry_after("grinder"))
        clock.advance(waits[-1] + 1)

    assert waits == sorted(waits), waits
    assert waits[-1] > waits[0]


def test_the_widening_stops_at_the_ceiling(throttle, clock):
    for _ in range(12):
        for _ in range(5):
            throttle.record_failure("grinder")
        clock.advance(throttle.retry_after("grinder") + 1)

    for _ in range(5):
        throttle.record_failure("grinder")

    assert throttle.retry_after("grinder") <= FIVE_IN_A_MINUTE.max_lock_seconds + 1


def test_failures_spread_out_over_time_never_add_up(throttle, clock):
    """Four failures an hour apart is someone who signs in rarely and types
    badly. It must never accumulate into a lockout."""
    for _ in range(20):
        throttle.record_failure("occasional")
        clock.advance(61)

    assert throttle.retry_after("occasional") == 0


def test_succeeding_forgets_everything(throttle):
    for _ in range(4):
        throttle.record_failure("someone")

    throttle.clear("someone")
    for _ in range(4):
        throttle.record_failure("someone")

    assert throttle.retry_after("someone") == 0


def test_succeeding_also_forgets_how_close_to_an_hour_they_were(throttle, clock):
    """Someone who fumbled last week and got in should not start today
    halfway to the longest lockout."""
    for _ in range(4):
        for _ in range(5):
            throttle.record_failure("returning")
        clock.advance(throttle.retry_after("returning") + 1)
    throttle.clear("returning")

    for _ in range(5):
        throttle.record_failure("returning")

    assert throttle.retry_after("returning") <= FIVE_IN_A_MINUTE.base_lock_seconds + 1


def test_keys_do_not_interfere(throttle):
    for _ in range(5):
        throttle.record_failure("noisy")

    assert throttle.retry_after("noisy") > 0
    assert throttle.retry_after("quiet") == 0


# --------------------------------------------------------------------------
# Working out who is calling
# --------------------------------------------------------------------------

class _Request:
    def __init__(self, headers=None, host=None):
        self.headers = headers or {}
        self.client = type("C", (), {"host": host})() if host else None


def test_the_forwarded_address_is_used_when_there_is_one():
    """Behind Caddy every request arrives from the loopback, so without this
    the whole deployment counts as one caller."""
    request = _Request({"x-forwarded-for": "41.220.16.7"}, host="127.0.0.1")

    assert client_address(request) == "41.220.16.7"


def test_the_original_client_is_taken_from_a_chain_of_proxies():
    request = _Request({"x-forwarded-for": "41.220.16.7, 10.0.0.1, 127.0.0.1"})

    assert client_address(request) == "41.220.16.7"


def test_the_socket_address_is_used_when_nothing_was_forwarded():
    assert client_address(_Request(host="41.220.16.7")) == "41.220.16.7"


def test_an_unidentifiable_caller_still_gets_a_key():
    """They are all counted together under one key, which is the safe
    direction to fail in: stricter, not looser."""
    assert client_address(_Request()) == "unknown"
