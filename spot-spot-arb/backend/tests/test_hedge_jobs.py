from __future__ import annotations

import unittest

from backend.services.hedge_jobs import HedgeJobStore


class HedgeJobStoreSelectionTests(unittest.TestCase):
    def _make_store(self, items: list[dict]) -> HedgeJobStore:
        store = HedgeJobStore.__new__(HedgeJobStore)
        store._items = items
        return store

    def test_latest_active_job_prefers_open_job_over_newer_closed_job(self) -> None:
        store = self._make_store(
            [
                {
                    'job_id': 'open-job',
                    'ticker': 'COS',
                    'status': 'hedged',
                    'created_at': 100,
                    'entry_qty_spot': 10,
                    'entry_qty_futures': 10,
                },
                {
                    'job_id': 'closed-job',
                    'ticker': 'COS',
                    'status': 'closed',
                    'created_at': 200,
                    'entry_qty_spot': 5,
                    'entry_qty_futures': 5,
                },
            ]
        )

        latest = store.latest_active_job('COS')

        self.assertIsNotNone(latest)
        self.assertEqual(latest['job_id'], 'open-job')

    def test_latest_active_job_returns_none_when_only_closed_jobs_exist(self) -> None:
        store = self._make_store(
            [
                {
                    'job_id': 'closed-older',
                    'ticker': 'COS',
                    'status': 'closed',
                    'created_at': 100,
                    'entry_qty_spot': 10,
                    'entry_qty_futures': 10,
                },
                {
                    'job_id': 'closed-newer',
                    'ticker': 'COS',
                    'status': 'closed',
                    'created_at': 200,
                    'entry_qty_spot': 12,
                    'entry_qty_futures': 12,
                },
            ]
        )

        latest = store.latest_active_job('COS')

        self.assertIsNone(latest)

    def test_open_jobs_returns_newest_first(self) -> None:
        store = self._make_store(
            [
                {
                    'job_id': 'open-older',
                    'ticker': 'COS',
                    'status': 'hedged',
                    'created_at': 100,
                    'entry_qty_spot': 10,
                    'entry_qty_futures': 10,
                },
                {
                    'job_id': 'open-newer',
                    'ticker': 'COS',
                    'status': 'partial_hedged',
                    'created_at': 200,
                    'entry_qty_spot': 12,
                    'entry_qty_futures': 11,
                },
            ]
        )

        open_jobs = store.open_jobs('COS')

        self.assertEqual([job['job_id'] for job in open_jobs], ['open-newer', 'open-older'])


if __name__ == '__main__':
    unittest.main()
