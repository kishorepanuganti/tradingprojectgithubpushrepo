from fyers_apiv3 import fyersModel
import credentialsfyer

client_id = "B50F5OA0Y0-100"
access_token = credentialsfyer.access_token

# Initialize the FyersModel instance with your client_id, access_token, and enable async mode
fyers = fyersModel.FyersModel(client_id=client_id, is_async=False, token=access_token, log_path="")

# Make a request to get the user profile information
profile = fyers.get_profile()

# Print the response received from the Fyers API
print(profile)

funds = fyers.funds()
print(funds)


orderbook = fyers.orderbook()
print(orderbook)

positions = fyers.positions()
print(positions)

tradebook = fyers.tradebook()
print(tradebook)

marketstatus = fyers.market_status()
print(marketstatus)


