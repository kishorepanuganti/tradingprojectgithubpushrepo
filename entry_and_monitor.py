"""
entry_and_monitor.py

Provides:
 - EntryModule: run once at market open (or scheduled time) to pick strategy and build trade_context
 - MonitorModule: tracks index & ATM CE/PE LTPs and computes straddle premium every N seconds

Assumptions:
 - `live_data` is a simple shared object with .get(symbol) -> latest tick dict (contains 'ltp' and optionally raw)
 - option_symbols is a list of all CE/PE symbols for nearest expiry (like you already extract)
 - Your websocket updates live_data in real-time; these modules only read live_data
"""

import logging
import threading
import time
import re
from datetime import datetime
from typing import List, Dict, Callable, Optional, Tuple

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

STRIKE_RE = re.compile(r"(\d{4,7})")


# -----------------------
# Helper functions
# -----------------------
def round_to_strike(price: float, step: int) -> int:
    """Round price to nearest strike multiple."""
    return int(round(price / step) * step)


def build_strike_map(option_symbols: List[str]) -> Dict[int, Dict[str, Optional[str]]]:
    """
    Build map: { strike: {"CE": symbol, "PE": symbol} }
    Keeps all strikes even if missing legs (caller checks).
    """
    m = {}
    for s in option_symbols:
        found = STRIKE_RE.search(s)
        if not found:
            continue
        strike = int(found.group(1))
        entry = m.setdefault(strike, {"CE": None, "PE": None})
        if s.endswith("CE"):
            entry["CE"] = s
        elif s.endswith("PE"):
            entry["PE"] = s
    return m


def choose_atm_and_symbols(
    underlying_sym: str,
    strike_map: Dict[int, Dict[str, Optional[str]]],
    live_data,
    step: int
) -> Tuple[Optional[int], Optional[str], Optional[str]]:
    """
    Return (chosen_atm_strike, ce_symbol, pe_symbol) by:
     - reading underlying LTP from live_data
     - rounding to nearest strike (step)
     - picking exact strike if present otherwise nearest available strike
    """
    tick = live_data.get(underlying_sym)
    if not tick or tick.get("ltp") in (None, ""):
        logger.debug("Underlying tick missing for %s", underlying_sym)
        return None, None, None

    try:
        underlying_ltp = float(tick["ltp"])
    except Exception:
        logger.exception("Invalid underlying LTP for %s", underlying_sym)
        return None, None, None

    atm_guess = round_to_strike(underlying_ltp, step)
    available = sorted(strike_map.keys())
    if not available:
        logger.debug("No strikes available in strike_map")
        return None, None, None

    chosen = atm_guess if atm_guess in strike_map else min(available, key=lambda s: abs(s - atm_guess))
    ce = strike_map[chosen].get("CE")
    pe = strike_map[chosen].get("PE")
    return chosen, ce, pe


def get_ltp(sym: Optional[str], live_data) -> Optional[float]:
    """Safe LTP extraction. Returns None if missing or invalid."""
    if not sym:
        return None
    tick = live_data.get(sym)
    if not tick:
        return None
    try:
        return float(tick.get("ltp"))
    except Exception:
        # fallback to raw
        raw = tick.get("raw", {}) if isinstance(tick, dict) else {}
        try:
            return float(raw.get("ltp"))
        except Exception:
            return None


def compute_underlying_pct_from_tick(tick: Dict) -> Optional[float]:
    """
    Try to extract percent-change (absolute) from underlying tick
    Prefer 'ltpchp' if present, else compute from 'ltpch' and 'ltp'.
    Returns value like 0.25 meaning 0.25%.
    """
    if not tick:
        return None
    raw = tick.get("raw", tick)
    if not raw:
        return None
    try:
        if "ltpchp" in raw and raw["ltpchp"] not in (None, ""):
            return abs(float(raw["ltpchp"]))
        if "ltpch" in raw and raw["ltpch"] not in (None, "") and "ltp" in raw and raw["ltp"] not in (None, ""):
            ltpc = float(raw["ltpch"])
            ltp = float(raw["ltp"])
            prev = ltp - ltpc
            if prev != 0:
                return abs((ltpc / prev) * 100.0)
    except Exception:
        logger.debug("Failed to compute underlying pct from raw tick", exc_info=True)
    return None


# -----------------------
# Strategy placeholders (do nothing - user will implement)
# -----------------------
def high_risk_strategy_entry(context: dict):
    logger.info("[STRATEGY CALL] high_risk_strategy_entry -> %s", context)


