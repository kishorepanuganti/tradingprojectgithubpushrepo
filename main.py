
import logging
import os
import pandas as pd
from datetime import datetime
from fyers_apiv3 import fyersModel
import credentialsfyer
from fyers_apiv3.FyersWebsocket import data_ws
import nearestexpiryandsymbols as nearsym
from reply_from_csv import replay_csv
from testentrylogic import decide_entry, start_entry_loop
from websocketmodule import start_websocket
from live_data_feed import LiveDataStore
import threading


ENTRY_CONFIG = {
    "NIFTY": {
        "step": 50,
        "thresholds": {"0DTE": 120, "1DTE": 180}
    },
    "SENSEX": {
        "step": 100,
        "thresholds": {"0DTE": 600, "1DTE": 400}
    }
}



# Execution Flow
# -----------------------------
def main():

    logging.info("Fetching option chain data for NIFTY and SENSEX...")

    nifty_chain = nearsym.safe_get_optionchain("NSE:NIFTY50-INDEX")
    sensex_chain = nearsym.safe_get_optionchain("BSE:SENSEX-INDEX")



    if not nifty_chain or not sensex_chain:
        logging.error("Failed to fetch option chains for one or both indices.")
        return

    result = nearsym.get_nearest_index_expiry(nifty_chain,sensex_chain)

    nearest_index = result["nearest_index"]
    nearest_expiry = result["nearest_expiry"]
    dte = result["dte"]
    dte_status = result["dte_status"]
    nearest_symbols = result["nearest_symbols"]
    #index_percentage_change= index_percentage_change
    
    print(dte)
    nearest_chain = result["option_chain"]
    #print(index_percentage_change)

    
    if not nearest_chain:
        logging.error("Unable to determine nearest expiry.")
        return

    symbols = nearest_symbols


    
    
    
    if not symbols:
        logging.warning("No symbols found for nearest expiry.")
        return

    logging.info(f"Nearest Index     : {nearest_index}")
    logging.info(f"Nearest Expiry    : {nearest_expiry}")
    logging.info(f"Total Symbols     : {len(symbols)}")

    # Display a few sample symbols
    for s in symbols[:10]:
        print(" ", s)


    logging.info("Symbol extraction completed successfully.")

    config = ENTRY_CONFIG.get(nearest_index, ENTRY_CONFIG["NIFTY"])
    step = config["step"]
    thresholds = config["thresholds"]



    #start websocket for these symbols to get live data
    #websocketmodule.start_websocket(symbols)
    

    MODE = "LIVE"  # Change to "REPLAY" for CSV replay mode

    csv_path = '24octniftysymbolsdata.csv'


    class LiveDataStore:
    
        
        def __init__(self):
            self.data = {}

        def update(self, symbol, tick):
            self.data[symbol] = tick

        def get(self, symbol):
            return self.data.get(symbol)

        def get_snapshot(self):
            return dict(self.data)

    live_data = LiveDataStore()

    # -------------------------------------------------
    # Execution logic
    # -------------------------------------------------
    if MODE == "BACKTEST":
        print(">>> Running BACKTEST mode using CSV replay...")
        replay_csv(
            csv_path=csv_path,
            live_data=live_data,
            nearest_index=nearest_index,
            dte=dte,
            option_symbols=symbols,
            #entry_logic_func = decide_entry,
            entry_logic_func=lambda *args, **kwargs: decide_entry(
                *args, thresholds=thresholds, step=step, **kwargs
            ),
            delay=0.2,   # adjust to control playback speed
        )

    elif MODE == "LIVE":
        print(">>> Running LIVE mode (WebSocket feed)...")
        
        start_websocket(symbols, live_data)
        start_entry_loop(live_data, nearest_index, dte, symbols,step = step, thresholds=thresholds,)



'''
    live_data = LiveDataStore()

    # EXECUTION
# -------------------
    if MODE == "LIVE":
        print(">>> Running LIVE mode using WebSocket...")

        # Start websocket in background thread
        t = threading.Thread(target=start_websocket, args=(symbols, live_data), daemon=False)
        t.start()

    elif MODE == "BACKTEST":
        print(">>> Running BACKTEST mode using CSV replay...")
        replay_csv(csv_path, live_data, nearest_index, dte, symbols, entry_logic_func=decide_entry, delay=0.2)

    else:
        print(">>> Invalid MODE. Please set to 'LIVE' or 'BACKTEST'.")
        return


    #lets give this symbols to entry logic now
    #entrylogic.start_entry_logic(symbols,nearest_index,nearest_expiry,dte,index_percentage_change)

    entry = EntryManager(live_data)

    while True:
        entry.check_entries()
    '''



    
# -----------------------------
# Run Script
# -----------------------------
if __name__ == "__main__":
    main()
