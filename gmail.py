import os
import pickle
import smtplib
import json
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.utils import parseaddr
from urllib.parse import quote_plus

from dotenv import load_dotenv
from openai import OpenAI

from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from google.auth.exceptions import RefreshError

# === Load Secrets ===
load_dotenv()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
FROM_EMAIL = os.getenv("FROM_EMAIL")
TO_EMAIL = os.getenv("TO_EMAIL")
APP_PSWD = os.getenv("APP_PSWD")
REPLY_LINK_MODE = os.getenv("REPLY_LINK_MODE", "gmail")
SCOPES = ['https://www.googleapis.com/auth/gmail.readonly']
TEST_MODE = os.getenv("TEST_MODE", "").lower() == "true"

client = OpenAI(api_key=OPENAI_API_KEY)

# === Auth ===
def gmail_auth():
    creds = None
    if os.path.exists('token.pkl'):
        with open('token.pkl', 'rb') as token:
            creds = pickle.load(token)
    if not creds or not creds.valid:
        if creds and creds.refresh_token:
            try:
                creds.refresh(Request())
            except RefreshError:
                flow = InstalledAppFlow.from_client_secrets_file('creds.json', SCOPES)
                creds = flow.run_local_server(port=0)
        else:
            flow = InstalledAppFlow.from_client_secrets_file('creds.json', SCOPES)
            creds = flow.run_local_server(port=0)
        with open('token.pkl', 'wb') as token:
            pickle.dump(creds, token)
    return build('gmail', 'v1', credentials=creds)

# === Reply Link Builder ===
def build_reply_link(to_addr: str, subject: str, body: str) -> str:
    mode = (REPLY_LINK_MODE or "gmail").lower()
    if mode == "gmail":
        return (
            f"https://mail.google.com/mail/?view=cm&fs=1"
            f"&to={quote_plus(to_addr)}"
            f"&su={quote_plus(subject)}"
            f"&body={quote_plus(body)}"
        )
    if mode in ("outlook_office", "outlook365", "owa"):
        return (
            f"https://outlook.office.com/mail/deeplink/compose"
            f"?to={quote_plus(to_addr)}&subject={quote_plus(subject)}&body={quote_plus(body)}"
        )
    if mode in ("outlook_live", "outlook", "outlook_com"):
        return (
            f"https://outlook.live.com/owa/?path=/mail/action/compose"
            f"&to={quote_plus(to_addr)}&subject={quote_plus(subject)}&body={quote_plus(body)}"
        )
    return f"mailto:{quote_plus(to_addr)}?subject={quote_plus(subject)}&body={quote_plus(body)}"

# === Fetch Email Threads ===
def fetch_email_threads(service):
    now = datetime.utcnow()
    one_day_ago = now - timedelta(hours=24)
    cutoff_ms = int(one_day_ago.timestamp() * 1000)

    threads_result = service.users().threads().list(userId='me', q="", maxResults=20).execute()
    threads = threads_result.get('threads', [])
    thread_summaries = []

    for thread in threads:
        thread_id = thread['id']
        thread_data = service.users().threads().get(userId='me', id=thread_id, format='full').execute()
        messages = thread_data.get('messages', [])
        recent_messages = [m for m in messages if int(m.get('internalDate', 0)) >= cutoff_ms]
        if not recent_messages:
            continue

        all_senders = set()
        conversation = []
        for msg in recent_messages:
            headers = msg['payload']['headers']
            from_full = next((h['value'] for h in headers if h['name'] == 'From'), 'Unknown')
            all_senders.add(from_full)
            snippet = msg.get('snippet', '')
            conversation.append(f"{from_full} said: {snippet}")

        if all(sender.lower().startswith((FROM_EMAIL or '').lower()) for sender in all_senders):
            continue

        # Reply target: last message not from me, else last
        recent_messages_sorted = sorted(recent_messages, key=lambda m: int(m.get('internalDate', 0)))
        last_non_self_email = ''
        for m in reversed(recent_messages_sorted):
            headers_m = m['payload']['headers']
            from_full = next((h['value'] for h in headers_m if h['name'] == 'From'), 'Unknown')
            _, addr = parseaddr(from_full)
            if addr and (not FROM_EMAIL or addr.lower() != FROM_EMAIL.lower()):
                last_non_self_email = addr
                break
        reply_to = last_non_self_email or parseaddr(next((h['value'] for h in recent_messages_sorted[-1]['payload']['headers'] if h['name'] == 'From'), 'Unknown'))[1]

        headers_first = messages[0]['payload']['headers']
        subject = next((h['value'] for h in headers_first if h['name'] == 'Subject'), 'No Subject')
        sender = next((h['value'] for h in headers_first if h['name'] == 'From'), 'Unknown Sender')

        thread_summaries.append({
            "subject": subject,
            "sender": sender,
            "thread_text": "\n".join(conversation),
            "reply_to": reply_to
        })

    return thread_summaries

