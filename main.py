import os
import json
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import quote_plus, quote
from email.utils import parseaddr
from dotenv import load_dotenv
from openai import OpenAI

# === Load Secrets ===
# Load base .env first
load_dotenv()

# Check for USER_PROFILE environment variable first (for easy switching)
user_profile = os.getenv("USER_PROFILE")
if user_profile:
    print(f"👤 Using USER_PROFILE: {user_profile}")
    # Load user-specific .env file
    user_env_file = f".env.{user_profile}"
    if os.path.exists(user_env_file):
        load_dotenv(user_env_file, override=True)
        print(f"⚙️ Loaded user profile: {user_profile} ({os.path.abspath(user_env_file)})")
    else:
        print(f"⚠️ Warning: User profile file {user_env_file} not found. Using base .env")
else:
    # Fallback to old behavior for backward compatibility
    _env_profile = (os.getenv("MSAL_PROFILE") or "").strip()
    if not _env_profile:
        _fe = (os.getenv("FROM_EMAIL") or "").strip()
        if _fe:
            _env_profile = re.sub(r"[^A-Za-z0-9_.-]+", "_", _fe.split("@")[0])
    # Load profile-specific overrides if present
    if _env_profile:
        _env_file = f".env.{_env_profile}"
        if os.path.exists(_env_file):
            load_dotenv(_env_file, override=True)
            try:
                print(f"⚙️ Loaded env profile: {_env_profile} ({os.path.abspath(_env_file)})")
            except Exception:
                pass

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
FROM_EMAIL = os.getenv("FROM_EMAIL") or ""
TO_EMAIL = os.getenv("TO_EMAIL")
APP_PSWD = os.getenv("APP_PSWD")
GRAPH_SCOPES = ['User.Read', 'Mail.Read', 'Mail.Send']
REPLY_LINK_MODE = os.getenv("REPLY_LINK_MODE", "outlook_office")
EMAIL_FORMAT = os.getenv("EMAIL_FORMAT", "modern").lower()

client = OpenAI(api_key=OPENAI_API_KEY)

# === Microsoft Graph Auth ===
def graph_auth():
    tenant_id = os.getenv('AZURE_TENANT_ID')
    client_id = os.getenv('AZURE_CLIENT_ID')
    if not tenant_id or not client_id:
        raise RuntimeError('Missing AZURE_TENANT_ID or AZURE_CLIENT_ID in environment')

    # Choose token cache file based on profile or explicit path
    # Priority: USER_PROFILE > MSAL_PROFILE > FROM_EMAIL
    user_profile = (os.getenv('USER_PROFILE') or '').strip()
    profile = (os.getenv('MSAL_PROFILE') or '').strip()
    explicit_cache = (os.getenv('MSAL_CACHE_FILE') or '').strip()
    cache_dir = os.getenv('MSAL_CACHE_DIR', '.')  # Support custom cache directory for cloud
    
    if explicit_cache:
        cache_file = explicit_cache
    elif user_profile:
        # Use USER_PROFILE for cache file naming
        safe_profile = re.sub(r'[^A-Za-z0-9_.-]+', '_', user_profile)
        cache_file = os.path.join(cache_dir, f"msal_token_cache_{safe_profile}.bin")
        profile = safe_profile
    else:
        derived_profile = profile
        if not derived_profile:
            fe = (os.getenv('FROM_EMAIL') or '').strip()
            if fe:
                derived_profile = re.sub(r'[^A-Za-z0-9_.-]+', '_', fe.split('@')[0])
        if derived_profile:
            safe_profile = re.sub(r'[^A-Za-z0-9_.-]+', '_', derived_profile)
            cache_file = os.path.join(cache_dir, f"msal_token_cache_{safe_profile}.bin")
            profile = safe_profile
        else:
            cache_file = os.path.join(cache_dir, 'msal_token_cache.bin')

    try:
        import msal
        from msal import SerializableTokenCache
    except Exception as e:
        raise RuntimeError('msal is required. Install with: pip install msal') from e

    print(f"🆔 Using MSAL profile: {profile or 'default'}")
    print(f"💾 Token cache file: {os.path.abspath(cache_file)}")

    token_cache = SerializableTokenCache()
    if os.path.exists(cache_file):
        with open(cache_file, 'r') as f:
            token_cache.deserialize(f.read())

    authority = f"https://login.microsoftonline.com/{tenant_id}"
    app = msal.PublicClientApplication(client_id=client_id, authority=authority, token_cache=token_cache)

    accounts = app.get_accounts()
    result = None
    if accounts:
        result = app.acquire_token_silent(GRAPH_SCOPES, account=accounts[0])

    if not result:
        flow = app.initiate_device_flow(scopes=GRAPH_SCOPES)
        if 'user_code' not in flow:
            raise RuntimeError('Failed to create device flow for Microsoft Graph')
        print(flow['message'])
        result = app.acquire_token_by_device_flow(flow)

    with open(cache_file, 'w') as f:
        f.write(token_cache.serialize())

    if 'access_token' not in result:
        raise RuntimeError(f"Authentication failed: {result}")

    import requests
    session = requests.Session()
    session.headers.update({'Authorization': f"Bearer {result['access_token']}", 'Accept': 'application/json'})
    return session