def high_rr_strategy_entry(context: dict):
    logger.info("[STRATEGY CALL] high_rr_strategy_entry -> %s", context)


def low_risk_strategy_entry(context: dict):
    logger.info("[STRATEGY CALL] low_risk_strategy_entry -> %s", context)


# -----------------------
# Entry Module
# -----------------------
class EntryModule:
    """
    Run once (scheduled or manual) to decide the strategy and build trade_context.

    Usage:
        entry = EntryModule(live_data, option_symbols, config)
        trade_context = entry.run_entry_check()
    """

    def __init__(
        self,
        live_data,
        nearest_index: str,
        dte: int,
        option_symbols: List[str],
        config: Dict,
        underlying_symbol_map: Dict[str, str] = None,
    ):
        """
        config example:
        {
          "NIFTY": {"step": 50, "thresholds": {"0DTE": 120, "1DTE": 180}},
          "SENSEX": {"step": 100, "thresholds": {"0DTE": 600, "1DTE": 400}}
        }
        """
        self.live_data = live_data
        self.nearest_index = nearest_index
        self.dte = dte
        self.option_symbols = option_symbols
        self.config = config
        self.underlying_symbol_map = underlying_symbol_map or {"NIFTY": "NSE:NIFTY50-INDEX", "SENSEX": "BSE:SENSEX-INDEX"}
        self.entry_done = False


    def run_entry_check(self) -> Optional[dict]:
        """
        Execute entry rules once and return trade_context if a strategy chosen, else None.
        trade_context contains atm strike, symbols to monitor, chosen strategy name, dte, etc.
        """
        if self.dte > 1:
            logger.info("Skipping entry — DTE = %s (>1DTE).", self.dte)
            self.entry_done = True
            return None

        cfg = self.config.get(self.nearest_index)
        if not cfg:
            logger.error("No config for index %s", self.nearest_index)
            return None

        step = cfg["step"]
        thresholds = cfg["thresholds"]

        # build strike mapping
        strike_map = build_strike_map(self.option_symbols)

        underlying_sym = self.underlying_symbol_map[self.nearest_index]
        chosen, ce_sym, pe_sym = choose_atm_and_symbols(underlying_sym, strike_map, self.live_data, step)
        if not chosen or not ce_sym or not pe_sym:
            logger.warning("ATM pair not found for %s at this time. ATM=%s CE=%s PE=%s", self.nearest_index, chosen, ce_sym, pe_sym)
            return None

        ce_ltp = get_ltp(ce_sym, self.live_data)
        pe_ltp = get_ltp(pe_sym, self.live_data)
        if ce_ltp is None or pe_ltp is None:
            logger.warning("Missing CE/PE LTPs for ATM symbols: %s / %s", ce_sym, pe_sym)
            return None

        straddle = ce_ltp + pe_ltp
        underlying_tick = self.live_data.get(underlying_sym)
        underlying_ltp = float(underlying_tick["ltp"]) if underlying_tick and underlying_tick.get("ltp") not in (None, "") else None
        underlying_pct = compute_underlying_pct_from_tick(underlying_tick)

        # decide
        decision = None
        reason = None

        if self.dte == 1:
            min_str = thresholds["1DTE"]
            if straddle >= min_str and (underlying_pct is None or underlying_pct <= 0.3):
                decision = "HIGH_RISK"
                reason = f"1DTE and straddle >= {min_str} and underlying quiet (pct={underlying_pct})"
                high_risk_strategy_entry  # placeholder - do not call here, we just choose
            else:
                decision = "LOW_RISK"
                reason = f"1DTE but criteria not met (straddle={straddle}, pct={underlying_pct})"
        elif self.dte == 0:
            min_str = thresholds["0DTE"]
            if straddle >= min_str and (underlying_pct is None or underlying_pct <= 0.3):
                decision = "HIGH_RR"
                reason = f"0DTE and straddle >= {min_str} and underlying quiet (pct={underlying_pct})"
            else:
                decision = "LOW_RISK"
                reason = f"0DTE but criteria not met (straddle={straddle}, pct={underlying_pct})"
        else:
            logger.info("DTE not 0/1: %s", self.dte)
            return None

        # Build trade_context with only the symbols to monitor (ATM CE/PE + underlying)
        trade_context = {
            "index": self.nearest_index,
            "expiry_dte": self.dte,
            "decision": decision,
            "reason": reason,
            "atm_strike": chosen,
            "ce_symbol": ce_sym,
            "pe_symbol": pe_sym,
            "underlying_symbol": underlying_sym,
            "initial_straddle": straddle,
            "underlying_ltp": underlying_ltp,
            "underlying_pct": underlying_pct,
            "config": cfg,
            "timestamp": datetime.now().isoformat()
        }

        logger.info("Entry decision: %s | %s | ATM=%s CE=%s PE=%s straddle=%s", decision, reason, chosen, ce_sym, pe_sym, straddle)
        return trade_context

        self.entry_done = True
        logger.info("✅ Entry locked for the day. Switching to monitor mode.")


