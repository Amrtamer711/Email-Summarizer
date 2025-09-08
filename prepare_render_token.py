#!/usr/bin/env python3
import base64
import os
import sys

def prepare_token_for_render(profile="jawad"):
    """Convert token cache to base64 for Render environment variable"""
    
    token_file = f"msal_token_cache_{profile}.bin"
    
    if not os.path.exists(token_file):
        print(f"❌ Token file not found: {token_file}")
        print(f"Please run: USER_PROFILE={profile} python main.py")
        return
    
    with open(token_file, 'rb') as f:
        token_data = f.read()
    
    b64_token = base64.b64encode(token_data).decode('utf-8')
    
    print(f"✅ Token prepared for {profile}")
    print("\n📋 Add this to Render environment variables:")
    print(f"\nMSAL_TOKEN_CACHE_BASE64={b64_token}\n")
    
    # Save to file for easy copying
    with open(f"render_token_{profile}.txt", 'w') as f:
        f.write(f"MSAL_TOKEN_CACHE_BASE64={b64_token}")
    
    print(f"💾 Also saved to: render_token_{profile}.txt")

if __name__ == "__main__":
    profile = sys.argv[1] if len(sys.argv) > 1 else "jawad"
    prepare_token_for_render(profile)