# === Graph helper with pagination (CHANGED) ===
def graph_get_all(session, url, params=None):
    items = []
    while True:
        resp = session.get(url, params=params) if params else session.get(url)
        resp.raise_for_status()
        data = resp.json()
        items.extend(data.get('value', []))
        next_link = data.get('@odata.nextLink')
        if not next_link:
            break
        url, params = next_link, None
    return items

# === Email address normalizer (CHANGED) ===
def _addr_only(obj):
    if not obj:
        return ""
    email_obj = obj.get('emailAddress') or {}
    _, addr = parseaddr(f"{email_obj.get('name', '')} <{email_obj.get('address', '')}>")
    return (addr or "").lower()

# === Fetch Email Threads with full conversation expansion (CHANGED) ===
def fetch_email_threads(session, hours_back=24, custom_range=None):
    now = datetime.now(timezone.utc)
    
    # Support custom time ranges for cron jobs
    if custom_range:
        if custom_range == "morning":  # 2pm yesterday to 9am today
            # When running at 9am, we want emails from 2pm yesterday to 9am today
            today_9am = now.replace(hour=9, minute=0, second=0, microsecond=0)
            yesterday_2pm = (now - timedelta(days=1)).replace(hour=14, minute=0, second=0, microsecond=0)
            cutoff = yesterday_2pm.isoformat().replace("+00:00", "Z")
            end_time = today_9am.isoformat().replace("+00:00", "Z")
        elif custom_range == "afternoon":  # 9am to 2pm today
            today_9am = now.replace(hour=9, minute=0, second=0, microsecond=0)
            today_2pm = now.replace(hour=14, minute=0, second=0, microsecond=0)
            cutoff = today_9am.isoformat().replace("+00:00", "Z")
            end_time = today_2pm.isoformat().replace("+00:00", "Z")
    else:
        cutoff = (now - timedelta(hours=hours_back)).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        end_time = None

    print(f"📧 Fetching messages since: {cutoff}")
    if end_time:
        print(f"📧 Until: {end_time}")
        filter_clause = f"receivedDateTime ge {cutoff} and receivedDateTime lt {end_time}"
    else:
        filter_clause = f"receivedDateTime ge {cutoff}"
    
    params = {
        "$select": "id,subject,from,sender,replyTo,receivedDateTime,bodyPreview,conversationId",
        "$filter": filter_clause,
        "$orderby": "receivedDateTime desc",
        "$top": "500"
    }
    inbox_url = "https://graph.microsoft.com/v1.0/me/mailFolders/Inbox/messages"
    recent_msgs = graph_get_all(session, inbox_url, params=params)
    print(f"📨 Found {len(recent_msgs)} recent messages")

    # Show the first few messages for debugging
    for msg in recent_msgs[:5]:
        print(f"  - Subject: {msg.get('subject', 'N/A')}")
        print(f"    From: {_addr_only(msg.get('from'))}")
        print(f"    ConvID: {msg.get('conversationId', 'N/A')}")
        print(f"    Time: {msg.get('receivedDateTime', 'N/A')}")
        print()

    # Group messages by conversationId
    conv_groups = {}
    for m in recent_msgs:
        cid = m.get('conversationId')
        if not cid:
            continue
        conv_groups.setdefault(cid, []).append(m)

    print(f"🔗 Found {len(conv_groups)} unique conversation IDs")

    threads = []
    for i, (cid, msgs) in enumerate(conv_groups.items(), 1):
        print(f"\n🧵 Processing conversation {i}/{len(conv_groups)}: {cid}")
        msgs.sort(key=lambda x: x.get("receivedDateTime", ""))  # chronological order

        print(f"  📊 Found {len(msgs)} messages in this conversation")
        participants = set()
        conversation_lines = []

        for m in msgs:
            addr = _addr_only(m.get('from'))
            subject = m.get('subject', 'N/A')
            time = m.get('receivedDateTime', 'N/A')
            snippet = (m.get('bodyPreview') or '').strip()[:100]

            print(f"    - From: {addr}")
            print(f"      Subject: {subject}")
            print(f"      Time: {time}")
            print(f"      Preview: {snippet}...")

            # Don't skip any messages since FROM_EMAIL and TO_EMAIL are the same
            participants.add(addr)
            conversation_lines.append(f"{addr or 'unknown'} said: {(m.get('bodyPreview') or '').strip()}")

        if not conversation_lines:
            print("  ❌ No non-bot messages found, skipping thread")
            continue

        print(f"  ✅ Thread has {len(conversation_lines)} non-bot messages from {len(participants)} participants")

        # Determine reply_to
        reply_to = ""
        for m in reversed(msgs):
            candidates = (m.get('replyTo') or [], [m.get('from')], [m.get('sender')])
            for cand in candidates:
                if isinstance(cand, list):
                    if not cand:
                        continue
                    obj = cand[0]
                else:
                    obj = cand
                addr = _addr_only(obj)
                if addr:
                    reply_to = addr
                    break
            if reply_to:
                break

        first = msgs[0]
        subject = (first.get('subject') or 'No Subject').strip()
        
        # Skip threads that are email digests
        if subject.startswith('📬 Daily Email Digest –'):
            print(f"  ⏭️  Skipping thread: '{subject}' (email digest)")
            continue
            
        display_from = first.get('from') or {}
        ea = display_from.get('emailAddress') or {}
        sender_display = f"{ea.get('name') or ''} <{ea.get('address') or ''}>".strip()

        threads.append({
            "subject": subject,
            "sender": sender_display,
            "thread_text": "\n".join(conversation_lines),
            "reply_to": reply_to
        })

        print(f"  📝 Added thread: '{subject}' with reply_to: {reply_to}")

    print(f"\n📋 Total threads created: {len(threads)}")
    return threads



