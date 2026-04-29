from __future__ import annotations

import unittest
from unittest.mock import patch

from backend import config
from backend.services.hedge_trade_service import HedgeTradeService


class HedgeTradeCloseDetectionTests(unittest.TestCase):
    def test_is_close_complete_accepts_small_usd_residual_buffer(self) -> None:
        with (
            patch.object(config, 'HEDGE_RESIDUAL_RATIO_TOLERANCE', 0.001),
            patch.object(
                config,
                'HEDGE_CLOSE_RESIDUAL_NOTIONAL_USD_TOLERANCE',
                2.0,
                create=True,
            ),
        ):
            is_complete = HedgeTradeService._is_close_complete(
                target_qty=199.1,
                closed_qty=196.15,
                reference_price_usdt=0.2977,
            )

        self.assertTrue(is_complete)

    def test_is_close_complete_rejects_residual_above_usd_buffer(self) -> None:
        with (
            patch.object(config, 'HEDGE_RESIDUAL_RATIO_TOLERANCE', 0.001),
            patch.object(
                config,
                'HEDGE_CLOSE_RESIDUAL_NOTIONAL_USD_TOLERANCE',
                2.0,
                create=True,
            ),
        ):
            is_complete = HedgeTradeService._is_close_complete(
                target_qty=199.1,
                closed_qty=190.0,
                reference_price_usdt=0.2977,
            )

        self.assertFalse(is_complete)


if __name__ == '__main__':
    unittest.main()
