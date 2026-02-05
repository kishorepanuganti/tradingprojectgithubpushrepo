"""
Live Positions Tracker and MTM (Mark-to-Market) P&L Calculator

This module provides centralized position tracking and real-time P&L monitoring
across all strategies. It aggregates positions from multiple strategies and
calculates combined MTM P&L.

Features:
- Track positions across all strategies
- Calculate real-time MTM P&L
- Display formatted position summary
- Portfolio-level P&L aggregation
"""

import logging
import threading
from datetime import datetime
from typing import Dict, List, Any, Optional
from collections import defaultdict

# Import Greeks calculator
try:
    from greeks_calculator import calculate_greeks, parse_symbol_info
    GREEKS_AVAILABLE = True
except ImportError:
    GREEKS_AVAILABLE = False

logger = logging.getLogger(__name__)

# Log warning about Greeks availability after logger is defined
if not GREEKS_AVAILABLE:
    logger.warning("Greeks calculator not available - Greeks will not be displayed")


class PositionsTracker:
    """
    Centralized tracker for all open positions across strategies.
    Calculates and displays MTM P&L in real-time.
    """

    def __init__(self, live_data, trade_context: Optional[Dict] = None):
        """
        Initialize positions tracker.
        
        Args:
            live_data: LiveDataStore instance for fetching current LTPs
            trade_context: Optional trade context with underlying info for Greeks
        """
        self.live_data = live_data
        self.trade_context = trade_context or {}
        self.lock = threading.RLock()
        
        # Positions by strategy: {strategy_name: {symbol: position_data}}
        self.positions_by_strategy: Dict[str, Dict[str, Dict]] = {}
        
        # Strategy metadata: {strategy_name: {custom_fields}}
        self.strategy_metadata: Dict[str, Dict[str, Any]] = {}
        
        # Realized P&L by strategy
        self.realized_pnl: Dict[str, float] = defaultdict(float)
        
        # Overall portfolio metrics
        self.portfolio_pnl = 0.0
        self.total_positions = 0
        
    def register_strategy(self, strategy_name: str):
        """Register a strategy for position tracking."""
        with self.lock:
            if strategy_name not in self.positions_by_strategy:
                self.positions_by_strategy[strategy_name] = {}
                logger.info(f"[PositionsTracker] Registered strategy: {strategy_name}")
    
    def update_positions(self, strategy_name: str, positions: Dict[str, Dict], metadata: Optional[Dict[str, Any]] = None, realized_pnl: Optional[float] = None):
        """
        Update positions for a specific strategy.
        
        Args:
            strategy_name: Name of the strategy
            positions: Dict of {symbol: {entry_price, qty, side, leg}}
            metadata: Optional dict with strategy-specific info
            realized_pnl: Optional total realized P&L for this strategy
        """
        with self.lock:
            if strategy_name not in self.positions_by_strategy:
                self.register_strategy(strategy_name)
            
            self.positions_by_strategy[strategy_name] = positions.copy()
            
            # Store metadata if provided
            # Store metadata if provided
            if metadata:
                self.strategy_metadata[strategy_name] = metadata.copy()
            
            # Update realized P&L if provided
            if realized_pnl is not None:
                self.realized_pnl[strategy_name] = realized_pnl
    
    def get_positions(self, strategy_name: Optional[str] = None) -> Dict:
        """
        Get positions for a specific strategy or all strategies.
        
        Args:
            strategy_name: Strategy name (None = all strategies)
        
        Returns:
            Dict of positions
        """
        with self.lock:
            if strategy_name:
                return self.positions_by_strategy.get(strategy_name, {})
            return self.positions_by_strategy.copy()
    
    def calculate_mtm(self) -> Dict[str, Any]:
        """
        Calculate mark-to-market P&L for all positions.
        """
    def calculate_mtm(self) -> Dict[str, Any]:
        """
        Calculate mark-to-market P&L for all positions.
        """
        # print("[PositionsTracker] Starting calculate_mtm...", flush=True)
        with self.lock:
            # print("[PositionsTracker] Acquired lock...", flush=True)
            total_pnl = 0.0
            total_realized = 0.0
            total_unrealized = 0.0
            strategy_pnl = {}
            total_positions_count = 0
            position_details = []
            
            for strategy_name, positions in self.positions_by_strategy.items():
                strategy_total = 0.0
                strategy_positions_count = len(positions)
                
                for symbol, pos_data in positions.items():
                    # Get current LTP
                    tick = self.live_data.get(symbol)
                    if not tick or "ltp" not in tick:
                         # Still display position even if live data missing
                         current_ltp = pos_data.get("entry_price", 0)  # Default to entry price so PnL is 0
                    else:
                        try:
                            current_ltp = float(tick["ltp"])
                        except (TypeError, ValueError):
                            current_ltp = pos_data.get("entry_price", 0)

                    try:
                        entry_price = pos_data.get("entry_price", 0)
                        qty = pos_data.get("qty", 0)
                        side = pos_data.get("side", 1)
                        
                        # Calculate P&L
                        pnl = (current_ltp - entry_price) * side * qty
                        
                        strategy_total += pnl
                        
                        # Calculate Greeks if available
                        greeks_data = self._calculate_position_greeks(
                            symbol, current_ltp, pos_data
                        )
                        
                        # Store position details
                        position_details.append({
                            'strategy': strategy_name,
                            'symbol': symbol,
                            'entry_price': entry_price,
                            'current_ltp': current_ltp,
                            'qty': qty,
                            'side': side,
                            'pnl': pnl,
                            'leg': pos_data.get('leg', 'UNKNOWN'),
                            'greeks': greeks_data  # Add Greeks data
                        })
                    except (TypeError, ValueError) as e:
                        logger.debug(f"Error calculating P&L for {symbol}: {e}")
                        continue
                    except (TypeError, ValueError) as e:
                        logger.debug(f"Error calculating P&L for {symbol}: {e}")
                        continue
                
                # Get realized P&L for this strategy
                realized = self.realized_pnl.get(strategy_name, 0.0)
                
                strategy_pnl[strategy_name] = {
                    'realized': realized,
                    'unrealized': strategy_total,
                    'total': realized + strategy_total
                }
                
                total_realized += realized
                total_unrealized += strategy_total
                total_pnl += (realized + strategy_total)
                total_positions_count += strategy_positions_count
            
            self.portfolio_pnl = total_pnl
            self.total_positions = total_positions_count
            
            # print("[PositionsTracker] Finished calculation, releasing lock.", flush=True)
            return {
                'total_pnl': total_pnl,
                'total_realized': total_realized,
                'total_unrealized': total_unrealized,
                'strategy_pnl': strategy_pnl,
                'positions_count': total_positions_count,
                'timestamp': datetime.now().isoformat(),
                'position_details': position_details
            }
    
    def display_mtm(self) -> str:
        """
        Generate formatted MTM display string.
        
        Returns:
            Formatted string showing positions and P&L
        """
        mtm_data = self.calculate_mtm()
        
        lines = []
        lines.append("\n" + "=" * 80)
        lines.append(f"{'LIVE POSITIONS & MTM P&L':^80}")
        lines.append(f"{'Time: ' + datetime.now().strftime('%Y-%m-%d %H:%M:%S'):^80}")
        lines.append("=" * 80)
        
        # Overall summary
        lines.append(f"\n{'PORTFOLIO SUMMARY':^80}")
        lines.append("-" * 80)
        lines.append(f"Total Positions: {mtm_data['positions_count']}")
        lines.append(f"Total MTM P&L: ₹{mtm_data['total_pnl']:,.2f}")
        lines.append("-" * 80)
        
        # Strategy-wise P&L with metadata
        if mtm_data['strategy_pnl']:
            lines.append(f"\n{'STRATEGY-WISE P&L':^80}")
            lines.append("-" * 80)
            lines.append(f"{'Strategy':<30} {'Realized':>12} {'Unrealized':>12} {'Total':>12} {'Info':<10}")
            lines.append("-" * 80)
            
            for strategy, pnl_data in mtm_data['strategy_pnl'].items():
                realized = pnl_data['realized']
                unrealized = pnl_data['unrealized']
                total = pnl_data['total']
                
                realized_str = f"₹{realized:,.0f}"
                unrealized_str = f"₹{unrealized:,.0f}"
                total_str = f"₹{total:,.0f}"
                
                # Add metadata info if available
                metadata_str = ""
                if strategy in self.strategy_metadata:
                    meta = self.strategy_metadata[strategy]
                    parts = []
                    if 'index_movement_pct' in meta:
                        parts.append(f"Δ Index: {meta['index_movement_pct']:+.3f}%")
                    if 'adjustment_count' in meta and 'max_adjustments' in meta:
                        parts.append(f"Adj: {meta['adjustment_count']}/{meta['max_adjustments']}")
                    if parts:
                        metadata_str = f" | {' | '.join(parts)}"
                
                    if parts:
                        metadata_str = f" | {' | '.join(parts)}"
                
                lines.append(f"{strategy:<30} {realized_str:>12} {unrealized_str:>12} {total_str:>12} {metadata_str}")
            lines.append("-" * 80)
        
        # Position details with Greeks
        if mtm_data['position_details']:
            lines.append(f"\n{'POSITION DETAILS WITH GREEKS':^120}")
            lines.append("-" * 120)
            lines.append(f"{'Strategy':<18} {'Leg':<8} {'Symbol':<18} {'Qty':<6} {'Entry':<8} {'LTP':<8} {'P&L':<12} {'Delta':<8} {'Gamma':<8} {'IV%':<8}")
            lines.append("-" * 120)
            
            for pos in mtm_data['position_details']:
                strategy_short = pos['strategy'][:16]
                leg = pos['leg'][:6]
                symbol_short = pos['symbol'].split(':')[-1][:16] if ':' in pos['symbol'] else pos['symbol'][:16]
                qty = f"{'+' if pos['side'] > 0 else '-'}{pos['qty']}"
                entry = f"₹{pos['entry_price']:.0f}"
                ltp = f"₹{pos['current_ltp']:.0f}"
                pnl = f"₹{pos['pnl']:,.0f}"
                
                # Format Greeks
                greeks = pos.get('greeks', {})
                if greeks:
                    delta_str = f"{greeks.get('delta', 0):.3f}"
                    gamma_str = f"{greeks.get('gamma', 0):.4f}"
                    iv_str = f"{greeks.get('iv', 0):.1f}%"
                else:
                    delta_str = "N/A"
                    gamma_str = "N/A"
                    iv_str = "N/A"
                
                lines.append(f"{strategy_short:<18} {leg:<8} {symbol_short:<18} {qty:<6} {entry:<8} {ltp:<8} {pnl:<12} {delta_str:<8} {gamma_str:<8} {iv_str:<8}")
        
        lines.append("=" * 120 + "\n")
        
        return "\n".join(lines)
    
    def _calculate_position_greeks(self, symbol: str, current_ltp: float, pos_data: Dict) -> Optional[Dict[str, float]]:
        """
        Calculate Greeks for a single position.
        
        Args:
            symbol: Option symbol
            current_ltp: Current LTP
            pos_data: Position data dictionary
        
        Returns:
            Greeks dict or None if calculation fails
        """
        if not GREEKS_AVAILABLE:
            return None
        
        try:
            # Parse symbol to get strike and option type
            parsed = parse_symbol_info(symbol)
            if not parsed:
                return None
            
            strike, option_type = parsed
            
            # Get spot price from trade context
            spot_price = None
            if 'underlying_ltp' in self.trade_context:
                spot_price = self.trade_context['underlying_ltp']
            elif 'underlying_symbol' in self.trade_context:
                underlying_tick = self.live_data.get(self.trade_context['underlying_symbol'])
                if underlying_tick and 'ltp' in underlying_tick:
                    spot_price = float(underlying_tick['ltp'])
            
            if not spot_price:
                return None
            
            # Get DTE from trade context
            dte = self.trade_context.get('dte', 1)  # Default to 1 if not available
            
            # Calculate Greeks
            greeks = calculate_greeks(
                option_price=current_ltp,
                spot=spot_price,
                strike=strike,
                days_to_expiry=dte,
                option_type=option_type
            )
            
            return greeks
        
        except Exception as e:
            logger.debug(f"Failed to calculate Greeks for {symbol}: {e}")
            return None
    
    def print_mtm(self):
        """Print MTM display to console."""
        display = self.display_mtm()
        print(display, flush=True)
        logger.info(f"[MTM] Total P&L: ₹{self.portfolio_pnl:,.2f} | Positions: {self.total_positions}")
    
    def save_positions_to_file(self, filename="positions_state.csv"):
        """
        Save current positions and realized P&L to CSV for persistence across restarts.
        Includes a date header to prevent loading stale data on next day.
        """
        import csv
        with self.lock:
            try:
                with open(filename, 'w', newline='') as f:
                    writer = csv.writer(f)
                    
                    # Header: Metadata (Date)
                    writer.writerow(['#METADATA', datetime.now().strftime("%Y-%m-%d")])
                    
                    # Section 1: Realized P&L
                    writer.writerow(['#REALIZED_PNL'])
                    writer.writerow(['strategy', 'realized_pnl'])
                    for strategy_name, pnl in self.realized_pnl.items():
                        writer.writerow([strategy_name, pnl])
                        
                    # Section 2: Positions
                    writer.writerow(['#POSITIONS'])
                    writer.writerow(['strategy', 'symbol', 'qty', 'side', 'entry_price', 'leg'])
                    
                    for strategy_name, positions in self.positions_by_strategy.items():
                        for symbol, pos_data in positions.items():
                            writer.writerow([
                                strategy_name,
                                symbol,
                                pos_data.get('qty', 0),
                                pos_data.get('side', 1),
                                pos_data.get('entry_price', 0),
                                pos_data.get('leg', '')
                            ])
                            
                logger.debug(f"[PositionsTracker] Saved positions & P&L to {filename}")
                return True
            except Exception as e:
                logger.error(f"[PositionsTracker] Failed to save positions: {e}")
                return False
    
    def load_positions_from_file(self, filename="positions_state.csv"):
        """
        Load positions and realized P&L from CSV on startup.
        Checks for stale date (yesterday's data) and archives if needed.
        """
        import csv
        import os
        import shutil
        
        if not os.path.exists(filename):
            logger.info(f"[PositionsTracker] No positions file found ({filename}), starting fresh")
            return False
            
        # Check for stale data (file modified date vs today)
        try:
             # Just use file content date check below, simpler and more robust
             pass
        except Exception:
             pass

        with self.lock:
            try:
                # Read entire file into memory to parse sections
                with open(filename, 'r') as f:
                    lines = f.readlines()
                
                if not lines:
                    return False
                    
                # Check Metadata for date
                today_str = datetime.now().strftime("%Y-%m-%d")
                first_line = lines[0].strip().split(',')
                
                if first_line[0] == '#METADATA':
                    file_date = first_line[1]
                    if file_date != today_str:
                        logger.warning(f"[PositionsTracker] Found stale positions from {file_date} (Today: {today_str})")
                        
                        # Archive old file
                        archive_name = f"logs/positions_state_{file_date}.csv"
                        shutil.move(filename, archive_name)
                        logger.info(f"[PositionsTracker] Archived old positions to {archive_name} and starting FRESH.")
                        return False
                else:
                     # Legacy format or missing header - treat as potentially stale / risky
                     # Or check file modification time? 
                     # For safety, let's backup legacy files too if they are old
                     # But currently let's just assume we assume it's legacy and try to load, 
                     # but typically users request fresh start if issues. 
                     # Let's check file mod time just in case
                     mtime = os.path.getmtime(filename)
                     file_date = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d")
                     if file_date != today_str:
                         logger.warning(f"[PositionsTracker] Legacy file from {file_date} detected (Stale). Archiving.")
                         archive_name = f"logs/positions_state_legacy_{file_date}.csv"
                         shutil.move(filename, archive_name)
                         return False

                current_section = None
                loaded_pos_count = 0
                loaded_pnl_count = 0
                
                for line in lines:
                    line = line.strip()
                    if not line or line.startswith('#METADATA'):
                        continue
                        
                    if line == '#REALIZED_PNL':
                        current_section = 'PNL'
                        continue
                    if line == '#POSITIONS':
                        current_section = 'POSITIONS'
                        continue
                    if line.startswith('strategy,realized_pnl') or line.startswith('strategy,symbol'):
                        continue # Skip headers
                        
                    parts = line.split(',')
                    if not parts: continue
                    
                    if current_section == 'PNL':
                        # strategy, realized_pnl
                        if len(parts) >= 2:
                            strat = parts[0]
                            pnl = float(parts[1])
                            self.realized_pnl[strat] = pnl
                            loaded_pnl_count += 1
                            
                    elif current_section == 'POSITIONS' or current_section is None: 
                        # Legacy files default to positions
                        # strategy,symbol,qty,side,entry_price,leg
                        if len(parts) >= 5:
                            strategy = parts[0]
                            symbol = parts[1]
                            
                            if strategy not in self.positions_by_strategy:
                                self.register_strategy(strategy)
                            
                            self.positions_by_strategy[strategy][symbol] = {
                                'qty': int(parts[2]),
                                'side': int(parts[3]),
                                'entry_price': float(parts[4]),
                                'leg': parts[5] if len(parts) > 5 else ''
                            }
                            loaded_pos_count += 1
                
                logger.info(f"[PositionsTracker] ✓ Loaded: {loaded_pos_count} positions, {loaded_pnl_count} strategy P&L records")
                return True
            except Exception as e:
                logger.error(f"[PositionsTracker] Failed to load positions: {e}")
                return False
    
    def start_auto_save(self, interval=10, filename="positions_state.csv"):
        """Start background thread to auto-save positions periodically."""
        import time
        
        def auto_save_loop():
            while True:
                time.sleep(interval)
                self.save_positions_to_file(filename)
        
        save_thread = threading.Thread(target=auto_save_loop, daemon=True, name="PositionAutoSave")
        save_thread.start()
        logger.info(f"[PositionsTracker] Auto-save enabled (every {interval}s)")


# Global singleton instance
_positions_tracker = None


def get_positions_tracker(live_data=None, trade_context=None):
    """
    Get or create the global positions tracker instance.
    
    Args:
        live_data: Required for first initialization
        trade_context: Optional trade context for Greeks calculation
    
    Returns:
        PositionsTracker instance
    """
    global _positions_tracker
    if _positions_tracker is None:
        if live_data is None:
            raise ValueError("live_data required for first initialization")
        _positions_tracker = PositionsTracker(live_data, trade_context)
    elif trade_context is not None:
        # Update trade context if provided
        _positions_tracker.trade_context = trade_context
    return _positions_tracker


__all__ = ["PositionsTracker", "get_positions_tracker"]
