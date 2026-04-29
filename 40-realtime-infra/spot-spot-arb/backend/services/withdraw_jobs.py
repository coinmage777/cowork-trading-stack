"""Withdrawal job persistence service."""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from typing import Any

logger = logging.getLogger(__name__)

WITHDRAW_JOBS_FILE = os.path.join(
    os.path.dirname(__file__), '..', '..', 'withdraw_jobs.json',
)


class WithdrawJobStore:
    """Stores withdrawal jobs in a local JSON file for on-demand operation."""

    def __init__(self) -> None:
        self._items: list[dict[str, Any]] = []
        self._load()

    def _load(self) -> None:
        try:
            if not os.path.exists(WITHDRAW_JOBS_FILE):
                self._items = []
                return
            with open(WITHDRAW_JOBS_FILE, 'r', encoding='utf-8') as file:
                data = json.load(file)
            raw_items = data.get('items', [])
            if not isinstance(raw_items, list):
                raw_items = []
            self._items = [item for item in raw_items if isinstance(item, dict)]
            logger.info('Loaded %d withdrawal jobs', len(self._items))
        except Exception as exc:
            logger.error('Failed to load withdrawal jobs: %s', exc)
            self._items = []

    def _save(self) -> None:
        try:
            payload = {'items': self._items}
            with open(WITHDRAW_JOBS_FILE, 'w', encoding='utf-8') as file:
                json.dump(payload, file, ensure_ascii=False, indent=2)
        except Exception as exc:
            logger.error('Failed to save withdrawal jobs: %s', exc)

    def list_jobs(self, limit: int = 100) -> list[dict[str, Any]]:
        if limit <= 0:
            return []
        sorted_items = sorted(
            self._items,
            key=lambda item: int(item.get('created_at', 0)),
            reverse=True,
        )
        return sorted_items[:limit]

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        for item in self._items:
            if item.get('job_id') == job_id:
                return dict(item)
        return None

    def find_by_preview_token(self, preview_token: str) -> dict[str, Any] | None:
        for item in self._items:
            if item.get('preview_token') == preview_token:
                return dict(item)
        return None

    def find_jobs(
        self,
        *,
        ticker: str | None = None,
        target_exchange: str | None = None,
        statuses: set[str] | None = None,
        created_after: int | None = None,
    ) -> list[dict[str, Any]]:
        normalized_ticker = str(ticker or '').strip().upper()
        normalized_exchange = str(target_exchange or '').strip().lower()
        normalized_statuses = {
            str(status).strip().lower()
            for status in (statuses or set())
            if str(status).strip()
        }
        min_created_at = int(created_after or 0)

        matches: list[dict[str, Any]] = []
        for item in self._items:
            if normalized_ticker and str(item.get('ticker', '')).strip().upper() != normalized_ticker:
                continue
            if (
                normalized_exchange
                and str(item.get('target_exchange', '')).strip().lower() != normalized_exchange
            ):
                continue
            if normalized_statuses:
                status = str(item.get('status', '')).strip().lower()
                if status not in normalized_statuses:
                    continue
            created_at = int(item.get('created_at', 0) or 0)
            if min_created_at > 0 and created_at < min_created_at:
                continue
            matches.append(dict(item))

        matches.sort(key=lambda item: int(item.get('created_at', 0) or 0))
        return matches

    def create_job(self, payload: dict[str, Any]) -> dict[str, Any]:
        now = int(time.time())
        job = {
            'job_id': str(uuid.uuid4()),
            'created_at': now,
            'updated_at': now,
            **payload,
        }
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
            self._items[idx] = merged
            self._save()
            return dict(merged)
        return None