# === Reply Link Builder ===
def build_reply_link(to_addr: str, subject: str, body: str) -> str:
    mode = (REPLY_LINK_MODE or "mailto").lower()
    if mode == "gmail":
        return f"https://mail.google.com/mail/?view=cm&fs=1&to={quote_plus(to_addr)}&su={quote_plus(subject)}&body={quote_plus(body)}"
    if mode in ("outlook_office", "outlook365", "owa"):
        # popoutv2=0 forces inline compose (slides up from bottom)
        # popoutv2=1 would open in a popup window
        return (
            f"https://outlook.office.com/mail/0/deeplink/compose"
            f"?popoutv2=0"
            f"&to={quote_plus(to_addr)}"
            f"&subject={quote_plus(subject)}"
            f"&body={quote_plus(body)}"
        )
    if mode in ("outlook_live", "outlook_com", "outlook"):
        return f"https://outlook.live.com/owa/?path=/mail/action/compose&to={quote_plus(to_addr)}&subject={quote_plus(subject)}&body={quote_plus(body)}"
    # Use quote() instead of quote_plus() for mailto links to avoid + signs
    return f"mailto:{quote(to_addr)}?subject={quote(subject)}&body={quote(body)}"


# === Summarize and suggest actions ===
def summarize_and_action(threads):
    processed = []
    for thread in threads:
        combined_prompt = (
            f"You are an executive assistant creating an email digest entry.\n"
            f"Given the email thread, produce a JSON object with: \n"
            f"- summary: 2–4 sentences summarizing the thread (professional, concise).\n"
            f"- action: one clear suggested action for the user.\n"
            f"- replies: an array with up to 3 objects, each with: \n"
            f"  - label: single-word lowercase label for a button.\n"
            f"  - body: 2–6 sentences, first person, no quotes/signatures.\n"
            f"Return ONLY valid JSON.\n\n"
            f"From: {thread['sender']}\n"
            f"Subject: {thread['subject']}\n"
            f"Conversation:\n{thread['thread_text']}"
        )

        response = client.responses.create(
            model="gpt-5",
            input=combined_prompt,
            text={
                "format": {
                    "type": "json_schema",
                    "name": "digest_schema",
                    "strict": True,
                    "schema": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["summary", "action", "replies"],
                        "properties": {
                            "summary": {"type": "string"},
                            "action": {"type": "string"},
                            "replies": {
                                "type": "array",
                                "maxItems": 3,
                                "items": {
                                    "type": "object",
                                    "additionalProperties": False,
                                    "required": ["label", "body"],
                                    "properties": {
                                        "label": {"type": "string"},
                                        "body": {"type": "string"}
                                    }
                                }
                            }
                        }
                    }
                }
            }
        )

        raw_text = response.output_text
        try:
            data = json.loads(raw_text)
        except Exception:
            try:
                start = raw_text.find('{')
                end = raw_text.rfind('}')
                data = json.loads(raw_text[start:end+1]) if start != -1 and end != -1 else {}
            except Exception:
                data = {}

        summary = (data.get('summary') or '').strip()
        action = (data.get('action') or '').strip()
        replies = data.get('replies') or []
        normalized_replies = []
        for r in replies[:3]:
            label = (r.get('label') or '').strip().replace(' ', '-').split('/')[0].lower() or 'reply'
            body = (r.get('body') or '').strip()
            if body:
                normalized_replies.append({"label": label, "body": body})

        processed.append({
            "sender": thread['sender'],
            "subject": thread['subject'],
            "summary": summary,
            "action": action,
            "replies": normalized_replies,
            "reply_to": thread.get('reply_to', '')
        })
    return processed

