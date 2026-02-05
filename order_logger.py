"""Order logging to CSV/Excel for backtesting and paper trading.

This module logs all orders (entry, square-off, re-entry) to a CSV file
instead of placing them via the broker. Useful for testing strategies
without live broker connection.
"""
import csv
import logging
from datetime import datetime
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)


class OrderLogger:
    """Log orders to CSV file for backtesting and analysis."""

    def __init__(self, filepath: str = "backtest_orders.csv"):
        """Initialize order logger.
        
        Args:
            filepath: Path to CSV file to log orders (default: backtest_orders.csv)
        """
        self.filepath = filepath
        self.orders = []
        self._initialize_csv()
    
    def _initialize_csv(self):
        """Create CSV header if file doesn't exist."""
        try:
            with open(self.filepath, 'w', newline='') as f:
                writer = csv.DictWriter(f, fieldnames=[
                    'timestamp', 'symbol', 'qty', 'side', 'type', 'entry_price',
                    'current_ltp', 'pnl', 'adjustment_count', 'order_tag', 'status'
                ])
                writer.writeheader()
            logger.info(f"Order logger initialized: {self.filepath}")
        except Exception as e:
            logger.error(f"Failed to initialize CSV: {e}")
    
    def log_order(self, symbol: str, qty: int, side: int, order_type: int,
                  entry_price: float, current_ltp: Optional[float] = None,
                  pnl: Optional[float] = None, adjustment_count: int = 0,
                  order_tag: str = "", status: str = "PLACED") -> Dict[str, Any]:
        """Log an order to CSV.
        
        Args:
            symbol: Option symbol (e.g., NSE:NIFTY25DEC25950CE)
            qty: Order quantity
            side: 1=BUY, -1=SELL
            order_type: 1=MARKET, 2=LIMIT, etc.
            entry_price: Entry/execution price
            current_ltp: Current LTP (for P&L calculation)
            pnl: Current P&L if available
            adjustment_count: Number of adjustments made
            order_tag: Custom order tag (e.g., "CESHORT", "SQUAREOFF", "REENTER_CE")
            status: Order status (PLACED, EXECUTED, CANCELLED)
        
        Returns:
            Order dict that was logged
        """
        order = {
            'timestamp': datetime.now().isoformat(),
            'symbol': symbol,
            'qty': qty,
            'side': side,
            'type': order_type,
            'entry_price': entry_price,
            'current_ltp': current_ltp if current_ltp else "",
            'pnl': round(pnl, 2) if pnl else "",
            'adjustment_count': adjustment_count,
            'order_tag': order_tag,
            'status': status
        }
        
        self.orders.append(order)
        self._append_to_csv(order)
        return order
    
    def _append_to_csv(self, order: Dict[str, Any]):
        """Append a single order to CSV file."""
        try:
            with open(self.filepath, 'a', newline='') as f:
                writer = csv.DictWriter(f, fieldnames=[
                    'timestamp', 'symbol', 'qty', 'side', 'type', 'entry_price',
                    'current_ltp', 'pnl', 'adjustment_count', 'order_tag', 'status'
                ])
                writer.writerow(order)
        except Exception as e:
            logger.error(f"Failed to write order to CSV: {e}")
    
    def get_order_summary(self) -> Dict[str, Any]:
        """Return summary of logged orders."""
        if not self.orders:
            return {"total_orders": 0}
        
        buy_orders = [o for o in self.orders if o['side'] == 1]
        sell_orders = [o for o in self.orders if o['side'] == -1]
        
        return {
            'total_orders': len(self.orders),
            'buy_orders': len(buy_orders),
            'sell_orders': len(sell_orders),
            'filepath': self.filepath
        }
    
    def print_summary(self):
        """Print summary to console."""
        summary = self.get_order_summary()
        logger.info(f"Order Summary: {summary}")


# Global instances keyed by filepath (supports multiple different log files)
_order_loggers: Dict[str, OrderLogger] = {}


def _dated_filepath(filepath: str) -> str:
    """Return filepath with today's date inserted before extension.
    Also prepends 'data/' directory to save all order logs in the data folder.

    Examples:
        strangle_orders.csv -> data/strangle_orders_2025-12-31.csv
        iron_condor_orders.csv -> data/iron_condor_orders_2025-12-31.csv
    """
    import os
    try:
        date_str = datetime.now().strftime("%Y-%m-%d")
        if "." in filepath:
            parts = filepath.rsplit(".", 1)
            dated_name = f"{parts[0]}_{date_str}.{parts[1]}"
        else:
            dated_name = f"{filepath}_{date_str}"
        
        # Prepend data/ directory
        return os.path.join("data", dated_name)
    except Exception:
        return filepath


def get_order_logger(filepath: str = "backtest_orders.csv") -> OrderLogger:
    """Get or create an OrderLogger for the given filepath with today's date.

    This creates a separate CSV per calendar day. Multiple different filepaths
    (e.g., different strategies) will get their own logger instances.
    """
    dated = _dated_filepath(filepath)
    logger_inst = _order_loggers.get(dated)
    if logger_inst is None:
        logger_inst = OrderLogger(dated)
        _order_loggers[dated] = logger_inst
    return logger_inst


__all__ = ["OrderLogger", "get_order_logger"]
