import os, requests, json
from datetime import datetime, timedelta
from fyers_api317.fyers_apiv3 import fyersModel
import api2credentials


# Load from environment or secure vault
APP_ID_HASH = "d267e9265f0bb0b6a10b3678169cd547fadc8b5f8b6d64901a253485517350f6"
REFRESH_TOKEN = api2credentials.refresh_token
PIN = "1734"
client_id = api2credentials.client_id

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
new_access_token = refresh_access_token()




#save this access code in fyers_access_token.txt file
with open('api2_fyers_access_token.txt', "w") as file:
    file.write(new_access_token)




with open("api2credentials.py", "r") as f:
    lines = f.readlines()


with open("api2credentials.py", "w") as f:
    for line in lines:
        if line.startswith("access_token"):
            f.write(f'access_token = "{new_access_token}"\n')
        else:
            f.write(line)
