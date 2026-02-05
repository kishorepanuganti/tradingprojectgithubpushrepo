# strategies/base.py
"""Minimal BaseStrategy with only order-placement functionality.

This file intentionally provides the abstract lifecycle methods and a
single, safe order placement helper used by concrete strategies. All
other helpers and convenience properties have been removed per request.
"""
from __future__ import annotations

import abc
import logging
from typing import Any, Dict, Optional

from orderplacement import place_order as _place_order


class BaseStrategy(abc.ABC):
    def __init__(self, trade_ctx: Dict[str, Any], live_data: Any, broker: Optional[Any] = None, qty: int = 0):
        self.trade_ctx = trade_ctx or {}
        self.live_data = live_data
        self.broker = broker
        # If qty is provided, use it; otherwise, subclass must define their own default or handle it
        self.qty = qty 
        self.logger = logging.getLogger(f"strategies.{self.__class__.__name__}")
        # simple order tracking: order_id -> meta
        self._orders: Dict[Any, Dict[str, Any]] = {}
        
        # Initialize positions tracker reference (lazy loaded)
        self._positions_tracker = None
        
        # Position symbol tracking for optimization (real-time monitoring)
        self._position_symbols = set()  # Symbols with active positions
        self._scan_needed = True  # Flag to control full symbol scanning
        import threading
        self._callback_lock = threading.Lock()

    @abc.abstractmethod
    def start(self) -> None:
        """Called once when strategy is launched."""

    @abc.abstractmethod
    def on_tick(self) -> None:
        """Called on market/monitor ticks."""

    @abc.abstractmethod
    def stop(self, reason: str) -> None:
        """Stop the strategy and cleanup positions if required."""
    
    @abc.abstractmethod
    def _on_position_tick(self, symbol: str, tick: dict) -> None:
        """
        Called immediately when tick arrives for a position symbol.
        Subclasses must implement this for real-time position monitoring.
        
        This enables instant target/SL checks on every tick (no 5-second delay).
        
        Args:
            symbol: Symbol that received tick update
            tick: Tick data dict with 'ltp', etc.
        """
        pass

    def place_order_safe(self, order_payload: Dict[str, Any]) -> Dict[str, Any]:
        """Place an order via the shared `orderplacement.place_order` helper.

        The method logs exceptions and always returns a dict response. If an
        order id is present in the broker response it will be stored in
        `self._orders` for basic tracking.
        """
        try:
            resp = _place_order(order_payload)
            order_id = resp.get("id") or resp.get("order_id") or resp.get("orderId")
            if order_id:
                self._orders[order_id] = {"payload": order_payload, "response": resp}
            self.logger.info("place_order_safe: %s -> code=%s", order_payload.get("symbol"), resp.get("code"))
            return resp
        except Exception as e:
            self.logger.exception("place_order_safe: exception placing order: %s", e)
            return {"code": -1, "message": str(e)}

    def track_order(self, order_id: Any, meta: Dict[str, Any]) -> None:
        """Attach metadata to a previously tracked order id."""
        if order_id is None:
            return
        self._orders.setdefault(order_id, {}).update(meta)
    
    def report_positions(self, positions: Dict[str, Dict[str, Any]], metadata: Optional[Dict[str, Any]] = None, realized_pnl: Optional[float] = None) -> None:
        """
        Report current positions to the global positions tracker.
        
        Args:
            positions: Dict of {symbol: {entry_price, qty, side, leg}}
            metadata: Optional dict with strategy-specific metadata (e.g., adjustment_count, index_movement_pct)
            realized_pnl: Optional float representing the total realized P&L for this strategy
        """
        try:
            # Lazy import to avoid circular dependency
            if self._positions_tracker is None:
                from positions_tracker import get_positions_tracker
                self._positions_tracker = get_positions_tracker(self.live_data)
            
            strategy_name = self.__class__.__name__
            self._positions_tracker.update_positions(strategy_name, positions, metadata, realized_pnl)
        except Exception as e:
            self.logger.debug(f"Failed to report positions: {e}")
    
    # -------------------------------------------------------------------------
    # Position Symbol Tracking - for performance optimization
    # -------------------------------------------------------------------------
    
    def _add_position_symbol(self, symbol: str) -> None:
        """Track a new position symbol and register for real-time callbacks."""
        with self._callback_lock:
            self._position_symbols.add(symbol)
            # Register callback for immediate tick updates
            if hasattr(self.live_data, 'register_position_callback'):
                self.live_data.register_position_callback(symbol, self._on_position_tick)
                self.logger.debug(f"Monitoring position symbol in real-time: {symbol}")
    
    def _remove_position_symbol(self, symbol: str) -> None:
        """Remove a position symbol and unregister callback."""
        with self._callback_lock:
            self._position_symbols.discard(symbol)
            # Unregister callback
            if hasattr(self.live_data, 'unregister_position_callback'):
                self.live_data.unregister_position_callback(symbol, self._on_position_tick)
                self.logger.debug(f"Stopped monitoring position symbol: {symbol}")
    
    def _require_scan(self) -> None:
        """Signal that a full symbol scan is needed (for entry/adjustment)."""
        self._scan_needed = True
    
    def _scan_complete(self) -> None:
        """Signal that scan is complete and position monitoring can resume."""
        self._scan_needed = False


