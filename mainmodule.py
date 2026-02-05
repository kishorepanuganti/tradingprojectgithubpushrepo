# main_live.py
import sys
# Force unbuffered output for real-time display
sys.stdout.reconfigure(line_buffering=True)

import datetime
import logging
import threading
import time
from fyers_api317.fyers_apiv3 import fyersModel
import credentialsfyer
import nearestexpirysymbols as nearsym
from websocketmodule import start_websocket
from entry_and_monitor2 import (
    EntryModule,
    MonitorModule,
    round_to_step,
)
from strategy_decision_executor import execute_decision
from broker_helper import get_fyers_client
from network_monitor import NetworkHealthMonitor
from positions_tracker import get_positions_tracker  # Get global PositionsTracker singleton

# --------------------------------------------------------------------
# Import Configuration
# --------------------------------------------------------------------
from config.config import ENTRY_CONFIG, MONITOR_INTERVAL


class LiveDataStore:
    """Thread-safe live-data container used by websocket and monitor."""

    def __init__(self):
        import threading as _th
        self.data = {}
        self.timestamps = {}  # Track when each symbol was last updated
        self.lock = _th.Lock()
        self.websocket_handle = None  # Reference to datasocket for recovery
        self.subscription_list = []   # Symbols to subscribe to
        self.websocket_thread = None  # Reference to WebSocket thread for restart
        self.restart_callback = None  # Callback to restart WebSocket
        
        # Position callback system for real-time monitoring
        self._position_callbacks = {}  # {symbol: [callback_functions]}
        self._callback_lock = _th.Lock()

    def update(self, symbol, tick):
        """Called by websocketmodule to store latest tick for a symbol."""
        with self.lock:
            self.data[symbol] = tick
            self.timestamps[symbol] = time.time()  # Store current timestamp
        
        # Trigger callbacks for position symbols (real-time monitoring)
        self._trigger_callbacks(symbol, tick)

    def get(self, symbol):
        with self.lock:
            return self.data.get(symbol)

    def get_snapshot(self):
        with self.lock:
            return dict(self.data)
    
    def get_last_update_time(self, symbol):
        """Get the timestamp when symbol was last updated."""
        with self.lock:
            return self.timestamps.get(symbol)
    
    def is_data_stale(self, symbol, max_age_seconds=30):
        """Check if data for symbol is older than max_age_seconds."""
        last_update = self.get_last_update_time(symbol)
        if last_update is None:
            return True  # No data received = stale
        return (time.time() - last_update) > max_age_seconds
    
    def set_websocket_handle(self, datasocket, subscription_list, ws_thread=None, restart_callback=None):
        """Store websocket handle, thread, and subscription list for recovery."""
        with self.lock:
            self.websocket_handle = datasocket
            self.subscription_list = subscription_list
            self.websocket_thread = ws_thread
            self.restart_callback = restart_callback
    
    def manual_resubscribe(self):
        """Manually trigger resubscription when staleness detected."""
        with self.lock:
            if self.websocket_handle and self.subscription_list:
                try:
                    logging.info(f"[Recovery] Attempting manual resubscription to {len(self.subscription_list)} symbols")
                    self.websocket_handle.subscribe(symbols=self.subscription_list, data_type="SymbolUpdate")
                    logging.info("[Recovery] Manual resubscription triggered successfully")
                    return True
                except Exception as e:
                    logging.error(f"[Recovery] Manual resubscription failed: {e}")
                    return False
            else:
                logging.warning("[Recovery] Cannot resubscribe - websocket handle not available")
                return False
    
    def force_reconnect(self):
        """Force complete WebSocket reconnection when auto-reconnect fails (e.g., DNS issues)."""
        logging.warning("[FORCE RECONNECT] Auto-reconnect failed repeatedly. Attempting to restart WebSocket connection...")
        
        # Step 1: Try to close existing connection
        try:
            if self.websocket_handle:
                logging.info("[FORCE RECONNECT] Closing existing WebSocket connection...")
                self.websocket_handle.close()
                import time
                time.sleep(2)  # Wait for clean shutdown
        except Exception as e:
            logging.error(f"[FORCE RECONNECT] Error closing WebSocket: {e}")
        
        # Step 2: Call restart callback if available
        if self.restart_callback:
            try:
                logging.info("[FORCE RECONNECT] Calling restart callback...")
                return self.restart_callback()
            except Exception as e:
                logging.error(f"[FORCE RECONNECT] Restart callback failed: {e}")
                return False
        else:
            logging.error("[FORCE RECONNECT] No restart callback available. Manual restart required.")
            return False
    
    def _trigger_callbacks(self, symbol, tick):
        """Trigger all registered callbacks for this symbol (real-time position monitoring)."""
        with self._callback_lock:
            callbacks = self._position_callbacks.get(symbol, [])
            if callbacks:
                # Execute callbacks in separate thread to avoid blocking WebSocket
                for callback in callbacks:
                    try:
                        threading.Thread(target=callback, args=(symbol, tick), daemon=True).start()
                    except Exception as e:
                        logging.error(f"Position callback error for {symbol}: {e}")
    
    def register_position_callback(self, symbol, callback):
        """Register a callback to be triggered when symbol tick arrives (for position monitoring)."""
        with self._callback_lock:
            if symbol not in self._position_callbacks:
                self._position_callbacks[symbol] = []
            if callback not in self._position_callbacks[symbol]:
                self._position_callbacks[symbol].append(callback)
                logging.debug(f"[LiveData] Registered real-time callback for position symbol: {symbol}")
    
    def unregister_position_callback(self, symbol, callback):
        """Unregister a callback for a symbol (when position is closed)."""
        with self._callback_lock:
            if symbol in self._position_callbacks:
                self._position_callbacks[symbol] = [
                    cb for cb in self._position_callbacks[symbol] if cb != callback
                ]
                if not self._position_callbacks[symbol]:
                    del self._position_callbacks[symbol]
                logging.debug(f"[LiveData] Unregistered callback for position symbol: {symbol}")