# === Format digest HTML for Windows Outlook ===
def format_email_digest_html_windows(summaries):
	today_str = datetime.now().strftime("%B %d, %Y")
	
	html = f"""
	<html>
	<head>
	</head>
	<body style="font-family: Arial, sans-serif; margin: 0; padding: 0;">
	<table width="100%" cellpadding="0" cellspacing="0" border="0" style="background-color: #f4f4f4;">
	<tr>
	<td align="center" style="padding: 20px;">
		<table width="700" cellpadding="0" cellspacing="0" border="0" style="background-color: white;">
		<tr>
		<td style="padding: 30px;">
			<h1 style="color: #333; font-size: 24px; margin: 0 0 20px 0;">📬 Email Digest – {today_str}</h1>
			<p style="margin: 0 0 20px 0;">Here's a summary of your recent conversations:</p>
	"""

	for i, thread in enumerate(summaries, 1):
		subject_reply = thread['subject'] if thread['subject'].lower().startswith('re:') else f"Re: {thread['subject']}"
		
		# Thread container
		html += f"""
			<table width="100%" cellpadding="0" cellspacing="0" border="0" style="margin-bottom: 30px;">
			<tr>
			<td>
				<!-- Summary Box -->
				<table width="100%" cellpadding="0" cellspacing="0" border="0" style="border-left: 4px solid #3498db; background-color: #f0f8ff;">
				<tr>
				<td style="padding: 15px;">
					<div style="font-weight: bold; margin-bottom: 10px; font-size: 16px;">📌 Thread {i}: {thread['subject']}</div>
					<div style="margin-bottom: 10px;"><strong>From:</strong> {thread['sender']}</div>
					<div style="font-weight: bold; margin-bottom: 6px;">📝 Summary:</div>
					<div>{thread['summary']}</div>
				</td>
				</tr>
				</table>
				
				<!-- Spacer row -->
				<table width="100%" cellpadding="0" cellspacing="0" border="0">
				<tr><td style="height: 10px;"></td></tr>
				</table>
				
				<!-- Action Box -->
				<table width="100%" cellpadding="0" cellspacing="0" border="0" style="border-left: 4px solid #f1c40f; background-color: #fffbe6;">
				<tr>
				<td style="padding: 15px;">
					<div style="font-weight: bold; color: #b37f00; margin-bottom: 6px;">⚡ Suggested Action:</div>
					<div>{thread['action']}</div>
				</td>
				</tr>
				</table>
		"""

		# Reply options
		if thread.get('replies'):
			html += """
				<!-- Spacer row -->
				<table width="100%" cellpadding="0" cellspacing="0" border="0">
				<tr><td style="height: 10px;"></td></tr>
				</table>
				
				<!-- Reply Options -->
				<table width="100%" cellpadding="0" cellspacing="0" border="0" style="border-left: 4px solid #2ecc71; background-color: #eefaf1;">
				<tr>
				<td style="padding: 15px;">
					<div style="font-weight: bold; margin-bottom: 15px;">💬 AI Reply Options:</div>
			"""
			
			for idx, opt in enumerate(thread['replies']):
				body_text = opt['body']
				reply_link = "#"
				if thread.get('reply_to'):
					reply_link = build_reply_link(thread['reply_to'], subject_reply, body_text)
				
				# Add separator between options except for the first one
				separator_style = "margin-top: 15px; padding-top: 15px; border-top: 1px solid #d4f1df;" if idx > 0 else ""
				
				# Calculate button width - be more generous for uppercase + letter spacing
				# 14px font, uppercase ~10px per char, letter-spacing adds ~1px per char, plus padding
				button_width = max(150, len(opt['label']) * 15 + 60)
				
				# Rounded button using VML for Outlook
				html += f"""
					<div style="{separator_style}">
						<!--[if mso]>
						<v:roundrect xmlns:v="urn:schemas-microsoft-com:vml" xmlns:w="urn:schemas-microsoft-com:office:word" href="{reply_link}" style="height:40px;v-text-anchor:middle;width:{button_width}px;" arcsize="15%" stroke="f" fillcolor="#2ecc71">
						<w:anchorlock/>
						<center style="color:#ffffff;font-family:Arial,sans-serif;font-size:14px;font-weight:bold;text-transform:uppercase;letter-spacing:0.5px;">
						<![endif]-->
						<a href="{reply_link}" style="background-color:#2ecc71;border-radius:6px;color:#ffffff;display:inline-block;font-family:Arial,sans-serif;font-size:14px;font-weight:bold;line-height:40px;text-align:center;text-decoration:none;padding:0 30px;text-transform:uppercase;letter-spacing:0.5px;-webkit-text-size-adjust:none;mso-hide:all;">{opt['label']}</a>
						<!--[if mso]>
						{opt['label'].upper()}
						</center>
						</v:roundrect>
						<![endif]-->
						<div style="margin-top: 10px; color: #333; line-height: 1.4;">{body_text.replace('<', '&lt;').replace('>', '&gt;')}</div>
					</div>
				"""
			
			html += """
				</td>
				</tr>
				</table>
			"""
		
		html += """
			</td>
			</tr>
			</table>
			
			<!-- Large spacer between threads -->
			<table width="100%" cellpadding="0" cellspacing="0" border="0">
			<tr><td style="height: 25px;"></td></tr>
			</table>
		"""

	html += """
		</td>
		</tr>
		</table>
	</td>
	</tr>
	</table>
	</body>
	</html>
	"""

	return html