# -----------------------
# Monitor Module
# -----------------------
class MonitorModule:
    """
    Monitor ATM straddle and underlying tick. Runs periodically (default every 5s).
    You must supply `live_data` that the websocket updates.

    on_update callback signature: func(trade_context: dict, update: dict)
        where update contains {"timestamp","ce_ltp","pe_ltp","straddle","underlying_ltp"}
    """

    def __init__(self, live_data, trade_context: dict, interval: int = 5, on_update: Callable = None):
        self.live_data = live_data
        self.context = trade_context
        self.interval = interval
        self.on_update = on_update
        self._stop = threading.Event()
        self._thread = None

    def _compute_once(self) -> Optional[dict]:
        ce = self.context.get("ce_symbol")
        pe = self.context.get("pe_symbol")
        underlying = self.context.get("underlying_symbol")
        ce_ltp = get_ltp(ce, self.live_data)
        pe_ltp = get_ltp(pe, self.live_data)
        underlying_tick = self.live_data.get(underlying)
        underlying_ltp = float(underlying_tick["ltp"]) if underlying_tick and underlying_tick.get("ltp") not in (None, "") else None

        if ce_ltp is None or pe_ltp is None or underlying_ltp is None:
            # missing data — caller may want to handle; return None so monitor can continue
            logger.debug("Monitor: missing data CE:%s PE:%s underlying:%s", ce_ltp, pe_ltp, underlying_ltp)
            return None

        update = {
            "timestamp": datetime.now().isoformat(),
            "ce_ltp": ce_ltp,
            "pe_ltp": pe_ltp,
            "straddle": ce_ltp + pe_ltp,
            "underlying_ltp": underlying_ltp
        }
        return update

    def _run_loop(self):
        logger.info("Monitor loop started for ATM %s (interval=%ss)", self.context.get("atm_strike"), self.interval)
        while not self._stop.is_set():
            try:
                update = self._compute_once()
                if update:
                    # update context with latest values
                    self.context["last_update"] = update
                    # callback for user-defined logic (alerts, MTM checks, adjustments)
                    if callable(self.on_update):
                        try:
                            self.on_update(self.context, update)
                        except Exception:
                            logger.exception("on_update callback failed")
                time.sleep(self.interval)
            except Exception:
                logger.exception("Monitor loop error")
                time.sleep(self.interval)

        logger.info("Monitor loop stopped")

    def start(self, daemon: bool = True):
        if self._thread and self._thread.is_alive():
            logger.warning("Monitor already running")
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run_loop, daemon=daemon)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)


# -----------------------
# Example integration snippet (to call from your main)
# -----------------------
"""
# Example usage in main():

# 1) Build ENTRY_CONFIG in main (passed from user)
ENTRY_CONFIG = {
    "NIFTY": {"step": 50, "thresholds": {"0DTE": 120, "1DTE": 180}},
    "SENSEX": {"step": 100, "thresholds": {"0DTE": 600, "1DTE": 400}}
}

# 2) After getting nearest_index, dte, and option_symbols (nearest expiry)
entry = EntryModule(live_data, nearest_index, dte, option_symbols, ENTRY_CONFIG)
trade_ctx = entry.run_entry_check()
if trade_ctx:
    # (You could call the strategy here)
    if trade_ctx["decision"] == "HIGH_RISK":
        high_risk_strategy_entry(trade_ctx)
    elif trade_ctx["decision"] == "HIGH_RR":
        high_rr_strategy_entry(trade_ctx)
    else:
        low_risk_strategy_entry(trade_ctx)

    # 3) Start monitor for ATM straddle & underlying
    def on_update(context, update):
        # example: print or persist update, or check MTM/exit rules
        logger.info("Monitor update: %s", update)
        # if needed: check portfolio-level target/stop and call close logic

    monitor = MonitorModule(live_data, trade_ctx, interval=5, on_update=on_update)
    monitor.start()

# To stop later:
# monitor.stop()
"""