# --------------------------------------------------------------------
# Monitor callback
# --------------------------------------------------------------------
def on_update(context, update):
    current_straddle = update["straddle"]  # Current ATM straddle
    entry_straddle = update.get("entry_straddle")  # Entry ATM straddle (for comparison)
    idx_ltp = update["underlying_ltp"]
    current_atm = update.get("current_atm", context['atm_strike'])
    
    # Display with timestamp for better visibility
    from datetime import datetime
    timestamp = datetime.now().strftime("%H:%M:%S")
    
    # Format straddle display
    if entry_straddle is not None:
        straddle_display = f"EntryStraddle: ₹{entry_straddle:.2f} | CurrStraddle: ₹{current_straddle:.2f}"
        straddle_log = f"EntryStraddle={entry_straddle:.2f} | CurrStraddle={current_straddle:.2f}"
    else:
        straddle_display = f"CurrStraddle: ₹{current_straddle:.2f}"
        straddle_log = f"CurrStraddle={current_straddle:.2f}"

    print(f"[{timestamp}] Monitor Tick → EntryATM: {context['atm_strike']} | CurrATM: {current_atm} | {straddle_display} | Index: {idx_ltp:.2f}", flush=True)
    logging.info(
        f"[Monitor] {context['index']} EntryATM={context['atm_strike']} | CurrATM={current_atm} | "
        f"{straddle_log} | Index={idx_ltp:.2f}"
        
    )
    # TODO: Add portfolio-level MTM target/stop tracking here later