# === Format digest HTML ===
def format_email_digest_html(summaries):
	# Restore styled, structured HTML digest while keeping reply buttons
	today_str = datetime.now().strftime("%B %d, %Y")

	# Check if we need Windows-compatible HTML
	if EMAIL_FORMAT == "windows":
		return format_email_digest_html_windows(summaries)

	html = f"""
	<html>
	<head>
	  <style>
		body {{
		  font-family: Arial, sans-serif;
		  background-color: #f4f4f4;
		  padding: 20px;
		}}
		.container {{
		  max-width: 700px;
		  margin: auto;
		  background-color: white;
		  padding: 30px;
		  border-radius: 10px;
		  box-shadow: 0 2px 8px rgba(0,0,0,0.05);
		}}
		h1 {{
		  color: #333;
		}}
		.thread {{
		  margin-bottom: 30px;
		}}
		.summary-box {{
		  border-left: 4px solid #3498db;
		  background-color: #f0f8ff;
		  padding: 15px;
		  border-radius: 6px;
		}}
		.summary-box .label {{
		  font-weight: bold;
		  margin-top: 6px;
		}}
		.action-box {{
		  background-color: #fffbe6;
		  border-left: 4px solid #f1c40f;
		  padding: 15px;
		  margin-top: 10px;
		  border-radius: 6px;
		}}
		.action-box .label {{
		  font-weight: bold;
		  color: #b37f00;
		  margin-bottom: 6px;
		}}
		.reply-options {{
		  background-color: #eefaf1;
		  border-left: 4px solid #2ecc71;
		  padding: 15px;
		  margin-top: 10px;
		  border-radius: 6px;
		}}
		.reply-option {{
		  margin-top: 10px;
		  padding-top: 10px;
		  border-top: 1px dashed #bfe9cf;
		}}
		.reply-btn {{
		  display: inline-block;
		  background-color: #2ecc71;
		  color: white !important;
		  text-decoration: none;
		  padding: 8px 12px;
		  border-radius: 4px;
		  margin-right: 8px;
		  font-weight: 600;
		  text-transform: uppercase;
		  letter-spacing: 0.5px;
		}}
		.reply-body {{
		  margin-top: 8px;
		  white-space: pre-wrap;
		}}
	  </style>
	</head>
	<body>
	  <div class="container">
		<h1>📬 Email Digest – {today_str}</h1>
		<p>Here's a summary of your recent conversations:</p>
	"""

	for i, thread in enumerate(summaries, 1):
		subject_reply = thread['subject'] if thread['subject'].lower().startswith('re:') else f"Re: {thread['subject']}"
		html += f"""
		<div class=\"thread\">
		  <div class=\"summary-box\">\n			<div class=\"thread-title\">📌 Thread {i}: {thread['subject']}</div>\n			<div><strong>From:</strong> {thread['sender']}</div>\n			<div class=\"label\">📝 Summary:</div>\n			<div>{thread['summary']}</div>\n		  </div>
		  <div class=\"action-box\">\n			<div class=\"label\">⚡ Suggested Action:</div>\n			<div>{thread['action']}</div>\n		  </div>
		"""

		# Render up to 3 reply options
		if thread.get('replies'):
			html += "<div class=\"reply-options\"><div class=\"label\">💬 AI Reply Options:</div>"
			for opt in thread['replies']:
				body_text = opt['body']
				reply_link = "#"
				if thread.get('reply_to'):
					reply_link = build_reply_link(thread['reply_to'], subject_reply, body_text)
				html += (
					f"<div class=\"reply-option\">"
					f"<a class=\"reply-btn\" href=\"{reply_link}\">{opt['label']}</a>"
					f"<div class=\"reply-body\">{body_text.replace('<', '&lt;').replace('>', '&gt;')}</div>"
					f"</div>"
				)
			html += "</div>"

		html += "</div>"

	html += """
	  </div>
	</body>
	</html>
	"""

	return html

