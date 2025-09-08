import os
import base64
from main import *

def graph_auth_render():
    """Modified graph_auth for Render deployment with secret files"""
    tenant_id = os.getenv('AZURE_TENANT_ID')
    client_id = os.getenv('AZURE_CLIENT_ID')
    if not tenant_id or not client_id:
        raise RuntimeError('Missing AZURE_TENANT_ID or AZURE_CLIENT_ID in environment')

    user_profile = (os.getenv('USER_PROFILE') or '').strip()
    profile = user_profile or 'default'
    safe_profile = re.sub(r'[^A-Za-z0-9_.-]+', '_', profile)
    
    try:
        import msal
        from msal import SerializableTokenCache
    except Exception as e:
        raise RuntimeError('msal is required. Install with: pip install msal') from e

    print(f"🆔 Using MSAL profile: {profile}")

    # Look for token cache in Render secret files directory
    cache_dir = os.getenv('MSAL_CACHE_DIR', '/etc/secrets')
    cache_file = os.path.join(cache_dir, f"msal_token_cache_{safe_profile}.bin")
    
    print(f"💾 Looking for token cache at: {cache_file}")
    
    token_cache = SerializableTokenCache()
    
    # Try to load token from secret file
    if os.path.exists(cache_file):
        try:
            with open(cache_file, 'r') as f:
                token_cache.deserialize(f.read())
            print("✅ Loaded token cache from secret file")
        except Exception as e:
            print(f"⚠️ Failed to load token cache: {e}")
    else:
        # Fallback to base64 environment variable
        token_cache_b64 = os.getenv('MSAL_TOKEN_CACHE_BASE64')
        if token_cache_b64:
            try:
                token_data = base64.b64decode(token_cache_b64)
                token_cache.deserialize(token_data.decode('utf-8'))
                print("✅ Loaded token cache from environment variable")
            except Exception as e:
                print(f"⚠️ Failed to load token cache from env: {e}")
    
    authority = f"https://login.microsoftonline.com/{tenant_id}"
    app = msal.PublicClientApplication(client_id=client_id, authority=authority, token_cache=token_cache)

    accounts = app.get_accounts()
    result = None
    if accounts:
        result = app.acquire_token_silent(GRAPH_SCOPES, account=accounts[0])

    if not result:
        # In production, we can't do interactive auth
        raise RuntimeError("No valid token found. Please upload msal_token_cache_jawad.bin to Render secret files")

    if 'access_token' not in result:
        raise RuntimeError(f"Authentication failed: {result}")

    import requests
    session = requests.Session()
    session.headers.update({'Authorization': f"Bearer {result['access_token']}", 'Accept': 'application/json'})
    return session

# Override the graph_auth function for Render
if os.getenv('RENDER'):
    graph_auth = graph_auth_render