# --------------------------------------------------------------------
# Main Live launcher
# --------------------------------------------------------------------
def main_live():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    logging.info("Fetching option chain data for NIFTY and SENSEX...")

    nifty_chain = nearsym.safe_get_optionchain("NSE:NIFTY50-INDEX")
    sensex_chain = nearsym.safe_get_optionchain("BSE:SENSEX-INDEX")
    if not nifty_chain or not sensex_chain:
        logging.error("Failed to fetch option chains for indices.")
        return


    result = nearsym.get_nearest_index_expiry(nifty_chain, sensex_chain)
    nearest_index = result["nearest_index"]
    nearest_expiry = result["nearest_expiry"]
    dte = result["dte"]
    nearest_symbols = result["nearest_symbols"]

    logging.info(f"Nearest Index     : {nearest_index}")
    logging.info(f"Nearest Expiry    : {nearest_expiry}")
    logging.info(f"Total Symbols     : {len(nearest_symbols)}")

    for s in nearest_symbols[:10]:
        print(" ", s)

    # Create live_data store and start websocket
    live_data = LiveDataStore()

    # WebSocket restart function for network recovery
    def restart_websocket():
        """
        Restart WebSocket connection when network recovers.
        Does NOT require re-checking entry conditions or expiry.
        """
        try:
            logging.warning("[RESTART] Attempting to restart WebSocket connection...")
            
            # Start new WebSocket thread
            ws_thread = threading.Thread(
                target=start_websocket,
                args=(nearest_symbols, live_data),
                daemon=True,
                name="WebSocket-Restarted"
            )
            ws_thread.start()
            
            logging.info("[RESTART] WebSocket restarted successfully!")
            return True
        except Exception as e:
            logging.error(f"[RESTART] Failed to restart WebSocket: {e}")
            return False
    
    # Start websocket in background so `live_data` receives ticks
    logging.info("Starting websocket to collect live ticks...")
    ws_thread = threading.Thread(
        target=start_websocket,
        args=(nearest_symbols, live_data),
        daemon=True,
    )
    ws_thread.start()




    # Wait for initial ticks and verify data is flowing
    logging.info("Waiting for websocket to connect and receive initial data...")
    time.sleep(5)
    
    # Verify that we're actually receiving data for index symbols
    index_symbol_map = {
        "NIFTY": "NSE:NIFTY50-INDEX",
        "SENSEX": "BSE:SENSEX-INDEX",
    }
    
    max_wait = 15  # seconds
    wait_interval = 2  # seconds
    elapsed = 0
    data_received = False
    
    while elapsed < max_wait:
        snapshot = live_data.get_snapshot()
        index_symbols_with_data = [
            idx for idx, sym in index_symbol_map.items() 
            if sym in snapshot and snapshot[sym].get("ltp") is not None
        ]
        
        if index_symbols_with_data:
            data_received = True
            logging.info(f" Websocket data confirmed for: {', '.join(index_symbols_with_data)}")
            logging.info(f" Total symbols receiving data: {len(snapshot)}/{len(nearest_symbols) + 2}")
            break
        
        if elapsed % 5 == 0 and elapsed > 0:
            logging.warning(f"Still waiting for data... ({elapsed}s/{max_wait}s)")
        
        time.sleep(wait_interval)
        elapsed += wait_interval
    
    if not data_received:
        logging.warning(
            "ALERT: No index data received after %ds. Websocket may not be connected properly. "
            "Continuing anyway, but ATM straddle display may show stale/missing data.",
            max_wait
        )
    else:
        logging.info("Websocket connection verified and active!")
    
    # Start network health monitor for automatic recovery
    logging.info("Starting network health monitor...")
    network_monitor = NetworkHealthMonitor(
        check_interval=10,  # Check every 10 seconds
        recovery_callback=restart_websocket  # Auto-restart WebSocket when network recovers
    )
    network_monitor.start()
    logging.info("Network monitor started - will auto-restart WebSocket if network recovers")

    # Run single entry check (morning/9:20 style)
    entry_module = EntryModule(
        live_data=live_data,
        nearest_index=nearest_index,
        dte=dte,
        option_symbols=nearest_symbols,
        config=ENTRY_CONFIG,
    )
    trade_ctx = entry_module.run_entry_check()
    
    if trade_ctx:
        # Inject entry config into trade context for downstream strategies to use (e.g. lot sizes)
        trade_ctx["entry_config"] = ENTRY_CONFIG[nearest_index]
        
        # Get the global PositionsTracker (same one strategies use)
        positions_track = get_positions_tracker(live_data, trade_ctx)
        logging.info(f"Using global PositionsTracker for dashboard (has {len(positions_track.positions_by_strategy)} strategies)")

    if not trade_ctx:
        logging.warning("No entry taken — DTE or conditions not met.")
        # still keep websocket running if you want to monitor; exit or wait
        return

    # Print the ATM straddle at entry
    initial_str = trade_ctx.get("straddle")
    print(f"Initial ATM Straddle Premium: {initial_str:.2f}")

    # Call the strategy decision executor based on entry decision
    # This will initialize and run multiple strategies for the selected decision type
    decision = trade_ctx["decision"]
    
    # Initialize broker connection for order placement
    try:
        broker_client = get_fyers_client()
        logging.info("Broker client initialized successfully for order placement")
    except Exception as e:
        logging.error("Failed to initialize broker client: %s", e)
        broker_client = None
    
    strategy_executor = execute_decision(
        decision=decision,
        trade_ctx=trade_ctx,
        live_data=live_data,
        broker=broker_client  # Pass the broker connection to strategies
    )

    logging.info(
        f"Strategy executor started: {strategy_executor.__class__.__name__} | "
        f"Strategies: {len(strategy_executor.strategies)}"
    )

    # ========================================================================
    # START LIVE DASHBOARD (Optional - for visual monitoring and demos)
    # ========================================================================
    try:
        from dashboard_server import run_dashboard, set_live_data_store
        
        # Set the live data reference with PositionsTracker
        set_live_data_store(live_data, positions_track, strategy_executor)
        
        # Start dashboard in background thread
        def start_dashboard():
            run_dashboard(port=5000, debug=False)
        
        dashboard_thread = threading.Thread(
            target=start_dashboard,
            daemon=True,
            name="DashboardServer"
        )
        dashboard_thread.start()
        
        print(f"\n{'='*80}", flush=True)
        print(f"📊 DASHBOARD AVAILABLE AT: http://localhost:5000", flush=True)
        print(f"   Open in browser to see live market data & backtest results", flush=True)
        print(f"{'='*80}\n", flush=True)
        logging.info("Dashboard started successfully at http://localhost:5000")
        
    except ImportError as e:
        logging.info(f"Dashboard not available (missing dependencies): {e}")
        logging.info("To enable dashboard: pip install flask flask-cors")
    except Exception as e:
        logging.warning(f"Dashboard failed to start: {e}")
    # ========================================================================

    # Start Monitor
    print(f"\n{'='*80}", flush=True)
    print(f"STARTING MONITOR - Will display ticks every {MONITOR_INTERVAL} seconds", flush=True)
    print(f"MTM P&L will display every 15 seconds", flush=True)
    print(f"{'='*80}\n", flush=True)
    
    monitor = MonitorModule(
        live_data=live_data,
        trade_context=trade_ctx,
        interval=MONITOR_INTERVAL,
        on_update=on_update,
        strategy_executor=strategy_executor,  # Pass executor so monitor calls strategy.on_tick()
    )
    
    monitor.start()
    logging.info("Monitor started. Running live...")
    print("Monitor started successfully! Waiting for ticks...\n", flush=True)

    # keep process running; monitor runs in background
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        logging.info("Stopping monitor and exiting...")
        monitor.stop()


if __name__ == "__main__":
    main_live()