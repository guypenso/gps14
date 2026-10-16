"""Cross-session contention checks; provider traffic remains blocked by conftest."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier

import pytest

from rentalsourcing.storage import BudgetExceeded, Store


def simultaneous(store, operation, count=8):
    if not store.pg:
        pytest.skip('Cross-session PostgreSQL integration test')
    barrier = Barrier(count)

    def worker(_):
        other = Store(store.test_url)
        try:
            barrier.wait(timeout=15)
            return operation(other)
        finally:
            other.close()

    with ThreadPoolExecutor(max_workers=count) as executor:
        return list(executor.map(worker, range(count)))


def test_concurrent_reservations_preserve_approval_and_rollback(store, settings, clock):
    limited = replace(settings, approved_requests=3)

    def reserve(other):
        try:
            other.reserve_api(limited, clock())
            return True
        except BudgetExceeded:
            return False

    results = simultaneous(store, reserve)
    assert sum(results) == 3
    usage = dict(store.execute('SELECT period,used FROM usage').fetchall())
    assert usage == {
        'day:2026-10-16': 3,
        'cycle:2026-10-01': 3,
        'approval:local-test-only': 3,
    }


def test_concurrent_notification_and_schedule_claims_have_one_winner(store):
    assert sum(simultaneous(store, lambda db: db.claim('same-property', {'fixture': True}))) == 1
    assert sum(simultaneous(store, lambda db: db.claim_scheduled_day('2026-10-16'))) == 1
    assert store.claimed('same-property')
    assert store.execute('SELECT count(*) FROM notifications').fetchone()[0] == 1
    assert store.execute('SELECT count(*) FROM scheduled_days').fetchone()[0] == 1
