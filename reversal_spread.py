# strategies/reversal_spread.py

import logging
try:
    from .base import BaseStrategy
except Exception:
    from basestrategy import BaseStrategy
from strike_utils import select_by_distance, select_by_spread_width

logger = logging.getLogger(__name__)


class ReversalSpread(BaseStrategy):
    """
    Reversal Spread Strategy: Quick directional plays on IV/price reversals
    
    Autonomously selects call or put spreads based on spread width:
    - Short: Near-ATM strike (higher premium collection)
    - Long: Further OTM strike (insurance/defined risk)
    - Target spread width: 20-30 rupees
    
    Profile:
    - Sell near-ATM options, buy further OTM options
    - Limited risk (spread width - premium received)
    - Quick profit taking on mean reversion
    - 60-70% probability of profit
    - Holds for minutes to hours
    """

    def __init__(self, trade_ctx, live_data, broker=None):
        super().__init__(trade_ctx, live_data, broker)
        self.short_strike = None
        self.long_strike = None
        self.position_type = None  # "CALL_SPREAD" or "PUT_SPREAD"
        self.entry_price = None
        self.last_selected = {"short": None, "long": None}
        self.target_spread_width = 25.0  # Adjust per index/preference

    def start(self):
        """Initialize Reversal Spread strategy (autonomous in on_tick)."""
        index = self.trade_ctx.get("index", "NIFTY")
        logger.info(
            f"[ReversalSpread] Starting for {index} | "
            f"ATM={self.trade_ctx['atm_strike']} | DTE={self.trade_ctx.get('dte', 0)}"
        )

    def on_tick(self):
        """Autonomously select call or put spreads based on spread width."""
        option_symbols = self.trade_ctx.get("option_symbols", [])
        if not option_symbols:
            logger.warning("[ReversalSpread] No option symbols available")
            return

        # Detect directional bias (TODO: implement proper bias detection)
        # For now, we'll alternate or use a simple heuristic
        # In production, this would analyze market microstructure, IV skew, etc.
        
        call_symbols = [s for s in option_symbols if s.endswith("CE")]
        put_symbols = [s for s in option_symbols if s.endswith("PE")]

        # Try to select spread with target width
        short_call_sym, long_call_sym, call_width = select_by_spread_width(
            call_symbols,
            call_symbols,  # Both from call symbols for vertical spread
            self.live_data,
            target_spread_width=self.target_spread_width,
            width_tolerance=5
        )

        short_put_sym, long_put_sym, put_width = select_by_spread_width(
            put_symbols,
            put_symbols,
            self.live_data,
            target_spread_width=self.target_spread_width,
            width_tolerance=5
        )

        # Choose the spread with better risk/reward (prefer wider spreads = more premium)
        if call_width and put_width:
            if call_width > put_width:
                short_sym = short_call_sym
                long_sym = long_call_sym
                position_type = "CALL_SPREAD"
                spread_width = call_width
            else:
                short_sym = short_put_sym
                long_sym = long_put_sym
                position_type = "PUT_SPREAD"
                spread_width = put_width
        elif call_width:
            short_sym, long_sym = short_call_sym, long_call_sym
            position_type = "CALL_SPREAD"
            spread_width = call_width
        elif put_width:
            short_sym, long_sym = short_put_sym, long_put_sym
            position_type = "PUT_SPREAD"
            spread_width = put_width
        else:
            logger.debug("[ReversalSpread] Could not select either call or put spread")
            return

        # Check if strikes have changed
        if short_sym != self.last_selected["short"] or long_sym != self.last_selected["long"]:
            logger.info(
                f"[ReversalSpread] {position_type} selected: "
                f"Short={short_sym} | Long={long_sym} | Width={spread_width:.2f}"
            )
            self.last_selected["short"] = short_sym
            self.last_selected["long"] = long_sym
            self.position_type = position_type
            
            # TODO: Place 2-leg spread order
        else:
            logger.debug(
                f"[ReversalSpread] Current {position_type}: "
                f"{short_sym} / {long_sym} (Width={spread_width:.2f})"
            )

        # TODO: Implement:
        # 1. place_spread_order(short_sym, long_sym, qty=1)
        # 2. Monitor P&L on every tick
        # 3. Exit at profit target (30-50% max profit)
        # 4. Strict stop loss at max loss (spread width - premium received)
        # 5. Handle early close for quick scalps

    def stop(self, reason: str):
        """Close Reversal Spread positions."""
        logger.info(f"[ReversalSpread] Stopping: {reason}")
        # TODO: Close both short and long legs via broker
