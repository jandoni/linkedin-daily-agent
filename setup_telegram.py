#!/usr/bin/env python3
"""Send/update the pinned Telegram guide + register the bot command menu.
Reads TG_TOKEN and CHAT_ID from config.env (see config.example.env).

Usage:
  python3 setup_telegram.py            # first run: sends + pins the guide, sets commands
  python3 setup_telegram.py <msg_id>   # later: edits the existing pinned guide in place
"""
import json, os, sys, urllib.request, urllib.error

def load_config():
    here = os.path.dirname(os.path.abspath(__file__))
    p = os.path.join(here, "config.env")
    if os.path.exists(p):
        for line in open(p):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
load_config()

TOKEN = os.environ.get("TG_TOKEN", "")
CHAT = os.environ.get("CHAT_ID", "")
if not TOKEN or not CHAT:
    sys.exit("ERROR: TG_TOKEN / CHAT_ID missing. Fill in config.env (copy config.example.env).")
API = "https://api.telegram.org/bot" + TOKEN

GUIDE = (
"🤖 <b>Your LinkedIn Agent — how it works</b>\n\n"
"I find a relevant tech/AI story, write a post in your voice, design a branded image card, "
"and send it here for approval. <b>Nothing is ever posted without your OK.</b>\n\n"
"<b>🗓 When</b>\n"
"3× a day (7am, 12pm, 6pm CT) I send you a <b>list of recent headlines</b> — paste back the one you like and I'll draft the post. You can also draft one anytime with /new, or get the list anytime with /headlines.\n\n"
"<b>✅ Approving a draft</b>\n"
"Each draft arrives as a preview image + the post text + buttons. Tap 👍 <b>Like it</b>, then choose:\n"
"• ✅ <b>Publish now</b> → posts it immediately\n"
"• 📝 <b>Save as draft</b> → drops it into your LinkedIn drafts so you can add @tags and schedule or publish it yourself\n"
"• 🗑 <b>Discard</b> / 👎 <b>Skip</b> → throws it away\n"
"• 🔄 <b>Different news</b> → picks a fresh story and rebuilds the draft\n"
"• 🎨 <b>Other style</b> → keeps the same post but redesigns the image in a different template\n\n"
"<b>📰 Browse recent headlines</b>\n"
"Send <b>/headlines</b> (or /top) and I'll list ~10 recent tech &amp; AI stories with links. Tap <b>➡️ Show 10 more</b> to page through the rest. Paste one back to me and I'll draft a post about it.\n\n"
"<b>✍️ Want changes? Just type them.</b>\n"
"Reply with a note and I'll rewrite the post + card and send a fresh preview.\n\n"
"<b>💡 Write about something specific</b>\n"
"• <b>Paste any article link</b> → I'll write an original post in your voice about it. (LinkedIn links are not opened: paste the original article, or /new plus the post text.)\n"
"• <b>/new your topic</b> → post about that topic.\n"
"• <b>/new</b> (alone) → pick a fresh story from today's news.\n\n"
"<b>🖼 Use your own image</b>\n"
"Send a <b>photo</b> (no caption) and I'll rebuild the current draft's card with your photo as the <b>background</b> — the headline box and badge stay on top.\n\n"
"<b>🎨 Make a title card from your photo</b>\n"
"Send a <b>photo with a caption</b> and I'll turn it into a branded card: your photo as the background, your caption as the headline, in the same designs as my cards. Tap 🎨 <b>Other style</b> to switch between the two templates, then long-press the image to save or share it. (This doesn't touch your current draft.)\n\n"
"<b>🎥 Post a video</b>\n"
"Send a <b>video</b> and I'll add your watermark and post it as native LinkedIn video.\n"
"⚠️ <b>Size limit: under ~20 MB</b> (Telegram's bot limit).\n\n"
"<b>⌨️ Commands</b>\n"
"/new — draft a new post now (add a topic, or leave blank for news)\n"
"/headlines — list recent tech &amp; AI headlines\n"
"/help — show this guide\n\n"
"Everything runs privately on your home server. 🔒"
)

def call(method, payload):
    req = urllib.request.Request(API + "/" + method, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        return json.load(urllib.request.urlopen(req, timeout=20))
    except urllib.error.HTTPError as e:
        return {"ok": False, "error": e.read().decode()}

if len(sys.argv) > 1:   # edit existing pinned guide in place
    mid = int(sys.argv[1])
    r = call("editMessageText", {"chat_id": CHAT, "message_id": mid, "text": GUIDE,
                                 "parse_mode": "HTML", "disable_web_page_preview": True})
    print("editMessageText ok =", r.get("ok"), "" if r.get("ok") else r)
else:                    # first run: send + pin
    r = call("sendMessage", {"chat_id": CHAT, "text": GUIDE, "parse_mode": "HTML",
                             "disable_web_page_preview": True})
    if r.get("ok"):
        mid = r["result"]["message_id"]
        print("sent guide, message_id =", mid, "-> pass this id on future runs to edit in place")
        print("pinned:", call("pinChatMessage", {"chat_id": CHAT, "message_id": mid,
                                                  "disable_notification": True}).get("ok"))
    else:
        print("sendMessage failed:", r)

c = call("setMyCommands", {"commands": [
    {"command": "new", "description": "Draft a new LinkedIn post now"},
    {"command": "headlines", "description": "List recent tech & AI headlines"},
    {"command": "help", "description": "How this bot works"},
]})
print("setMyCommands ok =", c.get("ok"), "" if c.get("ok") else c)