# === Send Email via Microsoft Graph ===
def send_email_via_graph(session, html_body, subject):
    """Send email using Microsoft Graph API"""
    
    print(f"📧 Sending email from: {FROM_EMAIL}")
    print(f"📬 Sending email to: {TO_EMAIL}")
    print(f"📋 Subject: {subject}")
    
    # Prepare the email message
    message = {
        "subject": subject,
        "body": {
            "contentType": "HTML",
            "content": html_body
        },
        "toRecipients": [
            {
                "emailAddress": {
                    "address": TO_EMAIL
                }
            }
        ]
    }
    
    # Send the email
    try:
        response = session.post(
            "https://graph.microsoft.com/v1.0/me/sendMail",
            json={"message": message}
        )
        response.raise_for_status()
        print("✅ Digest sent via Microsoft Graph!")
    except Exception as e:
        print(f"❌ Failed to send email: {e}")
        if hasattr(e, 'response') and e.response is not None:
            print(f"Response: {e.response.text}")

# === Main ===
def main():
    import sys
    
    # Check for command line arguments
    time_range = sys.argv[1] if len(sys.argv) > 1 else None
    
    print("🔐 Authenticating Outlook (Microsoft Graph)...")
    session = graph_auth()
    print("📬 Fetching recent email threads...")
    
    if time_range in ["morning", "afternoon"]:
        threads = fetch_email_threads(session, custom_range=time_range)
        period = "Morning" if time_range == "morning" else "Afternoon"
    else:
        threads = fetch_email_threads(session)
        period = "Daily"
    
    if not threads:
        print("📭 No recent threads found.")
        return
    print(f"🧠 Generating summaries and actions for {len(threads)} threads...")
    processed = summarize_and_action(threads)
    print("📤 Formatting and sending email...")
    html_body = format_email_digest_html(processed)
    send_email_via_graph(session, html_body, f"📬 {period} Email Digest – {datetime.now().strftime('%B %d, %Y')}")


# Import Render-specific modifications if running on Render
if os.getenv('RENDER'):
    from main_render import graph_auth_render
    graph_auth = graph_auth_render

if __name__ == "__main__":
    main()
