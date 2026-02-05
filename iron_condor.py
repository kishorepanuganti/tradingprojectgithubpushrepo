# strategies/iron_condor.py

import logging
try:
    from .base import BaseStrategy
except Exception:
    from basestrategy import BaseStrategy
from order_logger import get_order_logger
from config.config import IRON_CONDOR_SETTINGS, PAPER_TRADING_MODE

logger = logging.getLogger(__name__)


class IronCondor(BaseStrategy):
    """
    Iron Condor Strategy: Sell OTM Call Spread + Sell OTM Put Spread
    
    Strategy Flow:
    1. Select 4 strikes based on distance from ATM:
       - Short Call: ATM + 2 strikes (OTM)
       - Long Call: ATM + 4 strikes (further OTM)
       - Short Put: ATM - 2 strikes (OTM)
       - Long Put: ATM - 4 strikes (further OTM)
    2. Place 4-leg iron condor order
    3. Monitor combined position P&L
    4. Exit at profit target (30-50% of max profit) or stop loss
    
    Profile:
    - Defined max profit (net premium collected)
    - Defined max loss (spread width - net premium)
    - Benefit from time decay and range-bound markets
    - Lower risk than naked short strangle
    """

    def __init__(self, trade_ctx, live_data, broker=None, qty=0):
        super().__init__(trade_ctx, live_data, broker, qty)
        
        # Get index-specific settings from config
        index = trade_ctx.get("index", "NIFTY")
        settings = IRON_CONDOR_SETTINGS.get(index, IRON_CONDOR_SETTINGS["NIFTY"])

        # Use passed qty if > 0, else fallback to config
        if self.qty == 0:
            self.qty = settings["qty"]
        
        # Position tracking: {symbol: {entry_price, qty, side, leg_type}}
        self.positions = {}
        
        # Last selected strikes
        self.last_selected = {
            "short_call": None,
            "long_call": None,
            "short_put": None,
            "long_put": None,
        }
        
        # Get index-specific targets from config
        self.targets = settings["target_premium"]
        
        # Calculate target profit based on expected credit
        # Expected credit = (2 * short_premium) - (2 * long_premium) for 4-leg Iron Condor
        # Target: 40% of expected credit
        expected_credit = (2 * self.targets["short"] - 2 * self.targets["long"]) * self.qty
        self.target_profit = expected_credit * 0.4  # 40% of max profit
        
        # Adjustment-based strategy (from config)
        # Exit only on: 1) target hit, or 2) max adjustments reached
        self.adjustment_threshold_pct = settings["adjustment_threshold_pct"]
        self.max_adjustments = settings["max_adjustments"]
        self.adjustment_count = 0  # Current adjustment count
        
        # Index tracking for adjustment triggers
        self.entry_index_price = None  # Index price at initial entry
        self.last_adjustment_index_price = None  # Index price at last adjustment (for movement calculation)
        
        self.position_active = False
        
        # Validated P&L tracking
        self.realized_pnl = 0.0

    def start(self):
        """Initialize Iron Condor strategy."""
        index = self.trade_ctx.get("index", "NIFTY")
        logger.info(
            f"[IronCondor] Started for {index} | "
            f"ATM={self.trade_ctx['atm_strike']} | DTE={self.trade_ctx.get('dte', 0)} | "
            f"Adjustment Threshold={self.adjustment_threshold_pct}% | Max Adjustments={self.max_adjustments}"
        )
    
    def _on_position_tick(self, symbol: str, tick: dict):
        """
        REAL-TIME callback - called immediately when position symbol receives tick.
        
        This enables instant target/SL checks (no 5-second delay).
        Orders are executed as soon as target/SL is hit.
        """
        if symbol not in self.positions:
            return
        
        self._check_position_target_sl(tick)
    
    def _check_position_target_sl(self, tick: dict):
        """
        Check combined P&L for target - called on EVERY tick update (real-time).
        Note: NO stop-loss check. Exit only on target or max adjustments.
        
        Args:
            tick: Real-time tick data
        """
        if not self.positions:
            return
        
        # Calculate combined P&L for all 4 legs
        total_pnl = 0
        for symbol, pos_data in self.positions.items():
            entry_price = pos_data["entry_price"]
            side = pos_data["side"]
            qty = pos_data["qty"]
            
            # Get current LTP
            leg_tick = self.live_data.get(symbol)
            if not leg_tick or "ltp" not in leg_tick:
                return  # Skip if any leg has missing data
            
            try:
                current_ltp = float(leg_tick["ltp"])
            except (TypeError, ValueError):
                return
            
            # Update position info
            pos_data["current_ltp"] = current_ltp
            
            # Calculate P&L: (current - entry) * side * qty
            # For SHORT (-1): profit when LTP decreases
            # For LONG (1): profit when LTP increases
            leg_pnl = (current_ltp - entry_price) * side * qty
            pos_data["pnl"] = leg_pnl
            total_pnl += leg_pnl
        
        # Check ONLY target (no stop-loss)
        if total_pnl >= self.target_profit:
            logger.info(f"[IronCondor] TARGET HIT (real-time): Combined P&L={total_pnl:.2f} Rs")
            self._exit_position("target_hit_realtime")


    def on_tick(self):
        """Select strikes and manage iron condor position."""
        # print("[IronCondor] on_tick called", flush=True)
        option_symbols = self.trade_ctx.get("option_symbols", [])
        if not option_symbols:
            logger.warning("[IronCondor] No option symbols available")
            return

        # If position already active, just monitor P&L
        if self.position_active:
            self._monitor_position()
            # Report current positions during monitoring
            if self.positions:
                self.report_positions(self.positions, realized_pnl=self.realized_pnl)
            return

        # Position not active - check if we're ready for entry or re-entry
        if self.adjustment_count > 0:
            logger.info(
                f"[IronCondor] ATTEMPTING RE-ENTRY after adjustment {self.adjustment_count}/{self.max_adjustments}"
            )
        
        # Select 4 strikes using distance-based method
        strikes = self._select_iron_condor_strikes(option_symbols)
        if not strikes:
            self.logger.debug("[IronCondor] Could not select all 4 legs - skipping this tick")
            return
        
        self.logger.debug(f"[IronCondor] Successfully selected strikes: {list(strikes.keys())}")

        short_call_sym, short_call_ltp = strikes["short_call"]
        long_call_sym, long_call_ltp = strikes["long_call"]
        short_put_sym, short_put_ltp = strikes["short_put"]
        long_put_sym, long_put_ltp = strikes["long_put"]

        # Check if strikes have changed
        current = {
            "short_call": short_call_sym,
            "long_call": long_call_sym,
            "short_put": short_put_sym,
            "long_put": long_put_sym,
        }

        if current != self.last_selected:
            entry_type = "RE-ENTRY" if self.adjustment_count > 0 else "INITIAL ENTRY"
            logger.info(
                f"[IronCondor] {entry_type} - Strike change detected: "
                f"Call Spread: SELL {short_call_sym}@{short_call_ltp:.2f} / BUY {long_call_sym}@{long_call_ltp:.2f} | "
                f"Put Spread: SELL {short_put_sym}@{short_put_ltp:.2f} / BUY {long_put_sym}@{long_put_ltp:.2f}"
            )
            print(f"[IronCondor] {entry_type} - Placing 4-leg order: {short_call_sym}, {long_call_sym}, {short_put_sym}, {long_put_sym}", flush=True)
            self.last_selected = current
            
            # Place 4-leg iron condor order
            self._place_iron_condor_orders(strikes)

    def _select_iron_condor_strikes(self, option_symbols):
        """
        Select 4 strikes for iron condor based on step distance from ATM.
        
        For NIFTY (step=50):
        - ATM = 26200
        - Short legs: 2 steps OTM → CE=26300, PE=26100
        - Long legs: 4 steps OTM → CE=26400, PE=26000
        
        For SENSEX (step=100):
        - ATM = 85000
        - Short legs: 2 steps OTM → CE=85200, PE=84800
        - Long legs: 4 steps OTM → CE=85400, PE=84600
        """
        atm_strike = self.trade_ctx.get("atm_strike", 24000)
        index = self.trade_ctx.get("index", "NIFTY")
        step = 50 if index == "NIFTY" else 100
        
        # Get OTM distances from config
        settings = IRON_CONDOR_SETTINGS.get(index, IRON_CONDOR_SETTINGS["NIFTY"])
        short_otm_steps = settings["short_otm_steps"]  # From config (3 steps)
        long_otm_steps = settings["long_otm_steps"]    # From config (8 steps)
        
        # Debug: show how many symbols we have
        ce_count = sum(1 for s in option_symbols if 'CE' in s)
        pe_count = sum(1 for s in option_symbols if 'PE' in s)
        self.logger.debug(
            f"[IronCondor] Total option symbols available: {len(option_symbols)} "
            f"(CE={ce_count}, PE={pe_count})"
        )
        
        # Show a few sample symbols for debugging
        sample_ce = [s for s in option_symbols if 'CE' in s][:3]
        sample_pe = [s for s in option_symbols if 'PE' in s][:3]
        self.logger.debug(f"[IronCondor] Sample CE symbols: {sample_ce}")
        self.logger.debug(f"[IronCondor] Sample PE symbols: {sample_pe}")
        
        # Calculate strike prices
        short_call_strike = atm_strike + (short_otm_steps * step)
        long_call_strike = atm_strike + (long_otm_steps * step)
        short_put_strike = atm_strike - (short_otm_steps * step)
        long_put_strike = atm_strike - (long_otm_steps * step)
        
        self.logger.debug(
            f"[IronCondor] Calculated strikes: "
            f"SC={short_call_strike}, LC={long_call_strike}, "
            f"SP={short_put_strike}, LP={long_put_strike}"
        )
        
        # Find symbols matching these exact strikes (with fallback to nearest)
        short_call_sym = self._find_symbol_by_strike_with_fallback(
            option_symbols, short_call_strike, "CE", max_distance=step
        )
        long_call_sym = self._find_symbol_by_strike_with_fallback(
            option_symbols, long_call_strike, "CE", max_distance=step*2
        )
        short_put_sym = self._find_symbol_by_strike_with_fallback(
            option_symbols, short_put_strike, "PE", max_distance=step
        )
        long_put_sym = self._find_symbol_by_strike_with_fallback(
            option_symbols, long_put_strike, "PE", max_distance=step*2
        )
        
        if not all([short_call_sym, long_call_sym, short_put_sym, long_put_sym]):
            missing = []
            if not short_call_sym: missing.append(f"SC@{short_call_strike}")
            if not long_call_sym: missing.append(f"LC@{long_call_strike}")
            if not short_put_sym: missing.append(f"SP@{short_put_strike}")
            if not long_put_sym: missing.append(f"LP@{long_put_strike}")
            
            self.logger.warning(
                f"[IronCondor] Could not find symbols for strikes: {', '.join(missing)}"
            )
            
            # Show what strikes ARE available
            self._debug_available_strikes(option_symbols, atm_strike)
            
            return None
        
        # Get LTPs for the selected symbols
        self.logger.debug(
            f"[IronCondor] Fetching LTPs for symbols: "
            f"SC={short_call_sym}, LC={long_call_sym}, SP={short_put_sym}, LP={long_put_sym}"
        )
        
        short_call_ltp = self._get_ltp(short_call_sym)
        long_call_ltp = self._get_ltp(long_call_sym)
        short_put_ltp = self._get_ltp(short_put_sym)
        long_put_ltp = self._get_ltp(long_put_sym)
        
        # Check which LTPs are missing
        missing_ltps = []
        if short_call_ltp is None:
            missing_ltps.append(f"SC={short_call_sym}")
        if long_call_ltp is None:
            missing_ltps.append(f"LC={long_call_sym}")
        if short_put_ltp is None:
            missing_ltps.append(f"SP={short_put_sym}")
        if long_put_ltp is None:
            missing_ltps.append(f"LP={long_put_sym}")
        
        if missing_ltps:
            self.logger.warning(
                f"[IronCondor] Missing LTP data for: {', '.join(missing_ltps)}. "
                f"Will retry on next tick."
            )
            return None
        
        self.logger.debug(
            f"[IronCondor] Selected strikes with LTPs: "
            f"SC={short_call_sym}@{short_call_ltp:.2f}, "
            f"LC={long_call_sym}@{long_call_ltp:.2f}, "
            f"SP={short_put_sym}@{short_put_ltp:.2f}, "
            f"LP={long_put_sym}@{long_put_ltp:.2f}"
        )
        
        return {
            "short_call": (short_call_sym, short_call_ltp),
            "long_call": (long_call_sym, long_call_ltp),
            "short_put": (short_put_sym, short_put_ltp),
            "long_put": (long_put_sym, long_put_ltp),
        }
    
    def _find_symbol_by_strike(self, option_symbols, target_strike, option_type):
        """
        Find symbol matching exact strike price and option type.
        
        Args:
            option_symbols: List of all available option symbols
            target_strike: Target strike price (e.g., 26450)
            option_type: "CE" or "PE"
        
        Returns:
            Symbol string or None if not found
        """
        for symbol in option_symbols:
            # Check if symbol ends with CE or PE (not just contains)
            if not symbol.endswith(option_type):
                continue
            
            strike = self._extract_strike(symbol)
            if strike == target_strike:
                self.logger.debug(f"[IronCondor] Found {symbol} for strike {target_strike}")
                return symbol
        
        # If not found, log what we searched
        self.logger.warning(
            f"[IronCondor] Could not find {option_type} strike {target_strike} in {len(option_symbols)} symbols"
        )
        return None
    
    def _find_symbol_by_strike_with_fallback(self, option_symbols, target_strike, option_type, max_distance=100):
        """
        Find symbol matching exact strike, or nearest if exact doesn't exist.
        
        Args:
            option_symbols: List of all available option symbols
            target_strike: Target strike price (e.g., 26300)
            option_type: "CE" or "PE"
            max_distance: Maximum acceptable distance from target strike
        
        Returns:
            Symbol string or None if nothing found within tolerance
        """
        # First try exact match
        exact_match = self._find_symbol_by_strike(option_symbols, target_strike, option_type)
        if exact_match:
            return exact_match
        
        # Fallback: find nearest strike within max_distance
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
            self.logger.warning(
                f"[IronCondor] No {option_type} strikes found within {max_distance} of {target_strike}"
            )
            return None
        
        # Sort by distance and pick closest
        candidates.sort()
        distance, actual_strike, symbol = candidates[0]
        
        if distance > 0:
            self.logger.info(
                f"[IronCondor] Using fallback: {symbol} (strike {actual_strike}) "
                f"instead of exact {target_strike} (distance: {distance})"
            )
        
        return symbol
    
    def _get_ltp(self, symbol):
        """Get LTP for a symbol from live data."""
        tick = self.live_data.get(symbol)
        
        if tick:
            self.logger.debug(f"[IronCondor] Tick data for {symbol}: {tick}")
            if "ltp" in tick:
                try:
                    ltp = float(tick["ltp"])
                    self.logger.debug(f"[IronCondor] LTP for {symbol}: {ltp}")
                    return ltp
                except (TypeError, ValueError) as e:
                    self.logger.warning(f"[IronCondor] Failed to parse LTP for {symbol}: {e}")
            else:
                self.logger.warning(f"[IronCondor] Tick exists but no 'ltp' key for {symbol}. Keys: {list(tick.keys())}")
        else:
            # Try to show what symbols ARE in live_data for debugging
            try:
                snapshot = self.live_data.get_snapshot() if hasattr(self.live_data, 'get_snapshot') else {}
                available_symbols = list(snapshot.keys())[:10]  # First 10 for brevity
                self.logger.warning(
                    f"[IronCondor] No tick data for {symbol}. "
                    f"Live data has {len(snapshot)} symbols. Sample: {available_symbols}"
                )
            except:
                self.logger.warning(f"[IronCondor] No tick data for {symbol}")
        
        return None

    def _select_strike_near_atm(self, candidates, atm, side):
        """DEPRECATED - Kept for compatibility but not used."""
        pass

    def _select_strike_far_from_atm(self, candidates, atm, side, ref_strike):
        """DEPRECATED - Kept for compatibility but not used."""
        pass

    def _extract_strike(self, symbol):
        """
        Extract strike price from symbol string.
        
        Symbol format examples:
        - NSE:NIFTY2610626500CE → strike = 26500 (not 2610626500!)
        - NSE:NIFTY2610625950PE → strike = 25950
        - BSE:SENSEX26106850000PE → strike = 85000
        
        The symbol contains: exchange:index + expiry(YYMMDD) + strike + optionType
        For NIFTY: strikes are typically 5 digits (20000-30000)
        For SENSEX: strikes are typically 5-6 digits (70000-100000)
        """
        try:
            import re
            
            # First, strip the exchange prefix and option type suffix
            # Symbol format: NSE:NIFTY2610626500CE
            # We need to extract just the strike (26500)
            
            # For NIFTY: look for 5-digit number before CE/PE that's in valid range
            # Pattern: look for strike that makes sense (NIFTY: 20000-35000, SENSEX: 60000-110000)
            
            if "NIFTY" in symbol:
                # NIFTY strikes are typically 5 digits: 20000-35000
                # Symbol: NSE:NIFTY2610626500CE
                # The number before CE/PE is: 2610626500
                # We need the last 5 digits that represent a valid strike
                match = re.search(r'(\d+)(CE|PE)$', symbol)
                if match:
                    full_num = match.group(1)
                    # For NIFTY, try extracting last 5 digits
                    if len(full_num) >= 5:
                        strike = int(full_num[-5:])
                        # Validate it's in reasonable NIFTY range
                        if 15000 <= strike <= 40000:
                            self.logger.debug(f"[IronCondor] Extracted NIFTY strike {strike} from {symbol}")
                            return strike
                    # Also try last 4 digits for lower strikes
                    if len(full_num) >= 4:
                        strike = int(full_num[-4:])
                        if 1000 <= strike <= 9999:
                            self.logger.debug(f"[IronCondor] Extracted NIFTY strike {strike} from {symbol}")
                            return strike
            
            elif "SENSEX" in symbol:
                # SENSEX strikes are typically 5-6 digits: 70000-110000
                match = re.search(r'(\d+)(CE|PE)$', symbol)
                if match:
                    full_num = match.group(1)
                    # For SENSEX, try extracting last 5-6 digits
                    if len(full_num) >= 6:
                        strike = int(full_num[-6:])
                        if 50000 <= strike <= 150000:
                            self.logger.debug(f"[IronCondor] Extracted SENSEX strike {strike} from {symbol}")
                            return strike
                    if len(full_num) >= 5:
                        strike = int(full_num[-5:])
                        if 50000 <= strike <= 99999:
                            self.logger.debug(f"[IronCondor] Extracted SENSEX strike {strike} from {symbol}")
                            return strike
            
            self.logger.warning(f"[IronCondor] Could not extract strike from symbol: {symbol}")
            return None
        except (ValueError, IndexError) as e:
            self.logger.warning(f"[IronCondor] Error extracting strike from {symbol}: {e}")
            return None
    
    def _debug_available_strikes(self, option_symbols, atm_strike):
        """Show available strikes near ATM for debugging."""
        self.logger.info("[IronCondor] === AVAILABLE STRIKES NEAR ATM ===")
        
        # Extract all CE strikes
        ce_strikes = []
        for sym in option_symbols:
            if sym.endswith("CE"):
                strike = self._extract_strike(sym)
                if strike and abs(strike - atm_strike) <= 500:  # Within 500 points of ATM
                    ce_strikes.append((strike, sym))
        
        # Extract all PE strikes
        pe_strikes = []
        for sym in option_symbols:
            if sym.endswith("PE"):
                strike = self._extract_strike(sym)
                if strike and abs(strike - atm_strike) <= 500:  # Within 500 points of ATM
                    pe_strikes.append((strike, sym))
        
        # Sort and display
        ce_strikes.sort()
        pe_strikes.sort()
        
        self.logger.info(f"[IronCondor] ATM Strike: {atm_strike}")
        self.logger.info(f"[IronCondor] Available CE strikes near ATM ({len(ce_strikes)} total):")
        for strike, sym in ce_strikes[:10]:  # Show first 10
            marker = " ← TARGET" if strike in [atm_strike + 150, atm_strike + 400] else ""
            self.logger.info(f"  {strike}: {sym}{marker}")
        
        self.logger.info(f"[IronCondor] Available PE strikes near ATM ({len(pe_strikes)} total):")
        for strike, sym in pe_strikes[:10]:  # Show first 10
            marker = " ← TARGET" if strike in [atm_strike - 150, atm_strike - 400] else ""
            self.logger.info(f"  {strike}: {sym}{marker}")

    def _place_iron_condor_orders(self, strikes):
        """Place all 4 legs of iron condor."""
        order_logger = get_order_logger("iron_condor_orders.csv") if PAPER_TRADING_MODE else None
        
        legs = [
            ("short_call", strikes["short_call"], -1, "SHORT_CALL"),
            ("long_call", strikes["long_call"], 1, "LONG_CALL"),
            ("short_put", strikes["short_put"], -1, "SHORT_PUT"),
            ("long_put", strikes["long_put"], 1, "LONG_PUT"),
        ]
        
        for leg_name, (symbol, ltp), side, tag in legs:
            order_data = {
                "symbol": symbol,
                "qty": self.qty,
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
                    qty=self.qty,
                    side=side,
                    order_type=2,
                    entry_price=ltp,
                    order_tag=tag,
                    status="PLACED"
                )
                self.positions[symbol] = {
                    "entry_price": ltp,
                    "qty": self.qty,
                    "side": side,
                    "leg": leg_name
                }
                self._add_position_symbol(symbol)  # Register for real-time monitoring

                self.logger.info(f"[PAPER] Logged {tag}: {symbol} @ {ltp} | Real-time monitoring ENABLED")
            else:
                resp = self.place_order_safe(order_data)
                if resp.get("code") == 0:
                    self.positions[symbol] = {
                        "entry_price": ltp,
                        "qty": self.qty,
                        "side": side,
                        "leg": leg_name
                    }
                    self._add_position_symbol(symbol)  # Register for real-time monitoring

                    self.logger.info(f"[IronCondor] Placed {tag}: {symbol} @ {ltp} | Real-time monitoring ENABLED")
                else:
                    self.logger.warning(f"[IronCondor] Failed to place {tag}: {resp.get('message')}")
        
        self.position_active = True
        
        # Capture index price for adjustment tracking
        current_index = self._get_current_index_price()
        if self.entry_index_price is None:
            # First entry - capture initial index price
            self.entry_index_price = current_index
            self.last_adjustment_index_price = current_index
            logger.info(
                f"[IronCondor] Initial entry established | Index: {current_index:.2f} | "
                f"Adjustments: {self.adjustment_count}/{self.max_adjustments}"
            )
        else:
            # Re-entry after adjustment
            self.last_adjustment_index_price = current_index
            logger.info(
                f"[IronCondor] Re-entry after adjustment {self.adjustment_count} | "
                f"Index: {current_index:.2f} | Adjustments: {self.adjustment_count}/{self.max_adjustments}"
            )
        
        # Report positions to tracker with metadata
        metadata = {
            'adjustment_count': self.adjustment_count,
            'max_adjustments': self.max_adjustments,
            'index_movement_pct': 0.0,  # Will be updated in monitoring
            'entry_index': self.entry_index_price,
            'last_adjustment_index': self.last_adjustment_index_price
        }
        self.report_positions(self.positions, metadata=metadata, realized_pnl=self.realized_pnl)


    def _monitor_position(self):
        """Monitor combined P&L and check for index movement adjustment triggers."""
        if not self.positions:
            return
        
        # Calculate combined P&L for tracking (same as before)
        total_pnl = 0.0
        for symbol, pos_data in self.positions.items():
            tick = self.live_data.get(symbol)
            if tick and "ltp" in tick:
                try:
                    current_ltp = float(tick["ltp"])
                    pos_data["current_ltp"] = current_ltp
                    
                    # P&L calculation: (current - entry) * side * qty
                    pnl = (current_ltp - pos_data["entry_price"]) * pos_data["side"] * pos_data["qty"]
                    pos_data["pnl"] = pnl
                    total_pnl += pnl
                except (TypeError, ValueError):
                    pass
        
        logger.debug(f"[IronCondor] Combined P&L: {total_pnl:.2f}")
        
        # Check index movement for adjustment trigger
        current_index_price = self._get_current_index_price()
        if current_index_price is None or self.last_adjustment_index_price is None:
            logger.debug("[IronCondor] Waiting for index price data...")
            return
        
        # Calculate index movement percentage from last adjustment
        index_movement_pct = ((current_index_price - self.last_adjustment_index_price) / self.last_adjustment_index_price) * 100
        
        logger.debug(
            f"[IronCondor] Index Movement: {index_movement_pct:.3f}% | "
            f"Current: {current_index_price:.2f} | Last Adjustment: {self.last_adjustment_index_price:.2f} | "
            f"Adjustments: {self.adjustment_count}/{self.max_adjustments}"
        )
        
        # Check if adjustment threshold is crossed
        if abs(index_movement_pct) >= self.adjustment_threshold_pct:
            if self.adjustment_count < self.max_adjustments:
                logger.info(
                    f"[IronCondor] ADJUSTMENT TRIGGER: Index moved {index_movement_pct:.3f}% | "
                    f"Threshold: ±{self.adjustment_threshold_pct}%"
                )
                self._adjust_position(current_index_price, index_movement_pct)
            else:
                logger.warning(
                    f"[IronCondor] MAX ADJUSTMENTS REACHED ({self.max_adjustments}) | "
                    f"Closing all positions"
                )
                self._exit_position("Max adjustments reached")
        
        # Report metadata to positions tracker for MTM display
        if self.positions:
            metadata = {
                'adjustment_count': self.adjustment_count,
                'max_adjustments': self.max_adjustments,
                'index_movement_pct': index_movement_pct if current_index_price and self.last_adjustment_index_price else 0.0,
                'entry_index': self.entry_index_price,
                'last_adjustment_index': self.last_adjustment_index_price,
                'current_index': current_index_price
            }
            self.report_positions(self.positions, metadata=metadata, realized_pnl=self.realized_pnl)

    def _exit_position(self, reason):
        """Close all 4 legs."""
        logger.info(f"[IronCondor] Exiting position: {reason}")
        order_logger = get_order_logger("iron_condor_orders.csv") if PAPER_TRADING_MODE else None
        
        for symbol, pos_data in list(self.positions.items()):
            # Reverse the side to close
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
                "orderTag": "EXIT_IC",
                "isSliceOrder": False,
            }
            
            if PAPER_TRADING_MODE:
                # Get current LTP for logging
                tick = self.live_data.get(symbol)
                current_ltp = float(tick["ltp"]) if tick and "ltp" in tick else pos_data.get("entry_price", 0)
                
                # Calculate P&L: (Current - Entry) * Side * Qty
                pnl = (current_ltp - pos_data["entry_price"]) * pos_data["side"] * pos_data["qty"]
                
                order_logger.log_order(
                    symbol=symbol,
                    qty=pos_data["qty"],
                    side=close_side,
                    order_type=2,
                    entry_price=current_ltp,
                    pnl=pnl,
                    order_tag="EXIT_IC",
                    status="PLACED"
                )
                self._remove_position_symbol(symbol)  # Unregister callback

                logger.info(f"[PAPER] Logged exit for {symbol} | P&L: {pnl:.2f} | Real-time monitoring DISABLED")
                
                # Update Realized P&L
                self.realized_pnl += pnl
            else:
                resp = self.place_order_safe(order_data)
                if resp.get("code") == 0:
                    # Calculate realized P&L for this leg
                    tick = self.live_data.get(symbol)
                    current_ltp = float(tick["ltp"]) if tick and "ltp" in tick else pos_data["entry_price"]
                    
                    # P&L calculation: (current - entry) * side * qty
                    # For Short (-1): (Entry - Current) * Qty = (Current - Entry) * -1 * Qty
                    leg_pnl = (current_ltp - pos_data["entry_price"]) * pos_data["side"] * pos_data["qty"]
                    self.realized_pnl += leg_pnl
                    
                    self._remove_position_symbol(symbol)  # Unregister callback

                    logger.info(f"[IronCondor] Closed {symbol} | P&L: {leg_pnl:.2f} | Real-time monitoring DISABLED")
                else:
                    logger.error(f"[IronCondor] Failed to close {symbol}: {resp.get('message')}")
        
        self.positions.clear()
        self.position_active = False
        
        # CRITICAL: Reset last_selected strikes to force fresh strike selection on re-entry
        # Without this, the strategy won't re-enter after adjustment because it thinks
        # strikes haven't changed
        self.last_selected = {
            "short_call": None,
            "long_call": None,
            "short_put": None,
            "long_put": None,
        }
        
        logger.info(f"[IronCondor] Position closed: {reason} | Strikes reset for re-entry")
        
        # Report cleared positions (empty) with metadata
        metadata = {
            'adjustment_count': self.adjustment_count,
            'max_adjustments': self.max_adjustments,
            'index_movement_pct': 0.0,
            'entry_index': self.entry_index_price,
            'last_adjustment_index': self.last_adjustment_index_price
        }
        self.report_positions(self.positions, metadata=metadata, realized_pnl=self.realized_pnl)
    
    def _get_current_index_price(self):
        """Get current index price from live data."""
        # Get the underlying index symbol from trade context
        index = self.trade_ctx.get("index", "NIFTY")
        
        # Construct index symbol based on index type
        if index == "NIFTY":
            index_symbol = "NSE:NIFTY50-INDEX"
        elif index == "SENSEX":
            index_symbol = "BSE:SENSEX-INDEX"
        else:
            logger.warning(f"[IronCondor] Unknown index type: {index}")
            return None
        
        # Try to get from live data
        tick = self.live_data.get(index_symbol)
        if tick and "ltp" in tick:
            try:
                return float(tick["ltp"])
            except (TypeError, ValueError) as e:
                logger.warning(f"[IronCondor] Failed to parse index LTP: {e}")
                return None
        
        # Fallback: try to get from trade_ctx (underlying_ltp)
        underlying_ltp = self.trade_ctx.get("underlying_ltp")
        if underlying_ltp:
            try:
                return float(underlying_ltp)
            except (TypeError, ValueError):
                pass
        
        logger.debug(f"[IronCondor] No index price data available for {index_symbol}")
        return None
    
    def _adjust_position(self, current_index_price, index_movement_pct):
        """
        Adjust position: Square off all current positions and prepare for re-entry.
        
        Args:
            current_index_price: Current index price that triggered the adjustment
            index_movement_pct: Percentage movement from last adjustment
        """
        logger.info(
            f"[IronCondor] === ADJUSTMENT {self.adjustment_count + 1}/{self.max_adjustments} TRIGGERED === | "
            f"Index Movement: {index_movement_pct:.3f}% | "
            f"Current Index: {current_index_price:.2f} | "
            f"Last Adjustment Index: {self.last_adjustment_index_price:.2f}"
        )
        
        # Square off all current positions
        self._exit_position(f"Adjustment {self.adjustment_count + 1}")
        
        # Increment adjustment counter
        self.adjustment_count += 1
        
        # Position is now inactive, on next tick it will re-enter with new strikes
        logger.info(
            f"[IronCondor] Adjustment {self.adjustment_count} complete | "
            f"Remaining adjustments: {self.max_adjustments - self.adjustment_count} | "
            f"Ready for re-entry on next tick"
        )


    def stop(self, reason: str):
        """Close all Iron Condor positions."""
        logger.info(f"[IronCondor] Stopping: {reason}")
        if self.position_active:
            self._exit_position(reason)
