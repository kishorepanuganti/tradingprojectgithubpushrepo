# strategies/delta_strangle.py
"""
Delta-Based OTM Strangle Strategy

Selects strikes based on delta values instead of premium prices. Uses Greeks
calculations to identify strikes with specific delta thresholds and manages 
positions using delta-based targets and stop-losses.

Entry:
- Short CE at ~0.2 delta
- Short PE at ~-0.2 delta

Adjustments:
- CE target: 0.1 delta (profit-taking when delta decays)
- CE stop-loss: 0.35 delta (risk management when delta increases)
- PE target: -0.1 delta (profit-taking when delta decays)
- PE stop-loss: -0.35 delta (risk management when delta increases)
"""

import logging
from typing import Optional, Tuple, Dict
try:
    from .base import BaseStrategy
except Exception:
    from basestrategy import BaseStrategy

from greeks_helpers import find_strike_by_delta
from greeks_calculator import calculate_greeks, parse_symbol_info
from order_logger import get_order_logger
from config.config import DELTA_STRANGLE_SETTINGS, PAPER_TRADING_MODE

logger = logging.getLogger(__name__)


class DeltaStrangle(BaseStrategy):
    """
    Delta-Based OTM Strangle Strategy
    
    This strategy uses Greeks calculations to select strikes based on delta
    values rather than premium proximity. It continuously monitors position
    delta and adjusts when targets or stop-losses are hit.
    
    Key Features:
    - Delta-based strike selection (0.2 delta CE, -0.2 delta PE)
    - Real-time delta monitoring via _on_position_tick()
    - Delta-based profit taking and stop-loss (0.1/0.35 for CE, -0.1/-0.35 for PE)
    - Independent leg management
    """
    
    def __init__(self, trade_ctx, live_data, broker=None, qty=0):
        super().__init__(trade_ctx, live_data, broker, qty)
        
        # Get index-specific settings
        index = trade_ctx.get("index", "NIFTY")
        settings = DELTA_STRANGLE_SETTINGS.get(index, DELTA_STRANGLE_SETTINGS["NIFTY"])
        
        # Use passed qty if > 0, else fallback to config
        if self.qty == 0:
            self.qty = settings["qty"]
        
        # Delta thresholds
        self.entry_delta_ce = settings["entry_delta_ce"]
        self.entry_delta_pe = settings["entry_delta_pe"]
        self.target_delta_ce = settings["target_delta_ce"]
        self.target_delta_pe = settings["target_delta_pe"]
        self.stoploss_delta_ce = settings["stoploss_delta_ce"]
        self.stoploss_delta_pe = settings["stoploss_delta_pe"]
        self.delta_tolerance = settings["delta_tolerance"]
        
        # Max adjustments
        self.MAX_ADJUSTMENTS_PER_LEG = settings.get("max_adjustments", 30)
        
        # Position tracking: {symbol: {entry_price, qty, side, leg, entry_delta}}
        self.positions = {}
        
        # Track last selected strikes to prevent repeated entries
        self.last_selected = {"ce": None, "pe": None}
        
        # Adjustment tracking
        self.ce_adjustment_count = 0
        self.pe_adjustment_count = 0
        
        # Realized P&L tracking
        self.realized_pnl = 0.0
        
    def start(self):
        """Initialize strategy."""
        index = self.trade_ctx.get("index", "NIFTY")
        dte = self.trade_ctx.get("dte", 0)
        logger.info(
            f"[DeltaStrangle] Started for {index} | DTE={dte} | "
            f"Entry deltas: CE={self.entry_delta_ce}, PE={self.entry_delta_pe}"
        )
        
    def _on_position_tick(self, symbol: str, tick: dict):
        """
        REAL-TIME callback - called immediately when position symbol receives tick.
        
        Calculates current delta and checks if target/SL thresholds are hit.
        """
        if symbol not in self.positions:
            return
        
        # Calculate current delta for this position
        current_delta = self._calculate_position_delta(symbol, tick)
        if current_delta is None:
            return
        
        # Check if delta has drifted to target or stop-loss
        self._check_delta_adjustment(symbol, current_delta, tick)
    
    def _calculate_position_delta(self, symbol: str, tick: dict) -> Optional[float]:
        """
        Calculate current delta for a position using Greeks calculator.
        
        Args:
            symbol: Position symbol
            tick: Real-time tick data
            
        Returns:
            Current delta value or None if calculation fails
        """
        if not tick or 'ltp' not in tick:
            return None
        
        try:
            current_ltp = float(tick['ltp'])
            if current_ltp <= 0:
                return None
        except (TypeError, ValueError):
            return None
        
        # Parse symbol to get strike and option type
        parsed = parse_symbol_info(symbol)
        if not parsed:
            self.logger.warning(f"[DeltaStrangle] Could not parse symbol: {symbol}")
            return None
        
        strike, option_type = parsed
        
        # Get spot price and DTE from trade context
        spot_price = self.trade_ctx.get("spot_price")
        dte = self.trade_ctx.get("dte", 0)
        
        if not spot_price:
            return None
        
        # Calculate Greeks
        greeks = calculate_greeks(
            option_price=current_ltp,
            spot=spot_price,
            strike=strike,
            days_to_expiry=dte,
            option_type=option_type,
            risk_free_rate=0.065
        )
        
        if greeks is None:
            return None
        
        return greeks['delta']
    
    def _check_delta_adjustment(self, symbol: str, current_delta: float, tick: dict):
        """
        Check if delta has drifted to target or stop-loss levels.
        
        Args:
            symbol: Position symbol
            current_delta: Calculated current delta
            tick: Real-time tick data
        """
        pos_data = self.positions.get(symbol)
        if not pos_data:
            return
        
        leg_type = pos_data.get("leg", "")
        entry_delta = pos_data.get("entry_delta", 0)
        
        # Update position data with current delta
        pos_data["current_delta"] = current_delta
        
        # Determine if adjustment is needed based on leg type
        if leg_type == "CE":
            # CE: Target when delta decays to ≤0.1, SL when increases to ≥0.35
            if current_delta <= self.target_delta_ce:
                logger.info(
                    f"[DeltaStrangle] CE TARGET hit on {symbol}: "
                    f"Delta {entry_delta:.3f} → {current_delta:.3f} (target ≤{self.target_delta_ce})"
                )
                self._handle_adjustment(symbol, leg_type)
            elif current_delta >= self.stoploss_delta_ce:
                logger.warning(
                    f"[DeltaStrangle] CE STOPLOSS hit on {symbol}: "
                    f"Delta {entry_delta:.3f} → {current_delta:.3f} (SL ≥{self.stoploss_delta_ce})"
                )
                self._handle_adjustment(symbol, leg_type)
        
        elif leg_type == "PE":
            # PE: Target when delta decays to ≥-0.1, SL when increases to ≤-0.35
            # Note: PE deltas are negative, so "decay" means moving toward 0
            if current_delta >= self.target_delta_pe:
                logger.info(
                    f"[DeltaStrangle] PE TARGET hit on {symbol}: "
                    f"Delta {entry_delta:.3f} → {current_delta:.3f} (target ≥{self.target_delta_pe})"
                )
                self._handle_adjustment(symbol, leg_type)
            elif current_delta <= self.stoploss_delta_pe:
                logger.warning(
                    f"[DeltaStrangle] PE STOPLOSS hit on {symbol}: "
                    f"Delta {entry_delta:.3f} → {current_delta:.3f} (SL ≤{self.stoploss_delta_pe})"
                )
                self._handle_adjustment(symbol, leg_type)
    
    def on_tick(self):
        """
        Periodic check for new entries or full symbol scan.
        
        Only enters new positions if flat (no existing positions).
        Position monitoring happens via _on_position_tick() callbacks.
        """
        # Only scan for entry if we have no positions
        if self.positions:
            # Positions are being monitored in real-time via callbacks
            # Report current positions
            self.report_positions(self.positions, realized_pnl=self.realized_pnl)
            return
        
        # No positions - scan for entry
        option_symbols = self.trade_ctx.get("option_symbols", [])
        if not option_symbols:
            logger.warning("[DeltaStrangle] No option symbols available")
            return
        
        # DEBUG: Show sample symbols
        logger.info(f"[DeltaStrangle] Total option_symbols: {len(option_symbols)}")
        if option_symbols:
            logger.info(f"[DeltaStrangle] Sample symbols: {option_symbols[:5]}")
        
        spot_price = self.trade_ctx.get("spot_price")
        dte = self.trade_ctx.get("dte", 0)
        
        if not spot_price or dte is None:
            logger.warning("[DeltaStrangle] Missing spot_price or dte in trade_ctx")
            return
        
        # Extract available strikes from option symbols
        available_strikes = self._extract_strikes(option_symbols)
        if not available_strikes:
            logger.warning("[DeltaStrangle] Could not extract strikes from option symbols")
            return
        
        # DEBUG: Show extracted strikes
        logger.info(f"[DeltaStrangle] Extracted {len(available_strikes)} strikes: {available_strikes[:10]}")
        
        # Adjust tolerance for 0DTE (options have more extreme deltas on expiry day)
        tolerance = self.delta_tolerance
        if dte == 0:
            tolerance = 0.10  # Wider tolerance for 0DTE
            logger.info(f"[DeltaStrangle] 0DTE detected, using wider tolerance: {tolerance}")
        
        # Find CE strike with target delta
        ce_result = find_strike_by_delta(
            target_delta=self.entry_delta_ce,
            option_type='CE',
            spot_price=spot_price,
            available_strikes=available_strikes,
            days_to_expiry=dte,
            live_data=self.live_data,
            tolerance=tolerance,
            available_symbols=option_symbols
        )
        
        # Find PE strike with target delta
        pe_result = find_strike_by_delta(
            target_delta=self.entry_delta_pe,
            option_type='PE',
            spot_price=spot_price,
            available_strikes=available_strikes,
            days_to_expiry=dte,
            live_data=self.live_data,
            tolerance=tolerance,
            available_symbols=option_symbols
        )
        
        if not ce_result or not pe_result:
            logger.debug("[DeltaStrangle] Could not find both CE and PE strikes with target deltas")
            return
        
        ce_strike, ce_delta, ce_greeks = ce_result
        pe_strike, pe_delta, pe_greeks = pe_result
        
        # Build symbol names from strikes
        ce_symbol = self._find_symbol_by_strike(option_symbols, ce_strike, 'CE')
        pe_symbol = self._find_symbol_by_strike(option_symbols, pe_strike, 'PE')
        
        if not ce_symbol or not pe_symbol:
            logger.warning("[DeltaStrangle] Could not build symbols from selected strikes")
            return
        
        # Get LTP from greeks or live data
        ce_ltp = ce_greeks.get('theo_price', 0) or self._get_ltp(ce_symbol)
        pe_ltp = pe_greeks.get('theo_price', 0) or self._get_ltp(pe_symbol)
        
        if ce_ltp <= 0 or pe_ltp <= 0:
            logger.warning("[DeltaStrangle] Invalid LTP for selected strikes")
            return
        
        # Check if selection changed
        if ce_symbol == self.last_selected["ce"] and pe_symbol == self.last_selected["pe"]:
            return  # Same strikes, don't re-enter
        
        # Place initial strangle orders
        logger.info(
            f"[DeltaStrangle] ENTRY: CE={ce_symbol} (delta={ce_delta:.3f}, LTP={ce_ltp:.2f}) | "
            f"PE={pe_symbol} (delta={pe_delta:.3f}, LTP={pe_ltp:.2f})"
        )
        
        self.last_selected["ce"] = ce_symbol
        self.last_selected["pe"] = pe_symbol
        
        self._place_strangle_orders(
            ce_symbol, ce_ltp, ce_delta,
            pe_symbol, pe_ltp, pe_delta
        )
        
        # Report positions
        self.report_positions(self.positions, realized_pnl=self.realized_pnl)
    
    def stop(self, reason: str):
        """Close all positions."""
        logger.info(f"[DeltaStrangle] Stopping: {reason}")
        if self.positions:
             logger.info(f"[DeltaStrangle] Squaring off {len(self.positions)} positions due to stop.")
             for symbol in list(self.positions.keys()):
                 self._square_off_leg(symbol)
    
    def _place_strangle_orders(
        self,
        ce_symbol: str, ce_ltp: float, ce_delta: float,
        pe_symbol: str, pe_ltp: float, pe_delta: float
    ):
        """
        Place short strangle orders for both CE and PE.
        
        Args:
            ce_symbol: CE option symbol
            ce_ltp: CE current LTP
            ce_delta: CE calculated delta
            pe_symbol: PE option symbol
            pe_ltp: PE current LTP
            pe_delta: PE calculated delta
        """
        order_logger = get_order_logger("delta_strangle_orders.csv") if PAPER_TRADING_MODE else None
        
        # Place CE order
        if PAPER_TRADING_MODE:
            order_logger.log_order(
                symbol=ce_symbol,
                qty=self.qty,
                side=-1,
                order_type=2,
                entry_price=ce_ltp,
                order_tag="DELTA_CE_SHORT",
                status="PLACED"
            )
            self.positions[ce_symbol] = {
                "entry_price": ce_ltp,
                "qty": self.qty,
                "side": -1,
                "leg": "CE",
                "entry_delta": ce_delta,
                "current_delta": ce_delta
            }
            self._add_position_symbol(ce_symbol)
            logger.info(f"[PAPER] Logged CE short: {ce_symbol} @ {ce_ltp} (delta={ce_delta:.3f})")
        else:
            ce_order = {
                "symbol": ce_symbol,
                "qty": self.qty,
                "side": -1,
                "type": 2,
                "productType": "INTRADAY",
                "limitPrice": 0,
                "stopPrice": 0,
                "validity": "DAY",
                "disclosedQty": 0,
                "offlineOrder": False,
                "orderTag": "DELTA_CE_SHORT",
                "isSliceOrder": False,
            }
            resp = self.place_order_safe(ce_order)
            if resp.get("code") == 0:
                self.positions[ce_symbol] = {
                    "entry_price": ce_ltp,
                    "qty": self.qty,
                    "side": -1,
                    "leg": "CE",
                    "entry_delta": ce_delta,
                    "current_delta": ce_delta
                }
                self._add_position_symbol(ce_symbol)
                logger.info(f"[DeltaStrangle] Placed CE short: {ce_symbol} @ {ce_ltp} (delta={ce_delta:.3f})")
        
        # Place PE order
        if PAPER_TRADING_MODE:
            order_logger.log_order(
                symbol=pe_symbol,
                qty=self.qty,
                side=-1,
                order_type=2,
                entry_price=pe_ltp,
                order_tag="DELTA_PE_SHORT",
                status="PLACED"
            )
            self.positions[pe_symbol] = {
                "entry_price": pe_ltp,
                "qty": self.qty,
                "side": -1,
                "leg": "PE",
                "entry_delta": pe_delta,
                "current_delta": pe_delta
            }
            self._add_position_symbol(pe_symbol)
            logger.info(f"[PAPER] Logged PE short: {pe_symbol} @ {pe_ltp} (delta={pe_delta:.3f})")
        else:
            pe_order = {
                "symbol": pe_symbol,
                "qty": self.qty,
                "side": -1,
                "type": 2,
                "productType": "INTRADAY",
                "limitPrice": 0,
                "stopPrice": 0,
                "validity": "DAY",
                "disclosedQty": 0,
                "offlineOrder": False,
                "orderTag": "DELTA_PE_SHORT",
                "isSliceOrder": False,
            }
            resp = self.place_order_safe(pe_order)
            if resp.get("code") == 0:
                self.positions[pe_symbol] = {
                    "entry_price": pe_ltp,
                    "qty": self.qty,
                    "side": -1,
                    "leg": "PE",
                    "entry_delta": pe_delta,
                    "current_delta": pe_delta
                }
                self._add_position_symbol(pe_symbol)
                logger.info(f"[DeltaStrangle] Placed PE short: {pe_symbol} @ {pe_ltp} (delta={pe_delta:.3f})")
    
    def _handle_adjustment(self, current_symbol: str, leg_type: str):
        """
        Handle delta-based adjustment: square off and re-enter at new delta strike.
        
        Args:
            current_symbol: Symbol to square off
            leg_type: "CE" or "PE"
        """
        # Check max adjustments
        if leg_type == "CE":
            if self.ce_adjustment_count >= self.MAX_ADJUSTMENTS_PER_LEG:
                logger.warning(
                    f"[DeltaStrangle] Max CE adjustments ({self.MAX_ADJUSTMENTS_PER_LEG}) reached"
                )
                return
        else:
            if self.pe_adjustment_count >= self.MAX_ADJUSTMENTS_PER_LEG:
                logger.warning(
                    f"[DeltaStrangle] Max PE adjustments ({self.MAX_ADJUSTMENTS_PER_LEG}) reached"
                )
                return
        
        # Square off current position
        self._square_off_leg(current_symbol)
        
        # Find new strike with entry delta
        option_symbols = self.trade_ctx.get("option_symbols", [])
        spot_price = self.trade_ctx.get("spot_price")
        dte = self.trade_ctx.get("dte", 0)
        available_strikes = self._extract_strikes(option_symbols)
        
        if not spot_price or not available_strikes:
            logger.warning("[DeltaStrangle] Cannot find new strike for re-entry")
            return
        
        target_delta = self.entry_delta_ce if leg_type == "CE" else self.entry_delta_pe
        
        result = find_strike_by_delta(
            target_delta=target_delta,
            option_type=leg_type,
            spot_price=spot_price,
            available_strikes=available_strikes,
            days_to_expiry=dte,
            live_data=self.live_data,
            tolerance=self.delta_tolerance,
            available_symbols=option_symbols
        )
        
        if not result:
            logger.warning(f"[DeltaStrangle] Could not find new {leg_type} strike for re-entry")
            return
        
        strike, delta, greeks = result
        symbol = self._find_symbol_by_strike(option_symbols, strike, leg_type)
        
        if not symbol:
            logger.warning(f"[DeltaStrangle] Could not build symbol for re-entry strike {strike}")
            return
        
        ltp = greeks.get('theo_price', 0) or self._get_ltp(symbol)
        if ltp <= 0:
            logger.warning("[DeltaStrangle] Invalid LTP for re-entry")
            return
        
        # Re-enter position
        self._reenter_leg(symbol, ltp, delta, leg_type)
        
        # Increment adjustment count
        if leg_type == "CE":
            self.ce_adjustment_count += 1
            logger.info(f"[DeltaStrangle] CE adjustments: {self.ce_adjustment_count}/{self.MAX_ADJUSTMENTS_PER_LEG}")
        else:
            self.pe_adjustment_count += 1
            logger.info(f"[DeltaStrangle] PE adjustments: {self.pe_adjustment_count}/{self.MAX_ADJUSTMENTS_PER_LEG}")
    
    def _square_off_leg(self, symbol: str):
        """Square off a position."""
        if symbol not in self.positions:
            return
        
        pos_data = self.positions[symbol]
        qty = pos_data["qty"]
        entry_price = pos_data["entry_price"]
        
        order_logger = get_order_logger("delta_strangle_orders.csv") if PAPER_TRADING_MODE else None
        
        if PAPER_TRADING_MODE:
            tick = self.live_data.get(symbol)
            current_ltp = float(tick["ltp"]) if tick and "ltp" in tick else 0
            pnl = (entry_price - current_ltp) * qty
            
            order_logger.log_order(
                symbol=symbol,
                qty=qty,
                side=1,
                order_type=2,
                entry_price=current_ltp,
                pnl=pnl,
                order_tag="DELTA_SQUAREOFF",
                status="PLACED"
            )
            
            self._remove_position_symbol(symbol)
            del self.positions[symbol]
            self.realized_pnl += pnl
            logger.info(f"[PAPER] Squared off {symbol} | P&L: {pnl:.2f}")
        else:
            square_off_order = {
                "symbol": symbol,
                "qty": qty,
                "side": 1,
                "type": 2,
                "productType": "INTRADAY",
                "limitPrice": 0,
                "stopPrice": 0,
                "validity": "DAY",
                "disclosedQty": 0,
                "offlineOrder": False,
                "orderTag": "DELTA_SQUAREOFF",
                "isSliceOrder": False,
            }
            resp = self.place_order_safe(square_off_order)
            if resp.get("code") == 0:
                tick = self.live_data.get(symbol)
                current_ltp = float(tick["ltp"]) if tick and "ltp" in tick else entry_price
                pnl = (entry_price - current_ltp) * qty
                
                self._remove_position_symbol(symbol)
                del self.positions[symbol]
                self.realized_pnl += pnl
                logger.info(f"[DeltaStrangle] Squared off {symbol} | P&L: {pnl:.2f}")
    
    def _reenter_leg(self, symbol: str, ltp: float, delta: float, leg_type: str):
        """Re-enter a short position on a new strike."""
        order_logger = get_order_logger("delta_strangle_orders.csv") if PAPER_TRADING_MODE else None
        
        if PAPER_TRADING_MODE:
            order_logger.log_order(
                symbol=symbol,
                qty=self.qty,
                side=-1,
                order_type=2,
                entry_price=ltp,
                order_tag=f"DELTA_REENTER_{leg_type}",
                status="PLACED"
            )
            self.positions[symbol] = {
                "entry_price": ltp,
                "qty": self.qty,
                "side": -1,
                "leg": leg_type,
                "entry_delta": delta,
                "current_delta": delta
            }
            self._add_position_symbol(symbol)
            logger.info(f"[PAPER] Re-entered {leg_type}: {symbol} @ {ltp} (delta={delta:.3f})")
            self.report_positions(self.positions, realized_pnl=self.realized_pnl)
        else:
            reentry_order = {
                "symbol": symbol,
                "qty": self.qty,
                "side": -1,
                "type": 2,
                "productType": "INTRADAY",
                "limitPrice": 0,
                "stopPrice": 0,
                "validity": "DAY",
                "disclosedQty": 0,
                "offlineOrder": False,
                "orderTag": f"DELTA_REENTER_{leg_type}",
                "isSliceOrder": False,
            }
            resp = self.place_order_safe(reentry_order)
            if resp.get("code") == 0:
                self.positions[symbol] = {
                    "entry_price": ltp,
                    "qty": self.qty,
                    "side": -1,
                    "leg": leg_type,
                    "entry_delta": delta,
                    "current_delta": delta
                }
                self._add_position_symbol(symbol)
                logger.info(f"[DeltaStrangle] Re-entered {leg_type}: {symbol} @ {ltp} (delta={delta:.3f})")
    
    # Helper methods
    
    def _extract_strikes(self, option_symbols: list) -> list:
        """Extract unique strike prices from option symbols."""
        strikes = set()
        for symbol in option_symbols:
            parsed = parse_symbol_info(symbol)
            if parsed:
                strike, _ = parsed
                strikes.add(strike)
        return sorted(list(strikes))
    
    def _find_symbol_by_strike(self, option_symbols: list, strike: int, option_type: str) -> Optional[str]:
        """Find symbol matching strike and option type."""
        for symbol in option_symbols:
            parsed = parse_symbol_info(symbol)
            if parsed:
                sym_strike, sym_type = parsed
                if sym_strike == strike and sym_type.upper() == option_type.upper():
                    return symbol
        return None
    
    def _get_ltp(self, symbol: str) -> float:
        """Get LTP from live data."""
        tick = self.live_data.get(symbol) if hasattr(self.live_data, 'get') else None
        if tick and isinstance(tick, dict) and 'ltp' in tick:
            try:
                return float(tick['ltp'])
            except (TypeError, ValueError):
                pass
        return 0.0
