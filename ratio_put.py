# strategies/ratio_put.py
"""
Ratio Put Strategy: ATM Iron Fly with Extra ITM PE for Downside Protection

Strategy Structure (5 legs):
1. SELL ATM CE - 2 lots
2. SELL ATM PE - 2 lots
3. BUY ITM1 PE (ATM+1) - 1 lot (extra downside protection)
4. BUY Hedge CE (ATM+4) - 2 lots
5. BUY Hedge PE (ATM-4) - 2 lots

Adjustment Logic:
- Short legs (ATM CE/PE) + Hedges: Re-execute when index moves ±0.4%
- ITM1 PE: Independent management with -0.7% index move target (no SL)

Risk Profile:
- Max profit: Net premium from shorts minus cost of hedges and ITM1 PE
- Max loss: Limited by hedges
- Enhanced downside protection via ITM1 PE ratio
"""

import logging
try:
    from .base import BaseStrategy
except Exception:
    from basestrategy import BaseStrategy
from order_logger import get_order_logger
from config.config import RATIO_PUT_SETTINGS, PAPER_TRADING_MODE

logger = logging.getLogger(__name__)


class RatioPut(BaseStrategy):
    """
    Ratio Put Strategy with asymmetric put protection.
    
    Similar to Iron Fly but with an additional ITM put leg for enhanced
    downside protection, making it suitable for markets with downside risk.
    """

    def __init__(self, trade_ctx, live_data, broker=None, qty=0):
        super().__init__(trade_ctx, live_data, broker, qty)
        
        # Get index-specific settings
        index = trade_ctx.get("index", "NIFTY")
        settings = RATIO_PUT_SETTINGS.get(index, RATIO_PUT_SETTINGS["NIFTY"])
        
        # Get lot size from context
        self.lot_size = trade_ctx.get("entry_config", {}).get("lot_size", 75)
        
        # Calculate quantities for each leg based on lots
        self.short_qty = settings["short_qty_lots"] * self.lot_size  # 2 lots
        self.itm1_qty = settings["itm1_qty_lots"] * self.lot_size    # 1 lot
        self.hedge_qty = settings["hedge_qty_lots"] * self.lot_size  # 2 lots
        
        # Adjustment settings
        self.adjustment_threshold_pct = settings["adjustment_threshold_pct"]  # ±0.4%
        self.hedge_otm_steps = settings["hedge_otm_steps"]  # 4 strikes
        self.itm1_target_index_move_pct = settings["itm1_target_index_move_pct"]  # -0.7%
        
        # Position tracking: {symbol: {entry_price, qty, side, leg_type}}
        self.positions = {}
        
        # Last selected strikes for change detection
        self.last_selected = {
            "short_call": None,
            "short_put": None,
            "itm1_put": None,
            "hedge_call": None,
            "hedge_put": None,
        }
        
        # Index tracking for adjustments
        self.entry_index_price = None  # Index at initial entry
        self.last_adjustment_index_price = None  # Index at last adjustment
        self.itm1_entry_index_price = None  # Index when ITM1 PE was entered
        
        self.position_active = False
        self.itm1_active = False  # Track ITM1 PE separately
        self.adjustment_count = 0
        
        # Realized P&L tracking
        self.realized_pnl = 0.0

    def start(self):
        """Initialize Ratio Put strategy."""
        index = self.trade_ctx.get("index", "NIFTY")
        logger.info(
            f"[RatioPut] Started for {index} | "
            f"ATM={self.trade_ctx['atm_strike']} | DTE={self.trade_ctx.get('dte', 0)} | "
            f"Short Qty={self.short_qty} | ITM1 Qty={self.itm1_qty} | Hedge Qty={self.hedge_qty} | "
            f"Adjustment Threshold=±{self.adjustment_threshold_pct}% | "
            f"ITM1 Target={self.itm1_target_index_move_pct}%"
        )

    def _on_position_tick(self, symbol: str, tick: dict):
        """
        Real-time callback for position tick updates.
        Monitor ITM1 PE target based on index movement.
        """
        if symbol not in self.positions:
            return
        
        # Check if this is the ITM1 PE leg
        pos_data = self.positions.get(symbol)
        if pos_data and pos_data.get("leg") == "itm1_put":
            self._check_itm1_target(tick)

    def on_tick(self):
        """Main strategy tick - select strikes and manage positions."""
        option_symbols = self.trade_ctx.get("option_symbols", [])
        if not option_symbols:
            logger.warning("[RatioPut] No option symbols available")
            return

        # If position active, monitor for adjustments
        if self.position_active:
            self._monitor_positions()
            # Report current positions
            if self.positions:
                metadata = self._get_metadata()
                self.report_positions(self.positions, metadata=metadata, realized_pnl=self.realized_pnl)
            return

        # Position not active - attempt entry/re-entry
        if self.adjustment_count > 0:
            logger.info(f"[RatioPut] ATTEMPTING RE-ENTRY after adjustment {self.adjustment_count}")
        
        # Select 5 strikes for ratio put
        strikes = self._select_ratio_put_strikes(option_symbols)
        if not strikes:
            self.logger.debug("[RatioPut] Could not select all 5 legs - skipping this tick")
            return
        
        self.logger.debug(f"[RatioPut] Successfully selected strikes: {list(strikes.keys())}")

        # Extract strike data
        short_call_sym, short_call_ltp = strikes["short_call"]
        short_put_sym, short_put_ltp = strikes["short_put"]
        itm1_put_sym, itm1_put_ltp = strikes["itm1_put"]
        hedge_call_sym, hedge_call_ltp = strikes["hedge_call"]
        hedge_put_sym, hedge_put_ltp = strikes["hedge_put"]

        # Check if strikes have changed
        current = {
            "short_call": short_call_sym,
            "short_put": short_put_sym,
            "itm1_put": itm1_put_sym,
            "hedge_call": hedge_call_sym,
            "hedge_put": hedge_put_sym,
        }

        if current != self.last_selected:
            entry_type = "RE-ENTRY" if self.adjustment_count > 0 else "INITIAL ENTRY"
            logger.info(
                f"[RatioPut] {entry_type} - Placing 5-leg order: "
                f"SELL {short_call_sym}@{short_call_ltp:.2f} | "
                f"SELL {short_put_sym}@{short_put_ltp:.2f} | "
                f"BUY ITM1 {itm1_put_sym}@{itm1_put_ltp:.2f} | "
                f"BUY Hedge {hedge_call_sym}@{hedge_call_ltp:.2f} | "
                f"BUY Hedge {hedge_put_sym}@{hedge_put_ltp:.2f}"
            )
            print(f"[RatioPut] {entry_type} - Placing 5-leg Ratio Put order", flush=True)
            self.last_selected = current
            
            # Place all 5 legs
            self._place_ratio_put_orders(strikes)

    def _select_ratio_put_strikes(self, option_symbols):
        """
        Select 5 strikes for ratio put strategy.
        
        For NIFTY (step=50), ATM=26000:
        - Short CE: 26000 (ATM)
        - Short PE: 26000 (ATM)
        - ITM1 PE: 26050 (ATM+1, one strike above = ITM for PUT)
        - Hedge CE: 26200 (ATM+4, 4 strikes above)
        - Hedge PE: 25800 (ATM-4, 4 strikes below)
        """
        atm_strike = self.trade_ctx.get("atm_strike", 24000)
        index = self.trade_ctx.get("index", "NIFTY")
        step = 50 if index == "NIFTY" else 100
        
        # Calculate strike prices
        short_call_strike = atm_strike  # ATM
        short_put_strike = atm_strike   # ATM
        itm1_put_strike = atm_strike + step  # ATM+1 (ITM for PUT)
        hedge_call_strike = atm_strike + (self.hedge_otm_steps * step)  # ATM+4
        hedge_put_strike = atm_strike - (self.hedge_otm_steps * step)   # ATM-4
        
        self.logger.debug(
            f"[RatioPut] Calculated strikes: "
            f"Short CE/PE={atm_strike}, ITM1 PE={itm1_put_strike}, "
            f"Hedge CE={hedge_call_strike}, Hedge PE={hedge_put_strike}"
        )
        
        # Find symbols matching these strikes
        short_call_sym = self._find_symbol_by_strike_with_fallback(
            option_symbols, short_call_strike, "CE", max_distance=step
        )
        short_put_sym = self._find_symbol_by_strike_with_fallback(
            option_symbols, short_put_strike, "PE", max_distance=step
        )
        itm1_put_sym = self._find_symbol_by_strike_with_fallback(
            option_symbols, itm1_put_strike, "PE", max_distance=step
        )
        hedge_call_sym = self._find_symbol_by_strike_with_fallback(
            option_symbols, hedge_call_strike, "CE", max_distance=step*2
        )
        hedge_put_sym = self._find_symbol_by_strike_with_fallback(
            option_symbols, hedge_put_strike, "PE", max_distance=step*2
        )
        
        # Verify all symbols found
        if not all([short_call_sym, short_put_sym, itm1_put_sym, hedge_call_sym, hedge_put_sym]):
            missing = []
            if not short_call_sym: missing.append(f"Short CE@{short_call_strike}")
            if not short_put_sym: missing.append(f"Short PE@{short_put_strike}")
            if not itm1_put_sym: missing.append(f"ITM1 PE@{itm1_put_strike}")
            if not hedge_call_sym: missing.append(f"Hedge CE@{hedge_call_strike}")
            if not hedge_put_sym: missing.append(f"Hedge PE@{hedge_put_strike}")
            
            self.logger.warning(f"[RatioPut] Could not find symbols: {', '.join(missing)}")
            return None
        
        # Get LTPs
        short_call_ltp = self._get_ltp(short_call_sym)
        short_put_ltp = self._get_ltp(short_put_sym)
        itm1_put_ltp = self._get_ltp(itm1_put_sym)
        hedge_call_ltp = self._get_ltp(hedge_call_sym)
        hedge_put_ltp = self._get_ltp(hedge_put_sym)
        
        # Check for missing LTPs
        if not all([short_call_ltp is not None, short_put_ltp is not None, 
                    itm1_put_ltp is not None, hedge_call_ltp is not None, hedge_put_ltp is not None]):
            self.logger.warning("[RatioPut] Missing LTP data for some symbols. Will retry.")
            return None
        
        return {
            "short_call": (short_call_sym, short_call_ltp),
            "short_put": (short_put_sym, short_put_ltp),
            "itm1_put": (itm1_put_sym, itm1_put_ltp),
            "hedge_call": (hedge_call_sym, hedge_call_ltp),
            "hedge_put": (hedge_put_sym, hedge_put_ltp),
        }

    def _find_symbol_by_strike_with_fallback(self, option_symbols, target_strike, option_type, max_distance=100):
        """Find symbol with exact or nearest strike."""
        # Try exact match first
        for symbol in option_symbols:
            if not symbol.endswith(option_type):
                continue
            strike = self._extract_strike(symbol)
            if strike == target_strike:
                return symbol
        
        # Fallback to nearest
        candidates = []
        for symbol in option_symbols:
            if not symbol.endswith(option_type):
                continue
            strike = self._extract_strike(symbol)
            if strike:
                distance = abs(strike - target_strike)
                if distance <= max_distance:
                    candidates.append((distance, strike, symbol))
        
        if not candidates:
            return None
        
        candidates.sort()
        distance, actual_strike, symbol = candidates[0]
        
        if distance > 0:
            self.logger.info(
                f"[RatioPut] Using fallback: {symbol} (strike {actual_strike}) "
                f"instead of exact {target_strike} (distance: {distance})"
            )
        
        return symbol

    def _extract_strike(self, symbol):
        """Extract strike price from symbol string."""
        try:
            import re
            
            if "NIFTY" in symbol:
                match = re.search(r'(\d+)(CE|PE)$', symbol)
                if match:
                    full_num = match.group(1)
                    if len(full_num) >= 5:
                        strike = int(full_num[-5:])
                        if 15000 <= strike <= 40000:
                            return strike
                    if len(full_num) >= 4:
                        strike = int(full_num[-4:])
                        if 1000 <= strike <= 9999:
                            return strike
            
            elif "SENSEX" in symbol:
                match = re.search(r'(\d+)(CE|PE)$', symbol)
                if match:
                    full_num = match.group(1)
                    if len(full_num) >= 6:
                        strike = int(full_num[-6:])
                        if 50000 <= strike <= 150000:
                            return strike
                    if len(full_num) >= 5:
                        strike = int(full_num[-5:])
                        if 50000 <= strike <= 99999:
                            return strike
            
            return None
        except (ValueError, IndexError):
            return None

    def _get_ltp(self, symbol):
        """Get LTP for a symbol from live data."""
        tick = self.live_data.get(symbol)
        if tick and "ltp" in tick:
            try:
                return float(tick["ltp"])
            except (TypeError, ValueError):
                pass
        return None

    def _place_ratio_put_orders(self, strikes):
        """Place all 5 legs of ratio put strategy."""
        order_logger = get_order_logger("ratio_put_orders.csv") if PAPER_TRADING_MODE else None
        
        legs = [
            ("short_call", strikes["short_call"], -1, self.short_qty, "SHORT_CALL"),
            ("short_put", strikes["short_put"], -1, self.short_qty, "SHORT_PUT"),
            ("itm1_put", strikes["itm1_put"], 1, self.itm1_qty, "ITM1_PUT"),
            ("hedge_call", strikes["hedge_call"], 1, self.hedge_qty, "HEDGE_CALL"),
            ("hedge_put", strikes["hedge_put"], 1, self.hedge_qty, "HEDGE_PUT"),
        ]
        
        for leg_name, (symbol, ltp), side, qty, tag in legs:
            order_data = {
                "symbol": symbol,
                "qty": qty,
                "side": side,
                "type": 2,  # MARKET order
                "productType": "INTRADAY",
                "limitPrice": 0,
                "stopPrice": 0,
                "validity": "DAY",
                "disclosedQty": 0,
                "offlineOrder": False,
                "orderTag": tag,
                "isSliceOrder": False,
            }
            
            if PAPER_TRADING_MODE:
                order_logger.log_order(
                    symbol=symbol,
                    qty=qty,
                    side=side,
                    order_type=2,
                    entry_price=ltp,
                    order_tag=tag,
                    status="PLACED"
                )
                self.positions[symbol] = {
                    "entry_price": ltp,
                    "qty": qty,
                    "side": side,
                    "leg": leg_name
                }
                self._add_position_symbol(symbol)  # Register for real-time monitoring
                self.logger.info(f"[PAPER] Logged {tag}: {symbol} @ {ltp} | Qty={qty}")
            else:
                resp = self.place_order_safe(order_data)
                if resp.get("code") == 0:
                    self.positions[symbol] = {
                        "entry_price": ltp,
                        "qty": qty,
                        "side": side,
                        "leg": leg_name
                    }
                    self._add_position_symbol(symbol)  # Register for real-time monitoring
                    self.logger.info(f"[RatioPut] Placed {tag}: {symbol} @ {ltp} | Qty={qty}")
                else:
                    self.logger.warning(f"[RatioPut] Failed to place {tag}: {resp.get('message')}")
        
        self.position_active = True
        self.itm1_active = True
        
        # Capture index prices
        current_index = self._get_current_index_price()
        if self.entry_index_price is None:
            self.entry_index_price = current_index
            self.last_adjustment_index_price = current_index
            self.itm1_entry_index_price = current_index
            logger.info(
                f"[RatioPut] Initial entry | Index: {current_index:.2f} | "
                f"Adjustments: {self.adjustment_count}"
            )
        else:
            self.last_adjustment_index_price = current_index
            # ITM1 PE might still be from previous entry
            if not self.itm1_active:
                self.itm1_entry_index_price = current_index
            logger.info(
                f"[RatioPut] Re-entry after adjustment {self.adjustment_count} | "
                f"Index: {current_index:.2f}"
            )
        
        # Report positions
        metadata = self._get_metadata()
        self.report_positions(self.positions, metadata=metadata, realized_pnl=self.realized_pnl)

    def _monitor_positions(self):
        """Monitor positions for adjustment triggers and ITM1 PE target."""
        if not self.positions:
            return
        
        # Get current index price
        current_index_price = self._get_current_index_price()
        if current_index_price is None or self.last_adjustment_index_price is None:
            return
        
        # Calculate index movement from last adjustment
        index_movement_pct = ((current_index_price - self.last_adjustment_index_price) / 
                             self.last_adjustment_index_price) * 100
        
        logger.debug(
            f"[RatioPut] Index Movement: {index_movement_pct:.3f}% | "
            f"Current: {current_index_price:.2f} | Last Adjustment: {self.last_adjustment_index_price:.2f}"
        )
        
        # Check if adjustment threshold crossed (±0.4%)
        if abs(index_movement_pct) >= self.adjustment_threshold_pct:
            logger.info(
                f"[RatioPut] ADJUSTMENT TRIGGER: Index moved {index_movement_pct:.3f}% | "
                f"Threshold: ±{self.adjustment_threshold_pct}%"
            )
            self._adjust_position(current_index_price, index_movement_pct)
        
        # Check ITM1 PE target (independent of adjustment)
        if self.itm1_active:
            self._check_itm1_target_by_index()
        
        # Report positions with metadata
        metadata = self._get_metadata()
        self.report_positions(self.positions, metadata=metadata, realized_pnl=self.realized_pnl)

    def _check_itm1_target(self, tick: dict):
        """Check ITM1 PE target based on real-time tick (called by _on_position_tick)."""
        # For now, primarily using index-based check in _monitor_positions
        # This can be extended for premium-based targets if needed
        pass

    def _check_itm1_target_by_index(self):
        """Check if ITM1 PE target hit based on index movement."""
        if not self.itm1_active or self.itm1_entry_index_price is None:
            return
        
        current_index = self._get_current_index_price()
        if current_index is None:
            return
        
        # Calculate index movement from ITM1 PE entry
        index_move_pct = ((current_index - self.itm1_entry_index_price) / 
                         self.itm1_entry_index_price) * 100
        
        # Check if target hit (-0.7% downward move)
        if index_move_pct <= self.itm1_target_index_move_pct:
            logger.info(
                f"[RatioPut] ITM1 PE TARGET HIT: Index moved {index_move_pct:.3f}% | "
                f"Target: {self.itm1_target_index_move_pct}%"
            )
            self._exit_itm1_put("ITM1 target hit")

    def _exit_itm1_put(self, reason):
        """Exit only the ITM1 PE leg."""
        logger.info(f"[RatioPut] Exiting ITM1 PE: {reason}")
        order_logger = get_order_logger("ratio_put_orders.csv") if PAPER_TRADING_MODE else None
        
        # Find ITM1 PE position
        itm1_symbol = None
        for symbol, pos_data in self.positions.items():
            if pos_data.get("leg") == "itm1_put":
                itm1_symbol = symbol
                break
        
        if not itm1_symbol:
            logger.warning("[RatioPut] ITM1 PE position not found")
            return
        
        pos_data = self.positions[itm1_symbol]
        close_side = -pos_data["side"]  # Reverse side to close
        
        order_data = {
            "symbol": itm1_symbol,
            "qty": pos_data["qty"],
            "side": close_side,
            "type": 2,
            "productType": "INTRADAY",
            "limitPrice": 0,
            "stopPrice": 0,
            "validity": "DAY",
            "disclosedQty": 0,
            "offlineOrder": False,
            "orderTag": "EXIT_ITM1_PE",
            "isSliceOrder": False,
        }
        
        if PAPER_TRADING_MODE:
            tick = self.live_data.get(itm1_symbol)
            current_ltp = float(tick["ltp"]) if tick and "ltp" in tick else pos_data["entry_price"]
            
            # Calculate P&L
            pnl = (current_ltp - pos_data["entry_price"]) * pos_data["side"] * pos_data["qty"]
            
            order_logger.log_order(
                symbol=itm1_symbol,
                qty=pos_data["qty"],
                side=close_side,
                order_type=2,
                entry_price=current_ltp,
                pnl=pnl,
                order_tag="EXIT_ITM1_PE",
                status="PLACED"
            )
            self._remove_position_symbol(itm1_symbol)
            logger.info(f"[PAPER] Exited ITM1 PE {itm1_symbol} | P&L: {pnl:.2f}")
            
            self.realized_pnl += pnl
        else:
            resp = self.place_order_safe(order_data)
            if resp.get("code") == 0:
                tick = self.live_data.get(itm1_symbol)
                current_ltp = float(tick["ltp"]) if tick and "ltp" in tick else pos_data["entry_price"]
                pnl = (current_ltp - pos_data["entry_price"]) * pos_data["side"] * pos_data["qty"]
                self.realized_pnl += pnl
                
                self._remove_position_symbol(itm1_symbol)
                logger.info(f"[RatioPut] Closed ITM1 PE {itm1_symbol} | P&L: {pnl:.2f}")
            else:
                logger.error(f"[RatioPut] Failed to close ITM1 PE: {resp.get('message')}")
                return
        
        # Remove from positions
        del self.positions[itm1_symbol]
        self.itm1_active = False
        
        # Update last_selected to allow re-entry
        self.last_selected["itm1_put"] = None

    def _adjust_position(self, current_index_price, index_movement_pct):
        """
        Adjust position: Square off shorts + hedges, prepare for re-entry.
        ITM1 PE remains active unless it hits its own target.
        """
        logger.info(
            f"[RatioPut] ADJUSTING POSITION | Index: {current_index_price:.2f} | "
            f"Movement: {index_movement_pct:.3f}%"
        )
        
        # Exit shorts and hedges (keep ITM1 PE if active)
        self._exit_shorts_and_hedges("Adjustment trigger")
        
        # Increment adjustment count
        self.adjustment_count += 1
        
        # Reset last_selected for shorts/hedges to trigger re-entry
        self.last_selected["short_call"] = None
        self.last_selected["short_put"] = None
        self.last_selected["hedge_call"] = None
        self.last_selected["hedge_put"] = None
        # Don't reset ITM1 if it's still active

    def _exit_shorts_and_hedges(self, reason):
        """Exit short legs and hedge legs, keep ITM1 PE."""
        logger.info(f"[RatioPut] Exiting shorts and hedges: {reason}")
        order_logger = get_order_logger("ratio_put_orders.csv") if PAPER_TRADING_MODE else None
        
        # Identify legs to exit (all except ITM1 PE)
        to_exit = []
        for symbol, pos_data in list(self.positions.items()):
            if pos_data.get("leg") != "itm1_put":
                to_exit.append(symbol)
        
        for symbol in to_exit:
            pos_data = self.positions[symbol]
            close_side = -pos_data["side"]
            
            order_data = {
                "symbol": symbol,
                "qty": pos_data["qty"],
                "side": close_side,
                "type": 2,
                "productType": "INTRADAY",
                "limitPrice": 0,
                "stopPrice": 0,
                "validity": "DAY",
                "disclosedQty": 0,
                "offlineOrder": False,
                "orderTag": "ADJUST_EXIT",
                "isSliceOrder": False,
            }
            
            if PAPER_TRADING_MODE:
                tick = self.live_data.get(symbol)
                current_ltp = float(tick["ltp"]) if tick and "ltp" in tick else pos_data["entry_price"]
                pnl = (current_ltp - pos_data["entry_price"]) * pos_data["side"] * pos_data["qty"]
                
                order_logger.log_order(
                    symbol=symbol,
                    qty=pos_data["qty"],
                    side=close_side,
                    order_type=2,
                    entry_price=current_ltp,
                    pnl=pnl,
                    order_tag="ADJUST_EXIT",
                    status="PLACED"
                )
                self._remove_position_symbol(symbol)
                logger.info(f"[PAPER] Exited {symbol} | P&L: {pnl:.2f}")
                
                self.realized_pnl += pnl
            else:
                resp = self.place_order_safe(order_data)
                if resp.get("code") == 0:
                    tick = self.live_data.get(symbol)
                    current_ltp = float(tick["ltp"]) if tick and "ltp" in tick else pos_data["entry_price"]
                    pnl = (current_ltp - pos_data["entry_price"]) * pos_data["side"] * pos_data["qty"]
                    self.realized_pnl += pnl
                    
                    self._remove_position_symbol(symbol)
                    logger.info(f"[RatioPut] Closed {symbol} | P&L: {pnl:.2f}")
                else:
                    logger.error(f"[RatioPut] Failed to close {symbol}: {resp.get('message')}")
            
            # Remove from positions
            del self.positions[symbol]
        
        # Mark position as inactive to trigger re-entry
        self.position_active = False

    def _exit_all_positions(self, reason):
        """Exit all 5 legs including ITM1 PE."""
        logger.info(f"[RatioPut] Exiting all positions: {reason}")
        order_logger = get_order_logger("ratio_put_orders.csv") if PAPER_TRADING_MODE else None
        
        for symbol, pos_data in list(self.positions.items()):
            close_side = -pos_data["side"]
            
            order_data = {
                "symbol": symbol,
                "qty": pos_data["qty"],
                "side": close_side,
                "type": 2,
                "productType": "INTRADAY",
                "limitPrice": 0,
                "stopPrice": 0,
                "validity": "DAY",
                "disclosedQty": 0,
                "offlineOrder": False,
                "orderTag": "EXIT_ALL",
                "isSliceOrder": False,
            }
            
            if PAPER_TRADING_MODE:
                tick = self.live_data.get(symbol)
                current_ltp = float(tick["ltp"]) if tick and "ltp" in tick else pos_data["entry_price"]
                pnl = (current_ltp - pos_data["entry_price"]) * pos_data["side"] * pos_data["qty"]
                
                order_logger.log_order(
                    symbol=symbol,
                    qty=pos_data["qty"],
                    side=close_side,
                    order_type=2,
                    entry_price=current_ltp,
                    pnl=pnl,
                    order_tag="EXIT_ALL",
                    status="PLACED"
                )
                self._remove_position_symbol(symbol)
                logger.info(f"[PAPER] Exited {symbol} | P&L: {pnl:.2f}")
                
                self.realized_pnl += pnl
            else:
                resp = self.place_order_safe(order_data)
                if resp.get("code") == 0:
                    tick = self.live_data.get(symbol)
                    current_ltp = float(tick["ltp"]) if tick and "ltp" in tick else pos_data["entry_price"]
                    pnl = (current_ltp - pos_data["entry_price"]) * pos_data["side"] * pos_data["qty"]
                    self.realized_pnl += pnl
                    
                    self._remove_position_symbol(symbol)
                    logger.info(f"[RatioPut] Closed {symbol} | P&L: {pnl:.2f}")
                else:
                    logger.error(f"[RatioPut] Failed to close {symbol}: {resp.get('message')}")
        
        self.positions.clear()
        self.position_active = False
        self.itm1_active = False
        
        # Reset all strikes
        self.last_selected = {
            "short_call": None,
            "short_put": None,
            "itm1_put": None,
            "hedge_call": None,
            "hedge_put": None,
        }
        
        # Report cleared positions
        metadata = self._get_metadata()
        self.report_positions(self.positions, metadata=metadata, realized_pnl=self.realized_pnl)

    def _get_current_index_price(self):
        """Get current index price from live data."""
        index = self.trade_ctx.get("index", "NIFTY")
        
        if index == "NIFTY":
            index_symbol = "NSE:NIFTY50-INDEX"
        elif index == "SENSEX":
            index_symbol = "BSE:SENSEX-INDEX"
        else:
            logger.warning(f"[RatioPut] Unknown index type: {index}")
            return None
        
        tick = self.live_data.get(index_symbol)
        if tick and "ltp" in tick:
            try:
                return float(tick["ltp"])
            except (TypeError, ValueError):
                pass
        return None

    def _get_metadata(self):
        """Get metadata for position reporting."""
        current_index = self._get_current_index_price()
        index_movement_pct = 0.0
        
        if current_index and self.last_adjustment_index_price:
            index_movement_pct = ((current_index - self.last_adjustment_index_price) / 
                                 self.last_adjustment_index_price) * 100
        
        itm1_index_move = 0.0
        if self.itm1_active and current_index and self.itm1_entry_index_price:
            itm1_index_move = ((current_index - self.itm1_entry_index_price) / 
                              self.itm1_entry_index_price) * 100
        
        return {
            'adjustment_count': self.adjustment_count,
            'index_movement_pct': index_movement_pct,
            'entry_index': self.entry_index_price,
            'last_adjustment_index': self.last_adjustment_index_price,
            'current_index': current_index,
            'itm1_active': self.itm1_active,
            'itm1_index_move_pct': itm1_index_move,
            'itm1_target_pct': self.itm1_target_index_move_pct
        }

    def stop(self, reason: str):
        """Stop strategy and close all positions."""
        logger.info(f"[RatioPut] Stopping strategy: {reason}")
        if self.positions:
            self._exit_all_positions(f"Strategy stop: {reason}")
