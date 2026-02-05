from fyers_api317.fyers_apiv3 import fyersModel
import credentialsfyer


client_id= credentialsfyer.client_id
access_token = credentialsfyer.access_token


print(credentialsfyer.client_id)


fyers = fyersModel.FyersModel(client_id=client_id, is_async=False, token=access_token ,log_path='')



profile = fyers.get_profile()
print(profile)
