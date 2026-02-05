"""Example strategy implementations used by the RiskManager and router.

These are minimal, safe implementations that log actions and demonstrate
how to place orders via `orderplacement.place_order`.
"""
import logging
from typing import Any

from .base import BaseStrategy
from orderplacement import place_order

logger = logging.getLogger(__name__)


class PassiveShortStraddle(BaseStrategy):
    def start(self):
        # Sell ATM CE and PE as an example (qty, side: 2=SELL)
        ce = self.trade_ctx.get("ce_symbol")
        pe = self.trade_ctx.get("pe_symbol")
        qty = 1
        for sym in (ce, pe):
            if not sym:
                continue
            data = {
                "symbol": sym,
                "qty": qty,
                "type": 2,
                "side": 2,
                "productType": "INTRADAY",
                "limitPrice": 0,
                "stopPrice": 0,
            }
            resp = place_order(data)
            logger.info("PassiveShortStraddle placed order for %s: %s", sym, resp)

    def on_tick(self):
        # simple placeholder
        return

    def stop(self, reason: str):
        logger.info("PassiveShortStraddle stop: %s", reason)


class ProtectiveHedge(BaseStrategy):
    def start(self):
        # placeholder: implement protective hedges (buy OTM options)
        logger.info("ProtectiveHedge started (no orders placed by default)")

    def on_tick(self):
        return

    def stop(self, reason: str):
        logger.info("ProtectiveHedge stop: %s", reason)


class StructuredDebitSpread(BaseStrategy):
    def start(self):
        logger.info("StructuredDebitSpread started — example only")

    def on_tick(self):
        return

    def stop(self, reason: str):
        logger.info("StructuredDebitSpread stop: %s", reason)


class AggressiveDirectional(BaseStrategy):
    def start(self):
        logger.info("AggressiveDirectional started — example only")

    def on_tick(self):
        return

    def stop(self, reason: str):
        logger.info("AggressiveDirectional stop: %s", reason)


__all__ = [
    "PassiveShortStraddle",
    "ProtectiveHedge",
    "StructuredDebitSpread",
    "AggressiveDirectional",
]
