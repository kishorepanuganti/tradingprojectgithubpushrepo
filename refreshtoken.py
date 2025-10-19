import os, requests, json
from datetime import datetime, timedelta
from fyers_api317.fyers_apiv3 import fyersModel
import credentialsfyer


# Load from environment or secure vault
APP_ID_HASH = "717a7915bcd4b68c701dad202feec433b2c669336dc17bc241f30cc160d3b8e3"
REFRESH_TOKEN = credentialsfyer.refresh_token
PIN = "1734"
client_id = credentialsfyer.client_id

def refresh_access_token():
    url = "https://api-t1.fyers.in/api/v3/validate-refresh-token"
    payload = {
        "grant_type": "refresh_token",
        "appIdHash": APP_ID_HASH,
        "refresh_token": REFRESH_TOKEN,
        "pin": PIN
    }
    headers = {"Content-Type": "application/json"}
    response = requests.post(url, json=payload, headers=headers)
    data = response.json()

    if data.get("s") == "ok":
        new_token = data["access_token"]
        print("✅ Access token refreshed:", new_token)
        # Optionally update REFRESH_TOKEN if rotated
        return new_token
    else:
        print("❌ Failed to refresh:", data.get("message"))
        return None

# Example usage
access_token = refresh_access_token()

credentialsfyer.access_token = access_token

#save this access code in fyers_access_token.txt file
with open('fyers_access_token.txt', "w") as file:
    file.write(access_token)


