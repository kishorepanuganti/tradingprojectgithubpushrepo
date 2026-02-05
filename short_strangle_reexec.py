# strategies/short_strangle_reexec.py

import logging
try:
    from .base import BaseStrategy
except Exception:
    from basestrategy import BaseStrategy
from strike_utils import select_by_premium, find_hedge_strike
from order_logger import get_order_logger
from config.config import SHORT_STRANGLE_SETTINGS, PAPER_TRADING_MODE

logger = logging.getLogger(__name__)


# =====================================================================
# Short Strangle Strategy
# =====================================================================
class ShortStrangleReExec(BaseStrategy):
    """
    Short Strangle Strategy with dynamic re-execution based on LTP proximity.
    Uses strike_utils to autonomously select nearest LTP strikes every tick.
    
    Strategy Flow:
    1. Select CE & PE strikes with LTP nearest to target values:
       - NIFTY: 10 rupees each
       - SENSEX: 50 rupees each
    2. Place short strangle on selected strikes
    3. Continuously monitor and re-execute if LTP drifts
    4. Benefit from time decay on 0DTE options
    
    Profile:
    - Undefined loss potential
    - High time decay advantage
    - Requires active monitoring and re-execution
    - Suitable for high market conviction trades
    """

    def __init__(self, trade_ctx, live_data, broker=None, qty=0):
        super().__init__(trade_ctx, live_data, broker, qty)
        
        # Get index-specific settings from config
        index = trade_ctx.get("index", "NIFTY")
        settings = SHORT_STRANGLE_SETTINGS.get(index, SHORT_STRANGLE_SETTINGS["NIFTY"])
        
        # Use passed qty if > 0, else fallback to config
        if self.qty == 0:
            self.qty = settings["qty"]
        
        self.short_call = None
        self.short_put = None
        self.last_selected = {"ce": None, "pe": None}
        self.target_ltp = settings["target_premium"]
        
        # Get absolute profit/stop-loss from config
        self.target_profit = settings["target_profit"]  # Absolute value in rupees
        self.stop_loss = settings["stop_loss"]          # Absolute value in rupees
        
        # Max adjustments from config
        self.MAX_ADJUSTMENTS_PER_LEG = settings.get("max_adjustments", 30)
        
        # Position tracking: {symbol: {"entry_price": X, "qty": Y, "side": -1}}
        self.positions = {}
        
        # Adjustment tracking: count how many times each leg has been adjusted
        self.ce_adjustment_count = 0
        self.pe_adjustment_count = 0
        
        # Track realized P&L
        self.realized_pnl = 0.0

    def start(self):
        """Initialize strategy (strike selection happens autonomously in on_tick)."""
        index = self.trade_ctx.get("index", "NIFTY")
        logger.info(
            f"[ShortStrangleReExec] Started for {index} | "
            f"Target LTP={self.target_ltp} | "
            f"DTE={self.trade_ctx.get('dte', 0)}"
        )

    
    def _on_position_tick(self, symbol: str, tick: dict):
        """
        REAL-TIME callback - called immediately when position symbol receives tick.
        
        This enables instant target/SL checks (no 5-second delay).
        Orders are executed as soon as target/SL is hit.
        """
        if symbol not in self.positions:
            return
        
        self._check_position_target_sl(symbol, tick)
    
    def _check_position_target_sl(self, symbol: str, tick: dict):
        """
        Check target/SL for a position - called on EVERY tick update (real-time).
        
        Args:
            symbol: Position symbol
            tick: Real-time tick data
        """
        if not tick or 'ltp' not in tick:
            return
        
        pos_data = self.positions.get(symbol)
        if not pos_data:
            return
        
        try:
            current_ltp = float(tick['ltp'])
        except (TypeError, ValueError):
            return
        
        entry_price = pos_data["entry_price"]
        
        # Update tracking info
        pos_data["current_ltp"] = current_ltp
        pos_data["pnl"] = entry_price - current_ltp  # SHORT P&L
        
        pnl = pos_data["pnl"]
        
        # Check if target or SL hit
        if pnl >= self.target_profit:
            logger.info(f"[ShortStrangleReExec] TARGET HIT (real-time) on {symbol}: P&L={pnl:.2f} Rs")
            self._handle_leg_adjustment(symbol, self.trade_ctx.get("option_symbols", []), entry_price)
        elif pnl <= -self.stop_loss:
            logger.warning(f"[ShortStrangleReExec] STOPLOSS HIT (real-time) on {symbol}: P&L={pnl:.2f} Rs")
            self._handle_leg_adjustment(symbol, self.trade_ctx.get("option_symbols", []), entry_price)

    def on_tick(self):
        """Autonomously select nearest CE/PE strikes, place orders, and manage positions."""
        # print("[ShortStrangle] on_tick called", flush=True)
        option_symbols = self.trade_ctx.get("option_symbols", [])
        if not option_symbols:
            logger.warning("[ShortStrangleReExec] No option symbols available")
            print("[ShortStrangleReExec] No option symbols available")
            return

        # Select nearest CE & PE strikes from live websocket data autonomously
        ce_sym, ce_ltp = select_by_premium(
            option_symbols,
            self.live_data,
            target_premium=self.target_ltp,
            ce_pe_type="CE"
        )
        
        pe_sym, pe_ltp = select_by_premium(
            option_symbols,
            self.live_data,
            target_premium=self.target_ltp,
            ce_pe_type="PE"
        )

        if not (ce_sym and pe_sym):
            logger.debug("[ShortStrangleReExec] Could not select both CE and PE")
            print(f"[ShortStrangleReExec] NO SELECTION: CE={ce_sym}, PE={pe_sym}", flush=True)
            return
        
        # Debug: show first tick selection  
        if not hasattr(self, '_first_tick_logged'):
            print(f"[Strangle] First tick: CE={ce_sym}@{ce_ltp:.2f} PE={pe_sym}@{pe_ltp:.2f}", flush=True)
            logger.debug(f"[Strangle] First tick: CE={ce_sym}@{ce_ltp:.2f} PE={pe_sym}@{pe_ltp:.2f}")
            self._first_tick_logged = True

        # Position-aware logic:
        # 1) Update tracked LTP/pnl for any existing positions (so we don't re-enter every tick)
        for pos_sym, pos_data in list(self.positions.items()):
            tick = self.live_data.get(pos_sym)
            if tick and "ltp" in tick:
                try:
                    current_ltp = float(tick["ltp"])
                    pos_data["current_ltp"] = current_ltp
                    pos_data["pnl"] = pos_data.get("entry_price", 0) - current_ltp
                except (TypeError, ValueError):
                    pass

        # Only place new entries if strike selection changed AND there is no existing
        # open position on the selected strike(s). This prevents repeated entries on
        # the same strike every websocket tick.
        ce_changed = ce_sym != self.last_selected["ce"]
        pe_changed = pe_sym != self.last_selected["pe"]

        ce_has_position = ce_sym in self.positions
        pe_has_position = pe_sym in self.positions

        # Debug: Show entry decision on first few ticks
        if not hasattr(self, '_tick_count'):
            self._tick_count = 0
        self._tick_count += 1
        
        if self._tick_count <= 3:
            print(f"[Strangle Tick {self._tick_count}] CE={ce_sym}@{ce_ltp:.2f}, PE={pe_sym}@{pe_ltp:.2f}", flush=True)
            print(f"  ce_changed={ce_changed}, pe_changed={pe_changed}", flush=True)
            print(f"  ce_has_pos={ce_has_position}, pe_has_pos={pe_has_position}", flush=True)
            print(f"  positions={list(self.positions.keys())}", flush=True)
            print(f"  last_selected={self.last_selected}", flush=True)

        if (ce_changed or pe_changed) and not self.positions:
            print(f"[Strangle] ENTRY: CE={ce_sym}@{ce_ltp:.2f} PE={pe_sym}@{pe_ltp:.2f}", flush=True)
            logger.info(
                f"[ShortStrangleReExec] Strike change detected & FLAT positions: "
                f"CE={ce_sym} (LTP={ce_ltp:.2f}) | PE={pe_sym} (LTP={pe_ltp:.2f})"
            )
            self.last_selected["ce"] = ce_sym
            self.last_selected["pe"] = pe_sym

            # Place short strangle orders (sell both CE and PE)
            self._place_strangle_orders(ce_sym, ce_ltp, pe_sym, pe_ltp, qty=self.qty)
            # Report positions to tracker
            self.report_positions(self.positions, realized_pnl=self.realized_pnl)
        elif (ce_changed or pe_changed):
            logger.debug(
                f"[ShortStrangleReExec] Strike changed but skipping entry because open positions exist: "
                f"{list(self.positions.keys())}"
            )
        
        # Monitor positions and check for adjustments
        self._check_and_adjust_positions(option_symbols)
        
        # Report current positions after monitoring
        if self.positions:
            self.report_positions(self.positions, realized_pnl=self.realized_pnl)

    def stop(self, reason: str):
        """Close all strangle positions."""
        logger.info(f"[ShortStrangleReExec] Stopping: {reason}")
        if self.positions:
             logger.info(f"[ShortStrangleReExec] Squaring off {len(self.positions)} positions due to stop.")
             for symbol in list(self.positions.keys()):
                 self._square_off_leg(symbol)

    def _place_strangle_orders(self, ce_symbol: str, ce_ltp: float, pe_symbol: str, pe_ltp: float, qty: int = 150):
        """Helper to place both CE and PE short strangle orders using Fyers API format."""
        order_logger = get_order_logger("strangle_orders.csv") if PAPER_TRADING_MODE else None
        
        # Place short call order (SELL)
        ce_order_data = {
            "symbol": ce_symbol,
            "qty": qty,
            "side": -1,              # -1 = SELL
            "type": 2,              # 2 = market order
            "productType": "INTRADAY",
            "limitPrice": 0,
            "stopPrice": 0,
            "validity": "DAY",
            "disclosedQty": 0,
            "offlineOrder": False,
            "orderTag": "CESHORT",
            "isSliceOrder": False,
        }
        
        if PAPER_TRADING_MODE:
            # Skip placing/logging if we already have an open position on this symbol
            if ce_symbol in self.positions:
                self.logger.debug(f"[PAPER] CE already has position, skipping new CE entry: {ce_symbol}")
            else:
                order_logger.log_order(
                    symbol=ce_symbol,
                    qty=qty,
                    side=-1,
                    order_type=2,
                    entry_price=ce_ltp,
                    order_tag="CESHORT",
                    status="PLACED"
                )
                self.positions[ce_symbol] = {"entry_price": ce_ltp, "qty": qty, "side": -1, "leg": "CE"}
                self._add_position_symbol(ce_symbol)  # Register for real-time monitoring
                self.logger.info(f"[PAPER] Logged short call: {ce_symbol} @ {ce_ltp} | Real-time monitoring ENABLED")
        else:
            # Place live order via broker
            ce_resp = self.place_order_safe(ce_order_data)
            if ce_resp.get("code") == 0:
                self.short_call = ce_symbol
                self.positions[ce_symbol] = {"entry_price": ce_ltp, "qty": qty, "side": -1, "leg": "CE"}
                self._add_position_symbol(ce_symbol)  # Register for real-time monitoring
                print(f"[ShortStrangleReExec] PLACED SHORT CALL: {ce_symbol} @ {ce_ltp}", flush=True)
                self.logger.info(f"[ShortStrangleReExec] Placed short call: {ce_symbol} @ {ce_ltp} | Real-time monitoring ENABLED")
            else:
                self.logger.warning(f"[ShortStrangleReExec] Failed to place CE order: {ce_resp.get('message')}")

        # Place short put order (SELL)
        pe_order_data = {
            "symbol": pe_symbol,
            "qty": qty,
            "side": -1,              # -1 = SELL
            "type": 2,              # 2 = market order
            "productType": "INTRADAY",
            "limitPrice": 0,
            "stopPrice": 0,
            "validity": "DAY",
            "disclosedQty": 0,
            "offlineOrder": False,
            "orderTag": "PESHORT",
            "isSliceOrder": False,
        }
        
        if PAPER_TRADING_MODE:
            # Skip placing/logging if we already have an open position on this symbol
            if pe_symbol in self.positions:
                self.logger.debug(f"[PAPER] PE already has position, skipping new PE entry: {pe_symbol}")
            else:
                order_logger.log_order(
                    symbol=pe_symbol,
                    qty=qty,
                    side=-1,
                    order_type=2,
                    entry_price=pe_ltp,
                    order_tag="PESHORT",
                    status="PLACED"
                )
                self.positions[pe_symbol] = {"entry_price": pe_ltp, "qty": qty, "side": -1, "leg": "PE"}
                self._add_position_symbol(pe_symbol)  # Register for real-time monitoring
                self.logger.info(f"[PAPER] Logged short put: {pe_symbol} @ {pe_ltp} | Real-time monitoring ENABLED")
        else:
            # Place live order via broker
            pe_resp = self.place_order_safe(pe_order_data)
            if pe_resp.get("code") == 0:
                self.short_put = pe_symbol
                self.positions[pe_symbol] = {"entry_price": pe_ltp, "qty": qty, "side": -1, "leg": "PE"}
                self._add_position_symbol(pe_symbol)  # Register for real-time monitoring
                print(f"[ShortStrangleReExec] PLACED SHORT PUT: {pe_symbol} @ {pe_ltp}", flush=True)
                self.logger.info(f"[ShortStrangleReExec] Placed short put: {pe_symbol} @ {pe_ltp} | Real-time monitoring ENABLED")
            else:
                self.logger.warning(f"[ShortStrangleReExec] Failed to place PE order: {pe_resp.get('message')}")

        # ---------------------------
        # HEDGE PLACEMENT (Protection)
        # ---------------------------
        # Find hedges based on logic: Nifty ~1 Rs premium, Sensex ~6 Rs premium OR max 20 strikes away
        index = self.trade_ctx.get("index", "NIFTY") 
        hedge_premium = 6.0 if index == "SENSEX" else 1.0
        
        # Get base strikes from selected symbols
        try:
            from greeks_calculator import parse_symbol_info
            
            # CE Hedge
            ce_parsed = parse_symbol_info(ce_symbol)
            if ce_parsed:
                ce_strike = ce_parsed[0]
                hedge_ce, hedge_ce_ltp = find_hedge_strike(
                    self.trade_ctx.get("option_symbols", []),
                    self.live_data,
                    base_strike=ce_strike,
                    ce_pe_type="CE",
                    target_premium=hedge_premium,
                    max_strikes_away=20
                )
                
                if hedge_ce:
                    self._place_hedge_order(hedge_ce, hedge_ce_ltp, qty, "CE")
            
            # PE Hedge
            pe_parsed = parse_symbol_info(pe_symbol)
            if pe_parsed:
                pe_strike = pe_parsed[0]
                hedge_pe, hedge_pe_ltp = find_hedge_strike(
                    self.trade_ctx.get("option_symbols", []),
                    self.live_data,
                    base_strike=pe_strike,
                    ce_pe_type="PE",
                    target_premium=hedge_premium,
                    max_strikes_away=20
                )
                
                if hedge_pe:
                    self._place_hedge_order(hedge_pe, hedge_pe_ltp, qty, "PE")
                    
        except Exception as e:
            self.logger.error(f"[ShortStrangleReExec] Failed to place hedges: {e}")

    def _place_hedge_order(self, symbol, ltp, qty, leg_type):
        """Place BUY order for hedge protection."""
        order_data = {
            "symbol": symbol,
            "qty": qty,
            "side": 1,              # 1 = BUY
            "type": 2,              # MARKET
            "productType": "INTRADAY",
            "limitPrice": 0,
            "stopPrice": 0,
            "validity": "DAY",
            "disclosedQty": 0,
            "offlineOrder": False,
            "orderTag": f"HEDGE_{leg_type}",
            "isSliceOrder": False,
        }
        
        if PAPER_TRADING_MODE:
            if symbol not in self.positions:
                self.positions[symbol] = {
                    "entry_price": ltp, 
                    "qty": qty, 
                    "side": 1, 
                    "leg": leg_type,
                    "is_hedge": True
                }
                self._add_position_symbol(symbol)
                self.logger.info(f"[PAPER] Placed HEDGE {leg_type}: {symbol} @ {ltp}")
        else:
            resp = self.place_order_safe(order_data)
            if resp.get("code") == 0:
                self.positions[symbol] = {
                    "entry_price": ltp, 
                    "qty": qty, 
                    "side": 1, 
                    "leg": leg_type,
                    "is_hedge": True
                }
                self._add_position_symbol(symbol)
                self.logger.info(f"[ShortStrangleReExec] PLACED HEDGE {leg_type}: {symbol} @ {ltp}")
            else:
                self.logger.warning(f"[ShortStrangleReExec] Failed to place HEDGE {leg_type}: {resp.get('message')}")

    def _check_and_adjust_positions(self, option_symbols):
        """Monitor P&L on each leg and adjust if target/SL hit."""
        for symbol, pos_data in list(self.positions.items()):
            # Skip hedge positions for adjustments
            if pos_data.get("is_hedge", False):
                continue

            entry_price = pos_data["entry_price"]
            
            # Get current LTP
            tick = self.live_data.get(symbol)
            if not tick or "ltp" not in tick:
                continue
            
            try:
                current_ltp = float(tick["ltp"])
            except (TypeError, ValueError):
                continue

            # Update tracking info on the position for monitoring
            pos_data["current_ltp"] = current_ltp
            pos_data["pnl"] = entry_price - current_ltp  # for short position

            pnl = pos_data["pnl"]

            # Log small debug info about LTP movement
            self.logger.debug(f"[ShortStrangleReExec] Pos {symbol} entry={entry_price} ltp={current_ltp} pnl={pnl:.2f}")

            # Check if target or SL hit (only then we square off and re-enter)
            if pnl >= getattr(self, "target_profit", 3.0):
                self.logger.info(f"[ShortStrangleReExec] TARGET HIT on {symbol}: P&L={pnl:.2f}")
                self._handle_leg_adjustment(symbol, option_symbols, entry_price)
            elif pnl <= -getattr(self, "stop_loss", 3.0):
                self.logger.warning(f"[ShortStrangleReExec] STOPLOSS HIT on {symbol}: P&L={pnl:.2f}")
                self._handle_leg_adjustment(symbol, option_symbols, entry_price)
    
    def _handle_leg_adjustment(self, current_symbol: str, option_symbols, entry_price: float):
        """
        When target/SL hit on a leg:
        1. Find the strike with LTP nearest to TARGET_LTP (50 for SENSEX, 10 for NIFTY)
        2. If the NEW closest strike == current symbol, don't close (keep position)
        3. Else: square off current position and re-enter the strike that is now closest to TARGET_LTP
        4. Track adjustments (max 30 per leg)
        """
        ce_pe_type = "CE" if current_symbol.endswith("CE") else "PE"
        
        # Check if max adjustments reached for this leg
        if ce_pe_type == "CE":
            if self.ce_adjustment_count >= self.MAX_ADJUSTMENTS_PER_LEG:
                self.logger.warning(
                    f"[ShortStrangleReExec] Max CE adjustments ({self.MAX_ADJUSTMENTS_PER_LEG}) reached, closing ALL positions"
                )
                self.stop("Max CE Adjustments Reached")
                return
        else:
            if self.pe_adjustment_count >= self.MAX_ADJUSTMENTS_PER_LEG:
                self.logger.warning(
                    f"[ShortStrangleReExec] Max PE adjustments ({self.MAX_ADJUSTMENTS_PER_LEG}) reached, closing ALL positions"
                )
                self.stop("Max PE Adjustments Reached")
                return
        
        # Find strike with LTP nearest to TARGET_LTP (same target as initial entry)
        # For SENSEX: 50 rupees, For NIFTY: 10 rupees
        adjustment_sym, adjustment_ltp = select_by_premium(
            option_symbols,
            self.live_data,
            target_premium=self.target_ltp,  # Use TARGET_LTP, not 10-rupee premium
            ce_pe_type=ce_pe_type
        )
        
        if not adjustment_sym:
            self.logger.warning(f"[ShortStrangleReExec] Could not find adjustment strike for {ce_pe_type}")
            return
        
        # Check if the strike that is now closest to TARGET_LTP is the same as current
        if adjustment_sym == current_symbol:
            self.logger.info(
                f"[ShortStrangleReExec] {current_symbol} still closest to TARGET_LTP={self.target_ltp}, "
                f"keeping position (no square-off needed)"
            )
            return
        
        # Different strike is now closest to TARGET_LTP - square off old and re-enter new
        self.logger.info(
            f"[ShortStrangleReExec] {ce_pe_type} adjustment: {current_symbol} (LTP near SL/target) "
            f"-> {adjustment_sym} (now closest to TARGET_LTP={self.target_ltp})"
        )
        self._square_off_leg(current_symbol)
        self._reenter_leg(adjustment_sym, adjustment_ltp, ce_pe_type)
        
        # Increment adjustment counter
        if ce_pe_type == "CE":
            self.ce_adjustment_count += 1
            self.logger.info(f"[ShortStrangleReExec] CE adjustments: {self.ce_adjustment_count}/{self.MAX_ADJUSTMENTS_PER_LEG}")
        else:
            self.pe_adjustment_count += 1
            self.logger.info(f"[ShortStrangleReExec] PE adjustments: {self.pe_adjustment_count}/{self.MAX_ADJUSTMENTS_PER_LEG}")
    
    def _square_off_leg(self, symbol: str):
        """Close a position by buying (cover) the short."""
        if symbol not in self.positions:
            self.logger.warning(f"[ShortStrangleReExec] Position not found for {symbol}")
            return
        
        pos_data = self.positions[symbol]
        qty = pos_data["qty"]
        entry_price = pos_data["entry_price"]
        
        order_logger = get_order_logger("strangle_orders.csv") if PAPER_TRADING_MODE else None
        
        # Determine square off side (opposite of position side)
        # side: -1 (SELL) -> 1 (BUY)
        # side: 1 (BUY) -> -1 (SELL)
        exit_side = 1 if pos_data["side"] == -1 else -1

        square_off_order = {
            "symbol": symbol,
            "qty": qty,
            "side": exit_side,              # Opposite of entry side
            "type": 2,              # MARKET
            "productType": "INTRADAY",
            "limitPrice": 0,
            "stopPrice": 0,
            "validity": "DAY",
            "disclosedQty": 0,
            "offlineOrder": False,
            "orderTag": "SQUAREOFF",
            "isSliceOrder": False,
        }
        
        if PAPER_TRADING_MODE:
            # Get current LTP for logging
            tick = self.live_data.get(symbol)
            current_ltp = float(tick["ltp"]) if tick and "ltp" in tick else 0
            pnl = (pos_data["entry_price"] - current_ltp) * qty  # Total P&L for short
            
            order_logger.log_order(
                symbol=symbol,
                qty=qty,
                side=exit_side,
                order_type=2,
                entry_price=current_ltp,
                pnl=pnl,
                order_tag="SQUAREOFF",
                status="PLACED"
            )
            self._remove_position_symbol(symbol)  # Unregister callback
            del self.positions[symbol]
            self.logger.info(f"[PAPER] Logged square-off for {symbol} | P&L: {pnl:.2f} | Real-time monitoring DISABLED")
            
            # Update Realized P&L
            self.realized_pnl += pnl
            # Report updated positions
            self.report_positions(self.positions)
        else:
            # Place live order via broker
            resp = self.place_order_safe(square_off_order)
            if resp.get("code") == 0:
                # Calculate realized P&L for this leg
                # For SHORT position: (entry - exit) * qty
                # We need exit price. In market order we don't know exact execution price immediately
                # but we can use current live LTP as approximation or wait for order update (complex).
                # Using current LTP from live_data for P&L tracking:
                tick = self.live_data.get(symbol)
                current_ltp = float(tick["ltp"]) if tick and "ltp" in tick else entry_price
                
                leg_pnl = (entry_price - current_ltp) * qty
                self.realized_pnl += leg_pnl
                
                self._remove_position_symbol(symbol)  # Unregister callback
                del self.positions[symbol]
                print(f"[ShortStrangleReExec] SQUARED OFF {symbol} | P&L: {leg_pnl:.2f}", flush=True)
                self.logger.info(f"[ShortStrangleReExec] Squared off {symbol} | Realized P&L: {leg_pnl:.2f} | Real-time monitoring DISABLED")
            else:
                self.logger.error(f"[ShortStrangleReExec] Failed to square off {symbol}: {resp.get('message')}")
    
    def _reenter_leg(self, symbol: str, ltp: float, ce_pe_type: str):
        """Re-enter a short position on a new strike."""
        order_logger = get_order_logger("strangle_orders.csv") if PAPER_TRADING_MODE else None
        
        reentry_order = {
            "symbol": symbol,
            "qty": self.qty,
            "side": -1,             # -1 = SELL
            "type": 2,              # MARKET
            "productType": "INTRADAY",
            "limitPrice": 0,
            "stopPrice": 0,
            "validity": "DAY",
            "disclosedQty": 0,
            "offlineOrder": False,
            "orderTag": f"REENTER_{ce_pe_type}",
            "isSliceOrder": False,
        }
        
        if PAPER_TRADING_MODE:
            # Log to CSV instead of placing live order
            order_logger.log_order(
                symbol=symbol,
                qty=self.qty,
                side=-1,
                order_type=2,
                entry_price=ltp,
                adjustment_count=self.ce_adjustment_count if ce_pe_type == "CE" else self.pe_adjustment_count,
                order_tag=f"REENTER_{ce_pe_type}",
                status="PLACED"
            )
            self.positions[symbol] = {"entry_price": ltp, "qty": self.qty, "side": -1, "leg": ce_pe_type}
            self._add_position_symbol(symbol)  # Register for real-time monitoring
            self.logger.info(f"[PAPER] Logged re-entry {ce_pe_type} at {symbol} @ {ltp} | Real-time monitoring ENABLED")
            # Report updated positions
            self.report_positions(self.positions, realized_pnl=self.realized_pnl)
        else:
            # Place live order via broker
            resp = self.place_order_safe(reentry_order)
            if resp.get("code") == 0:
                self.positions[symbol] = {"entry_price": ltp, "qty": self.qty, "side": -1, "leg": ce_pe_type}
                self._add_position_symbol(symbol)  # Register for real-time monitoring
                print(f"[ShortStrangleReExec] RE-ENTERED {ce_pe_type} at {symbol} @ {ltp}", flush=True)
                self.logger.info(f"[ShortStrangleReExec] Re-entered {ce_pe_type} at {symbol} @ {ltp} | Real-time monitoring ENABLED")
            else:
                self.logger.error(f"[ShortStrangleReExec] Failed to re-enter {symbol}: {resp.get('message')}")

