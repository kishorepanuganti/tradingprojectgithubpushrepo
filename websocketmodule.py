
import logging
import os
from urllib import response
import pandas as pd
from datetime import datetime
from fyers_api317.fyers_apiv3 import fyersModel
import credentialsfyer
from fyers_api317.fyers_apiv3.FyersWebsocket import data_ws


def start_websocket(symbols, live_data_store):
    # Initialize WebSocket connection
    websocket_symbols = symbols  # Store in a new variable if needed
    print("Symbols to subscribe:", websocket_symbols)
    def onmessage(message):
        try:

            live_data_store.update(message)
            
            # Convert message to DataFrame
            df = pd.DataFrame([message])
            
            # Save to CSV file, append mode
            df.to_csv('25oct.csv', mode='a', header=not os.path.exists('25oct.csv'), index=False)

            # Optional: Print response for monitoring
            print("Response saved:", message)
        except Exception as e:
            print(f"Error saving data: {e}")


    def onerror(message):

        print("Error:", message)


    def onclose(message):

        print("Connection closed:", message)


    def onopen():
        # Specify the data type and symbols you want to subscribe to
        data_type = "SymbolUpdate"
        
        # Use the websocket_symbols defined in outer scope
        datasocket.subscribe(symbols=websocket_symbols, data_type=data_type)

        # Keep the socket running to receive real-time data
        datasocket.keep_running()

    access_token= credentialsfyer.access_token

    # Create a FyersDataSocket instance with the provided parameters

    datasocket = data_ws.FyersDataSocket(
        access_token=access_token,       # Access token in the format "appid:accesstoken"
        log_path="",                     # Path to save logs. Leave empty to auto-create logs in the current directory.
        litemode=False,                  # Lite mode disabled. Set to True if you want a lite response.
        write_to_file=False,              # Save response in a log file instead of printing it.
        reconnect=True,                  # Enable auto-reconnection to WebSocket on disconnection.
        on_connect=onopen,               # Callback function to subscribe to data upon connection.
        on_close=onclose,                # Callback function to handle WebSocket connection close events.
        on_error=onerror,                # Callback function to handle WebSocket errors.
        on_message=onmessage             # Callback function to handle incoming messages from the WebSocket.
    )

    
    # Establish a connection to the Fyers WebSocket
   
    datasocket.connect()