# === Single-call LLM: summary, action, up to 3 replies ===
def generate_digest_items(threads):
    processed = []
    for thread in threads:
        combined_prompt = (
            f"You are an executive assistant creating an email digest entry.\n"
            f"Given the email thread, produce a JSON object with: \n"
            f"- summary: 2–4 sentences summarizing the thread (professional, concise).\n"
            f"- action: one clear suggested action for the user.\n"
            f"- replies: an array with up to 3 objects, each with: \n"
            f"  - label: a single-word lowercase label suitable for a button (e.g., 'confirm', 'decline', 'schedule').\n"
            f"  - body: the full email reply text in first person, no quotes or signatures.\n"
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

        data = {}
        try:
            data = json.loads(response.output_text)
        except Exception:
            try:
                raw = response.output_text
                s = raw.find('{'); e = raw.rfind('}')
                if s != -1 and e != -1 and e > s:
                    data = json.loads(raw[s:e+1])
            except Exception:
                data = {}

        summary = (data.get('summary') or '').strip()
        action = (data.get('action') or '').strip()
        replies = data.get('replies') or []
        normalized_replies = []
        for r in replies[:3]:
            label = (r.get('label') or '').strip()
            label = label.replace(' ', '-').split('/')[0].lower() or 'reply'
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

        if TEST_MODE:
            try:
                print("\n=== TEST MODE: Processed Thread ===")
                print(json.dumps(processed[-1], indent=2, ensure_ascii=False))
            except Exception:
                print(processed[-1])

    return processed

# === HTML digest ===
def format_email_digest_html(summaries):
    today_str = datetime.now().strftime("%B %d, %Y")

    html = f"""
    <html>
    <head>
      <style>
        body {{ font-family: Arial, sans-serif; background-color: #f4f4f4; padding: 20px; }}
        .container {{ max-width: 700px; margin: auto; background-color: white; padding: 30px; border-radius: 10px; box-shadow: 0 2px 8px rgba(0,0,0,0.05); }}
        h1 {{ color: #333; }}
        .thread {{ margin-bottom: 30px; }}
        .summary-box {{ border-left: 4px solid #3498db; background-color: #f0f8ff; padding: 15px; border-radius: 6px; }}
        .summary-box .label {{ font-weight: bold; margin-top: 6px; }}
        .action-box {{ background-color: #fffbe6; border-left: 4px solid #f1c40f; padding: 15px; margin-top: 10px; border-radius: 6px; }}
        .action-box .label {{ font-weight: bold; color: #b37f00; margin-bottom: 6px; }}
        .reply-options {{ background-color: #eefaf1; border-left: 4px solid #2ecc71; padding: 15px; margin-top: 10px; border-radius: 6px; }}
        .reply-option {{ margin-top: 10px; padding-top: 10px; border-top: 1px dashed #bfe9cf; }}
        .reply-btn {{ display: inline-block; background-color: #2ecc71; color: white !important; text-decoration: none; padding: 8px 12px; border-radius: 4px; margin-right: 8px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.5px; }}
        .reply-body {{ margin-top: 8px; white-space: pre-wrap; }}
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
          <div class=\"summary-box\">\n            <div class=\"thread-title\">📌 Thread {i}: {thread['subject']}</div>\n            <div><strong>From:</strong> {thread['sender']}</div>\n            <div class=\"label\">📝 Summary:</div>\n            <div>{thread['summary']}</div>\n          </div>
          <div class=\"action-box\">\n            <div class=\"label\">⚡ Suggested Action:</div>\n            <div>{thread['action']}</div>\n          </div>
        """
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

# === Send Email via SMTP ===
def send_email(html_body):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"📬 Daily Email Digest – {datetime.now().strftime('%B %d, %Y')}"
    msg["From"] = FROM_EMAIL
    msg["To"] = TO_EMAIL

    msg.attach(MIMEText("Your email digest is attached as HTML.", "plain"))
    msg.attach(MIMEText(html_body, "html"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(FROM_EMAIL, APP_PSWD)
        server.sendmail(FROM_EMAIL, TO_EMAIL, msg.as_string())

    print("✅ Digest sent via Gmail SMTP!")

# === Main ===
def main():
    print("🔐 Authenticating Gmail...")
    service = gmail_auth()

    print("📬 Fetching recent email threads...")
    threads = fetch_email_threads(service)
    if not threads:
        print("📭 No recent threads found.")
        return

    print(f"🧠 Generating summaries, actions, and replies for {len(threads)} threads...")
    processed = generate_digest_items(threads)

    print("📤 Formatting digest...")
    html_body = format_email_digest_html(processed)

    print("📤 Sending email via Gmail SMTP...")
    send_email(html_body)

if __name__ == "__main__":
    main()
