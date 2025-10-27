
# Import the required module from the fyers_apiv3 package
import webbrowser
from fyers_apiv3 import fyersModel
import requests
import credentialsfyer


# Replace these values with your actual API credentials
client_id = credentialsfyer.client_id
secret_key = credentialsfyer.secret_key
redirect_uri = "https://www.google.com"
response_type = "code"  
state = "sample_state"
grant_type = "authorization_code"


# Create a session model with the provided credentials
session = fyersModel.SessionModel(
    client_id=client_id,
    secret_key=secret_key,
    redirect_uri=redirect_uri,
    response_type=response_type,
    grant_type=grant_type,
    state=state

)

# Generate the auth code using the session model
generateTokenUrl = session.generate_authcode()

# Print the auth code received in the response
print(generateTokenUrl)   #this is the url to be opened in browser for authcode. copy authcode after "authcode=" in url.


#continue to login and click on copy button on webpage to copy Auth code generated
auth_code= input("enter authentication code: ")
session.set_token(auth_code)
response = session.generate_token()

print(response)



#here is the access token will be used further for creating Fyers instance of your account.

try: 
    access_token= response['access_token']
    print('token: ', access_token)

except Exception as e:
    print(e,response)



with open('fyers_client_id.txt', 'w') as file:
    file.write(client_id)

with open('fyers_access_token.txt', 'w') as file:
    file.write(access_token)

with open('fyers_refresh_token.txt', 'w') as file:
    file.write(response['refresh_token'])


