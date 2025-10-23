import logging
from datetime import datetime
from fyers_api317.fyers_apiv3 import fyersModel
import credentialsfyer
from fyers_api317.fyers_apiv3.FyersWebsocket import data_ws



# -----------------------------
# Configuration & Setup
# -----------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)

client_id = credentialsfyer.client_id
access_token = credentialsfyer.access_token


fyers = fyersModel.FyersModel(
    client_id=client_id,
    token=access_token,
    is_async=False,
    log_path=""
)

# -----------------------------
# Core Functions
# -----------------------------
def safe_get_optionchain(symbol: str, strikecount: int = 10):
    """Safely fetch option chain data for given symbol."""
    try:
        data = {"symbol": symbol, "strikecount": strikecount, "timestamp": ""}
        response = fyers.optionchain(data=data)

        if response.get("code") != 200 or "data" not in response:
            logging.error(f"Invalid response for {symbol}: {response}")
            return None

        return response

    except Exception as e:
        logging.exception(f"Failed to fetch option chain for {symbol}: {e}")
        return None



def get_nearest_index_expiry(nifty_chain, sensex_chain):
    """Compare expiries and return the nearest index and expiry date."""
    try:
        nifty_expiry = nifty_chain["data"]["expiryData"][0]["date"]
        sensex_expiry = sensex_chain["data"]["expiryData"][0]["date"]

        nifty_date = datetime.strptime(nifty_expiry, "%d-%m-%Y")
        sensex_date = datetime.strptime(sensex_expiry, "%d-%m-%Y")
        today = datetime.now().date()
        
        if nifty_date < sensex_date:
            nearest_index = "NIFTY"
            nearest_expiry = nifty_expiry
            nearest_chain = nifty_chain
            nearest_date = nifty_date
        else:
            nearest_index = "SENSEX"
            nearest_expiry = sensex_expiry
            nearest_chain = sensex_chain
            nearest_date = sensex_date

                    # Calculate DTE (Days to Expiry)
        dte = (nearest_date.date() - today).days
        if dte < 0:
            dte_status = "Expired"
        elif dte == 0:
            dte_status = "0DTE"
        elif dte == 1:
            dte_status = "1DTE"
        elif dte == 2:
            dte_status = "2DTE"
        else:
            dte_status = f"{dte}DTE"

        return {
            "nearest_index": nearest_index,
            "nearest_expiry": nearest_expiry,
            "dte": dte,
            "dte_status": dte_status,
            "option_chain": nearest_chain
        }

    except Exception as e:
        print("Error while finding nearest expiry:", str(e))
        return None



def extract_nearest_expiry_symbols(chain_response, expiry_date):
    """Extract all CE/PE option symbols that match the nearest expiry."""
    if not chain_response or "data" not in chain_response:
        logging.error("Invalid option chain data. Cannot extract symbols.")
        return []

    try:
        options = chain_response["data"]["optionsChain"]

        expiry_dt = datetime.strptime(expiry_date, "%d-%m-%Y")
        yy = expiry_dt.strftime("%y")
        month_map = {
            1: "1", 2: "2", 3: "3", 4: "4", 5: "5", 6: "6",
            7: "7", 8: "8", 9: "9", 10: "O", 11: "N", 12: "D"
        }
        m = month_map[expiry_dt.month]
        dd = expiry_dt.strftime("%d")
        expiry_pattern = f"{yy}{m}{dd}"

        symbols = [
            opt["symbol"]
            for opt in options
            if expiry_pattern in opt.get("symbol", "")
            and opt.get("option_type") in ["CE", "PE"]
        ]

        return symbols

    except Exception as e:
        logging.exception(f"Error extracting symbols: {e}")
        return []



def start_websocket(symbols):
    from fyers_api317.fyers_apiv3.FyersWebsocket import data_ws
    
    def onmessage(message):
   
        print("Response:", message)


    def onerror(message):

        print("Error:", message)


    def onclose(message):

        print("Connection closed:", message)


    def onopen():
    
        # Specify the data type and symbols you want to subscribe to
        data_type = "SymbolUpdate"

        # Subscribe to the specified symbols and data type
        datasocket.subscribe(symbols=symbols, data_type=data_type)

        # Keep the socket running to receive real-time data
        datasocket.keep_running()

    access_token= credentialsfyer.access_token

    # Create a FyersDataSocket instance with the provided parameters

    datasocket = data_ws.FyersDataSocket(
        access_token=access_token,       # Access token in the format "appid:accesstoken"
        log_path="",                     # Path to save logs. Leave empty to auto-create logs in the current directory.
        litemode=True,                  # Lite mode disabled. Set to True if you want a lite response.
        write_to_file=False,              # Save response in a log file instead of printing it.
        reconnect=True,                  # Enable auto-reconnection to WebSocket on disconnection.
        on_connect=onopen,               # Callback function to subscribe to data upon connection.
        on_close=onclose,                # Callback function to handle WebSocket connection close events.
        on_error=onerror,                # Callback function to handle WebSocket errors.
        on_message=onmessage             # Callback function to handle incoming messages from the WebSocket.
    )

    
    # Establish a connection to the Fyers WebSocket
   
    datasocket.connect()


# -----------------------------
# Execution Flow
# -----------------------------
def main():
    logging.info("Fetching option chain data for NIFTY and SENSEX...")

    nifty_chain = safe_get_optionchain("NSE:NIFTY50-INDEX")
    sensex_chain = safe_get_optionchain("BSE:SENSEX-INDEX")



    if not nifty_chain or not sensex_chain:
        logging.error("Failed to fetch option chains for one or both indices.")
        return

    result = get_nearest_index_expiry(nifty_chain,sensex_chain)

    nearest_index = result["nearest_index"]
    nearest_expiry = result["nearest_expiry"]
    dte = result["dte"]
    dte_status = result["dte_status"]

    print(dte)
    nearest_chain = result["option_chain"]

    
    if not nearest_chain:
        logging.error("Unable to determine nearest expiry.")
        return

    symbols = extract_nearest_expiry_symbols(nearest_chain, nearest_expiry)

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

    start_websocket(symbols)



# -----------------------------
# Run Script
# -----------------------------
if __name__ == "__main__":
    main()

