"""Hedge trade job persistence service."""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from typing import Any

from backend.services.hedge_status import classify_hedge_status

logger = logging.getLogger(__name__)

HEDGE_JOBS_FILE = os.path.join(
    os.path.dirname(__file__), '..', '..', 'hedge_jobs.json',
)


class HedgeJobStore:
    """Stores hedge trade jobs in a local JSON file."""

    def __init__(self) -> None:
        self._items: list[dict[str, Any]] = []
        self._load()

    def _load(self) -> None:
        try:
            if not os.path.exists(HEDGE_JOBS_FILE):
                self._items = []
                return
            with open(HEDGE_JOBS_FILE, 'r', encoding='utf-8') as file:
                data = json.load(file)
            raw_items = data.get('items', [])
            if not isinstance(raw_items, list):
                raw_items = []
            loaded_items = [item for item in raw_items if isinstance(item, dict)]
            changed = False
            normalized: list[dict[str, Any]] = []
            for item in loaded_items:
                normalized_item, updated = self._normalize_hedge_entry_status(item)
                normalized.append(normalized_item)
                changed = changed or updated
            self._items = normalized
            logger.info('Loaded %d hedge jobs', len(self._items))
            if changed:
                logger.info('Reclassified hedge statuses using current tolerances')
                self._save()
        except Exception as exc:
            logger.error('Failed to load hedge jobs: %s', exc)
            self._items = []

    def _save(self) -> None:
        try:
            payload = {'items': self._items}
            with open(HEDGE_JOBS_FILE, 'w', encoding='utf-8') as file:
                json.dump(payload, file, ensure_ascii=False, indent=2)
        except Exception as exc:
            logger.error('Failed to save hedge jobs: %s', exc)

    def list_jobs(
        self,
        limit: int = 100,
        ticker: str | None = None,
    ) -> list[dict[str, Any]]:
        if limit <= 0:
            return []
        items = self._items
        if ticker:
            target = ticker.strip().upper()
            items = [item for item in items if str(item.get('ticker', '')).upper() == target]
        sorted_items = sorted(
            items,
            key=lambda item: int(item.get('created_at', 0)),
            reverse=True,
        )
        return [dict(item) for item in sorted_items[:limit]]

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        for item in self._items:
            if item.get('job_id') == job_id:
                return dict(item)
        return None

    def create_job(self, payload: dict[str, Any]) -> dict[str, Any]:
        now = int(time.time())
        raw_job = {
            'job_id': str(uuid.uuid4()),
            'created_at': now,
            'updated_at': now,
            **payload,
        }
        job, _ = self._normalize_hedge_entry_status(raw_job)
        self._items.append(job)
        self._save()
        return dict(job)

    def update_job(self, job_id: str, updates: dict[str, Any]) -> dict[str, Any] | None:
        for idx, item in enumerate(self._items):
            if item.get('job_id') != job_id:
                continue
            merged = {
                **item,
                **updates,
                'updated_at': int(time.time()),
            }
            merged, _ = self._normalize_hedge_entry_status(merged)
            self._items[idx] = merged
            self._save()
            return dict(merged)
        return None

    @staticmethod
    def _normalize_hedge_entry_status(
        item: dict[str, Any],
    ) -> tuple[dict[str, Any], bool]:
        status = str(item.get('status', '')).strip().lower()
        if status not in {'hedged', 'partial_hedged'}:
            return dict(item), False

        spot_qty = float(item.get('entry_qty_spot', 0) or 0)
        futures_qty = float(item.get('entry_qty_futures', 0) or 0)
        if spot_qty <= 0 or futures_qty <= 0:
            return dict(item), False

        eval_result = classify_hedge_status(
            spot_qty=spot_qty,
            futures_qty=futures_qty,
            futures_price_usdt=item.get('entry_avg_futures_usdt'),
        )

        normalized = dict(item)
        normalized['status'] = eval_result['status']
        normalized['residual_qty'] = eval_result['residual_qty']
        normalized['residual_ratio'] = eval_result['residual_ratio']
        normalized['residual_notional_usd'] = eval_result['residual_notional_usd']
        normalized['hedge_ratio_tolerance'] = eval_result['hedge_ratio_tolerance']
        normalized['hedge_notional_tolerance_usd'] = eval_result[
            'hedge_notional_tolerance_usd'
        ]

        changed = normalized != item
        return normalized, changed

    def latest_active_job(self, ticker: str) -> dict[str, Any] | None:
        return self.latest_open_job(ticker)

    def open_jobs(self, ticker: str) -> list[dict[str, Any]]:
        target = ticker.strip().upper()
        candidates = [
            item
            for item in self._items
            if str(item.get('ticker', '')).upper() == target
            and str(item.get('status', '')).lower() in {'hedged', 'partial_hedged'}
            and float(item.get('entry_qty_spot', 0) or 0) > 0
            and float(item.get('entry_qty_futures', 0) or 0) > 0
        ]
        candidates.sort(key=lambda item: int(item.get('created_at', 0)), reverse=True)
        return [dict(item) for item in candidates]

    def latest_closed_job(self, ticker: str) -> dict[str, Any] | None:
        target = ticker.strip().upper()
        candidates = [
            item
            for item in self._items
            if str(item.get('ticker', '')).upper() == target
            and str(item.get('status', '')).lower() == 'closed'
            and float(item.get('entry_qty_spot', 0) or 0) > 0
            and float(item.get('entry_qty_futures', 0) or 0) > 0
        ]
        if not candidates:
            return None
        candidates.sort(key=lambda item: int(item.get('created_at', 0)), reverse=True)
        return dict(candidates[0])

    def latest_open_job(self, ticker: str) -> dict[str, Any] | None:
        candidates = self.open_jobs(ticker)
        if not candidates:
            return None
        return dict(candidates[0])
