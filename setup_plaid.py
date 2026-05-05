import json
import os
import subprocess
from pathlib import Path
from dotenv import load_dotenv, set_key
from flask import Flask, request, jsonify
from plaid.model.link_token_create_request import LinkTokenCreateRequest
from plaid.model.link_token_create_request_user import LinkTokenCreateRequestUser
from plaid.model.item_public_token_exchange_request import ItemPublicTokenExchangeRequest
from plaid.model.products import Products
from plaid.model.country_code import CountryCode
import plaid_client as pc

SECRETS_FILE = Path.home() / ".config" / "paycheck-tracker" / "secrets.env"
load_dotenv(SECRETS_FILE)

with open(Path(__file__).parent / "config.json") as f:
    config = json.load(f)

env = config.get("plaid_env", "sandbox").lower()
client_api = pc.make_client(config)
token_key = "PLAID_ACCESS_TOKEN_PRODUCTION" if env == "production" else "PLAID_ACCESS_TOKEN_SANDBOX"

app = Flask(__name__)


def get_base_url():
    try:
        result = subprocess.run(
            ["tailscale", "status", "--json"],
            capture_output=True, text=True, timeout=5
        )
        data = json.loads(result.stdout)
        hostname = data["Self"]["DNSName"].rstrip(".")
        return f"https://{hostname}"
    except Exception:
        pass
    # fallback to LAN IP over HTTP (only works for sandbox)
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
    return f"http://{ip}:5000"


BASE_URL = get_base_url()
REDIRECT_URI = f"{BASE_URL}/oauth-redirect"

print(f"\n=== Plaid OAuth Setup ({env.upper()}) ===")
print(f"1. Start Tailscale Funnel in another terminal:")
print(f"     tailscale funnel 5000")
print(f"2. Register this redirect URI in your Plaid dashboard")
print(f"   (Team Settings → API → Allowed redirect URIs):")
print(f"     {REDIRECT_URI}")
print(f"3. Open this on your phone: {BASE_URL}\n")


@app.route("/")
def index():
    req = LinkTokenCreateRequest(
        user=LinkTokenCreateRequestUser(client_user_id="setup-user"),
        client_name="Paycheck Tracker",
        products=[Products("transactions")],
        country_codes=[CountryCode("US")],
        language="en",
        redirect_uri=REDIRECT_URI,
    )
    response = client_api.link_token_create(req)
    link_token = response["link_token"]
    return f"""<!DOCTYPE html><html><body>
<script src="https://cdn.plaid.com/link/v2/stable/link-initialize.js"></script>
<script>
var handler = Plaid.create({{
  token: '{link_token}',
  onSuccess: function(public_token, metadata) {{
    fetch('{BASE_URL}/exchange', {{method:'POST', headers:{{'Content-Type':'application/json'}},
      body: JSON.stringify({{public_token: public_token}})
    }});
  }},
  onExit: function(err, metadata) {{ console.log(err); }}
}});
handler.open();
</script>
<p>Opening Plaid Link...</p>
</body></html>"""


@app.route("/oauth-redirect")
def oauth_redirect():
    return f"""<!DOCTYPE html><html><body>
<script src="https://cdn.plaid.com/link/v2/stable/link-initialize.js"></script>
<script>
var handler = Plaid.create({{
  receivedRedirectUri: window.location.href,
  onSuccess: function(public_token, metadata) {{
    fetch('{BASE_URL}/exchange', {{method:'POST', headers:{{'Content-Type':'application/json'}},
      body: JSON.stringify({{public_token: public_token}})
    }});
  }},
  onExit: function(err, metadata) {{ console.log(err); }}
}});
handler.open();
</script>
</body></html>"""


@app.route("/exchange", methods=["POST"])
def exchange():
    public_token = request.json["public_token"]
    resp = client_api.item_public_token_exchange(
        ItemPublicTokenExchangeRequest(public_token=public_token)
    )
    token = resp["access_token"]
    set_key(str(SECRETS_FILE), token_key, token)
    print(f"\n✅ {token_key} saved to {SECRETS_FILE}")
    import threading
    threading.Timer(1.0, lambda: os._exit(0)).start()
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
