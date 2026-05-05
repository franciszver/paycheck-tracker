import os
import plaid
from plaid.api import plaid_api

def make_client(config):
    env = config.get("plaid_env", "sandbox").lower()
    if env == "production":
        secret = os.environ["PLAID_SECRET_PRODUCTION"]
        host = plaid.Environment.Production
    else:
        secret = os.environ["PLAID_SECRET_SANDBOX"]
        host = plaid.Environment.Sandbox

    configuration = plaid.Configuration(
        host=host,
        api_key={"clientId": os.environ["PLAID_CLIENT_ID"], "secret": secret},
    )
    return plaid_api.PlaidApi(plaid.ApiClient(configuration))

def access_token(config):
    env = config.get("plaid_env", "sandbox").lower()
    key = "PLAID_ACCESS_TOKEN_PRODUCTION" if env == "production" else "PLAID_ACCESS_TOKEN_SANDBOX"
    return os.environ[key]
