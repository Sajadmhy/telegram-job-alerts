#!/usr/bin/env python3
"""
Telegram job alerts.

Watches job channels and sends you a Telegram bot message whenever a post
looks like a fit (web dev, AI, programming, ...).

Why two pieces:
  * A Telegram *bot* can't read channels it isn't an admin of, so reading is
    done with your own account (Telethon "user client", read-only polling -
    it never joins channels, posts, or marks anything as read).
  * Notifications are sent by your bot (BotFather token) so they arrive as a
    normal chat with pings. If no bot token is set, they go to Saved Messages.

Commands:
  python job_alert.py discover   # read the chat with Mobina, find channels -> channels.json
  python job_alert.py chat-id    # after messaging your bot once, prints your chat id
  python job_alert.py test       # run the filter on the last 30 posts of each channel (no alerts sent)
  python job_alert.py run        # watch forever and notify
  python job_alert.py once       # check once, notify, save state, exit (used by GitHub Actions)
  python job_alert.py export     # make a separate login for GitHub -> github_secret.txt
"""
import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path

import requests
from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.types import Channel, InputPeerChannel

HERE = Path(__file__).resolve().parent
load_dotenv(HERE / ".env")

API_ID = int(os.getenv("TG_API_ID", "0"))
API_HASH = os.getenv("TG_API_HASH", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
NOTIFY_CHAT_ID = os.getenv("NOTIFY_CHAT_ID", "").strip()
SOURCE_CHAT = os.getenv("SOURCE_CHAT", "Mobina")          # chat to discover channels from
DISCOVER_LIMIT = int(os.getenv("DISCOVER_LIMIT", "200"))  # how many recent messages to scan
POLL_MINUTES = float(os.getenv("POLL_MINUTES", "5"))
MIN_SCORE = int(os.getenv("MIN_SCORE", "2"))
PROXY = os.getenv("PROXY", "").strip()                    # e.g. socks5://127.0.0.1:1080
TG_SESSION = os.getenv("TG_SESSION", "").strip()          # string login (used on GitHub)

CHANNELS_FILE = HERE / "channels.json"
STATE_FILE = HERE / "state.json"

# --------------------------------------------------------------------------
# Relevance filter. Weight 3 = strong signal, 2 = good, 1 = weak on its own.
# Edit freely. Matching is case-insensitive; English terms use word boundaries.
# --------------------------------------------------------------------------
POSITIVE = {
    # web
    "web developer": 3, "web development": 3, "frontend": 3, "front-end": 3, "front end": 3,
    "backend": 3, "back-end": 3, "back end": 3, "full stack": 3, "fullstack": 3, "full-stack": 3,
    "react": 3, "next.js": 3, "nextjs": 3, "vue": 3, "angular": 3, "svelte": 3, "node.js": 3,
    "nodejs": 3, "typescript": 3, "javascript": 3, "html": 2, "css": 2, "tailwind": 2,
    "django": 3, "fastapi": 3, "flask": 3, "laravel": 3, "php": 2, "wordpress": 2,
    "web designer": 2, "ui developer": 3, "rest api": 2, "graphql": 2,
    # general programming
    "software engineer": 3, "software developer": 3, "developer": 2, "programmer": 3,
    "engineer": 1, "python": 3, "java": 2, "golang": 3, "rust": 2, "c#": 2, ".net": 2,
    "devops": 2, "mobile developer": 2, "flutter": 2, "react native": 3, "sql": 1,
    "#developer": 3, "#itjob": 2, "#backend": 3, "#frontend": 3, "#python": 3,
    # AI
    "ai engineer": 3, "machine learning": 3, "ml engineer": 3, "llm": 3, "genai": 3,
    "generative ai": 3, "deep learning": 3, "nlp": 2, "data scientist": 2, "prompt engineer": 2,
    "#ai": 2, "ai": 1, "openai": 2, "langchain": 3, "pytorch": 2,
    # Persian
    "برنامه نویس": 3, "برنامه‌نویس": 3, "توسعه دهنده": 3, "توسعه‌دهنده": 3, "طراحی سایت": 3,
    "طراح سایت": 3, "فرانت": 3, "بک اند": 3, "بک‌اند": 3, "فول استک": 3, "هوش مصنوعی": 3,
    "یادگیری ماشین": 3, "پایتون": 3, "جاوااسکریپت": 3, "ری اکت": 3, "ری‌اکت": 3, "لاراول": 3,
    "جنگو": 3, "وردپرس": 2, "نرم افزار": 2, "نرم‌افزار": 2, "وب": 1,
}
NEGATIVE = {
    "waitress": 4, "waiter": 4, "housekeeping": 4, "cashier": 4, "driver": 3, "nurse": 4,
    "receptionist": 3, "admin coordinator": 3, "accountant": 3, "sales executive": 3,
    "security guard": 4, "chef": 4, "cleaner": 4, "barista": 4, "telesales": 3,
    "information security": 2, "head of": 1,
}
# Things that make it a job post rather than an article/ad (small bonus).
JOB_SIGNALS = ["hiring", "job", "vacancy", "position", "apply", "remote", "freelance",
               "contract", "استخدام", "همکاری", "دورکاری", "پروژه", "فریلنس", "#remote"]


def _pattern(term):
    t = re.escape(term.lower())
    # Word boundaries only make sense for plain ASCII words
    if re.fullmatch(r"[a-z0-9 .\-]+", term.lower()):
        return re.compile(r"(?<![a-z0-9])" + t + r"(?![a-z0-9])")
    return re.compile(t)


_POS = [(k, w, _pattern(k)) for k, w in POSITIVE.items()]
_NEG = [(k, w, _pattern(k)) for k, w in NEGATIVE.items()]


def score_post(text):
    """Return (score, matched_positive_terms)."""
    low = text.lower()
    hits = [(k, w) for k, w, p in _POS if p.search(low)]
    # Don't double count "react" inside "react native" etc.: keep longest distinct terms
    hits.sort(key=lambda kw: -len(kw[0]))
    kept = []
    for k, w in hits:
        if not any(k in bigger for bigger, _ in kept):
            kept.append((k, w))
    pos = sum(w for _, w in kept)
    neg = sum(w for k, w, p in _NEG if p.search(low))
    bonus = 1 if pos and any(s in low for s in JOB_SIGNALS) else 0
    return pos + bonus - neg, [k for k, _ in kept]


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def make_client(session=None):
    if not API_ID or not API_HASH:
        sys.exit("Set TG_API_ID and TG_API_HASH in .env (get them at https://my.telegram.org).")
    kwargs = {}
    if PROXY:
        m = re.match(r"(socks5|socks4|http)://(?:(.+?):(.+?)@)?([^:]+):(\d+)", PROXY)
        if not m:
            sys.exit("PROXY must look like socks5://host:port or socks5://user:pass@host:port")
        kind, user, pw, host, port = m.groups()
        kwargs["proxy"] = (kind, host, int(port), True, user, pw)
    if session is None:
        session = StringSession(TG_SESSION) if TG_SESSION else str(HERE / "job_alert")
    return TelegramClient(session, API_ID, API_HASH, **kwargs)


def http_proxies():
    if not PROXY:
        return None
    p = PROXY.replace("socks5://", "socks5h://")
    return {"http": p, "https": p}


def post_link(ch, msg_id):
    if ch.get("username"):
        return f"https://t.me/{ch['username']}/{msg_id}"
    return f"https://t.me/c/{ch['id']}/{msg_id}"


def html_escape(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


async def notify(client, ch, msg, score, terms):
    text = msg.message or ""
    snippet = text if len(text) <= 700 else text[:700] + "…"
    body = (
        f"💼 <b>{html_escape(ch['title'])}</b>  ·  score {score}\n"
        f"<i>{html_escape(', '.join(terms[:6]))}</i>\n\n"
        f"{html_escape(snippet)}\n\n"
        f"<a href=\"{post_link(ch, msg.id)}\">Open post</a>"
    )
    if BOT_TOKEN and NOTIFY_CHAT_ID:
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                json={"chat_id": NOTIFY_CHAT_ID, "text": body, "parse_mode": "HTML",
                      "disable_web_page_preview": True},
                proxies=http_proxies(), timeout=30,
            )
            if r.ok:
                return
            print("Bot send failed:", r.text)
        except requests.RequestException as e:
            print("Bot send failed:", e)
    # Fallback: Saved Messages
    await client.send_message("me", body, parse_mode="html", link_preview=False)


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------
async def find_source_dialog(client):
    want = SOURCE_CHAT.lower().lstrip("@")
    async for d in client.iter_dialogs():
        ent = d.entity
        uname = (getattr(ent, "username", None) or "").lower()
        if d.name.lower() == want or uname == want:
            return d
    async for d in client.iter_dialogs():
        if want in d.name.lower():
            return d
    return None


def channel_record(ent):
    return {"id": ent.id, "username": ent.username, "title": ent.title}


async def cmd_discover(client):
    dialog = await find_source_dialog(client)
    if not dialog:
        sys.exit(f"Couldn't find a chat named '{SOURCE_CHAT}'. Set SOURCE_CHAT in .env.")
    print(f"Scanning last {DISCOVER_LIMIT} messages in '{dialog.name}'…")
    found = {c["id"]: c for c in load_json(CHANNELS_FILE, [])}
    usernames = set()
    link_re = re.compile(r"(?:https?://)?t\.me/(?!c/|joinchat|\+)([A-Za-z0-9_]{5,32})")
    at_re = re.compile(r"(?<![\w.])@([A-Za-z][A-Za-z0-9_]{4,31})")

    async for msg in client.iter_messages(dialog, limit=DISCOVER_LIMIT):
        # 1) forwarded channel posts - the main source
        if msg.fwd_from:
            try:
                ent = await msg.forward.get_chat()
            except Exception:
                ent = None
            if isinstance(ent, Channel) and ent.broadcast:
                found[ent.id] = channel_record(ent)
        # 2) links / @mentions in the text
        text = msg.message or ""
        usernames.update(link_re.findall(text))
        usernames.update(at_re.findall(text))

    for u in usernames:
        try:
            ent = await client.get_entity(u)
        except Exception:
            continue
        if isinstance(ent, Channel) and ent.broadcast:
            found[ent.id] = channel_record(ent)

    channels = sorted(found.values(), key=lambda c: c["title"].lower())
    save_json(CHANNELS_FILE, channels)
    print(f"\nSaved {len(channels)} channel(s) to channels.json:")
    for c in channels:
        print(f"  - {c['title']}  ({'@' + c['username'] if c['username'] else 'id ' + str(c['id'])})")
    print("\nEdit channels.json to remove any you don't want (e.g. non-tech ones).")


async def resolve(client, ch):
    try:
        if ch.get("access_hash"):
            return InputPeerChannel(ch["id"], ch["access_hash"])
        return await client.get_input_entity(ch["username"] or ch["id"])
    except Exception as e:
        print(f"  ! can't access {ch['title']}: {e}")
        return None


async def cmd_test(client):
    channels = load_json(CHANNELS_FILE, [])
    if not channels:
        sys.exit("No channels.json yet - run: python job_alert.py discover")
    for ch in channels:
        peer = await resolve(client, ch)
        if not peer:
            continue
        print(f"\n=== {ch['title']} ===")
        async for msg in client.iter_messages(peer, limit=30):
            text = msg.message or ""
            if not text.strip():
                continue
            score, terms = score_post(text)
            mark = "✅" if score >= MIN_SCORE else "  "
            first = text.strip().splitlines()[0][:80]
            print(f"{mark} {score:>3}  {first}   {terms[:4] if score >= MIN_SCORE else ''}")


def bot_say(text):
    if BOT_TOKEN and NOTIFY_CHAT_ID:
        try:
            requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                          json={"chat_id": NOTIFY_CHAT_ID, "text": text},
                          proxies=http_proxies(), timeout=30)
        except requests.RequestException as e:
            print("Couldn't send bot message:", e)


async def setup_peers(client):
    channels = load_json(CHANNELS_FILE, [])
    if not channels:
        sys.exit("No channels.json yet - run: python job_alert.py discover")
    state = load_json(STATE_FILE, {})
    peers = {}
    for ch in channels:
        p = await resolve(client, ch)
        if p:
            peers[str(ch["id"])] = (ch, p)
            if str(ch["id"]) not in state:
                # First time: start from the newest post, don't flood with old ones
                latest = await client.get_messages(p, limit=1)
                state[str(ch["id"])] = latest[0].id if latest else 0
    save_json(STATE_FILE, state)
    return peers, state


async def poll_once(client, peers, state):
    matches = 0
    for key, (ch, peer) in peers.items():
        try:
            new = [m async for m in client.iter_messages(peer, min_id=state[key], reverse=True)]
        except Exception as e:
            print(f"  ! {ch['title']}: {e}")
            continue
        for msg in new:
            state[key] = max(state[key], msg.id)
            text = msg.message or ""
            if not text.strip():
                continue
            score, terms = score_post(text)
            if score >= MIN_SCORE:
                matches += 1
                print(f"[{time.strftime('%H:%M')}] match ({score}) in {ch['title']}: {terms[:4]}")
                await notify(client, ch, msg, score, terms)
        print(f"  {ch['title']}: {len(new)} new post(s)")
        await asyncio.sleep(2)  # be gentle with rate limits
    save_json(STATE_FILE, state)
    return matches


async def cmd_run(client):
    peers, state = await setup_peers(client)
    where = "your bot" if BOT_TOKEN and NOTIFY_CHAT_ID else "Saved Messages"
    print(f"Watching {len(peers)} channel(s), every {POLL_MINUTES:g} min. Alerts go to {where}. Ctrl+C to stop.")
    bot_say("✅ Job alerts running. Watching:\n" + "\n".join(f"• {c['title']}" for c, _ in peers.values()))
    while True:
        await poll_once(client, peers, state)
        await asyncio.sleep(POLL_MINUTES * 60)


async def cmd_once(client):
    peers, state = await setup_peers(client)
    if not peers:
        sys.exit("Couldn't open any channel - check the login secret.")
    n = await poll_once(client, peers, state)
    print(f"Done: {n} alert(s) sent.")


async def cmd_export(client):
    """Run on your Mac. Makes a separate login for GitHub and writes github_secret.txt."""
    # 1) Store access hashes so channels without a public @username work on a fresh login
    channels = load_json(CHANNELS_FILE, [])
    for ch in channels:
        if not ch.get("username") and not ch.get("access_hash"):
            try:
                ent = await client.get_entity(ch["id"])
                ch["access_hash"] = ent.access_hash
            except Exception as e:
                print(f"  ! couldn't get access for {ch['title']}: {e}")
    save_json(CHANNELS_FILE, channels)
    # 2) New, independent login (Telegram kills a login used from two places at once)
    print("\nNow logging in a SECOND time, for GitHub. Enter your phone number and the new code.")
    gh = make_client(session=StringSession())
    await gh.start()
    session_str = gh.session.save()
    await gh.disconnect()
    # 3) Everything GitHub needs, as one secret
    keep = ("TG_API_ID", "TG_API_HASH", "BOT_TOKEN", "NOTIFY_CHAT_ID", "MIN_SCORE")
    lines = [l for l in (HERE / ".env").read_text(encoding="utf-8").splitlines()
             if l.split("=", 1)[0] in keep]
    lines.append(f"TG_SESSION={session_str}")
    out = HERE / "github_secret.txt"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.chmod(out, 0o600)
    print(f"\nWrote {out.name}. Paste its whole contents into a GitHub secret named JOB_ALERTS_ENV,")
    print("then delete the file. Don't share it - it's a login to your Telegram account.")


def cmd_chat_id():
    if not BOT_TOKEN:
        sys.exit("Set BOT_TOKEN in .env first.")
    api = f"https://api.telegram.org/bot{BOT_TOKEN}"
    me = requests.get(f"{api}/getMe", proxies=http_proxies(), timeout=30).json()
    if not me.get("ok"):
        sys.exit(f"Bot token rejected by Telegram: {me}. Copy the token from BotFather into .env again.")
    print(f"Bot OK: @{me['result']['username']}")
    r = requests.get(f"{api}/getUpdates", proxies=http_proxies(), timeout=30).json()
    chats = {u["message"]["chat"]["id"]: u["message"]["chat"].get("first_name", "")
             for u in r.get("result", []) if "message" in u and u["message"]["chat"]["type"] == "private"}
    if not chats:
        print("Bot hasn't seen a message yet; will use your account id after login instead.")
        print("(getUpdates said:", str(r)[:300], ")")
        return
    cid = next(iter(chats))
    save_chat_id(cid)
    print(f"Chat id {cid} ({chats[cid]}) saved to .env")


def save_chat_id(cid):
    global NOTIFY_CHAT_ID
    env = HERE / ".env"
    lines = env.read_text(encoding="utf-8").splitlines()
    lines = [f"NOTIFY_CHAT_ID={cid}" if l.startswith("NOTIFY_CHAT_ID=") else l for l in lines]
    env.write_text("\n".join(lines) + "\n", encoding="utf-8")
    NOTIFY_CHAT_ID = str(cid)


async def ensure_chat_id(client):
    # A private chat with a bot has the same id as your user account.
    if BOT_TOKEN and not NOTIFY_CHAT_ID:
        me = await client.get_me()
        save_chat_id(me.id)
        print(f"Using your account id {me.id} as NOTIFY_CHAT_ID (saved to .env)")


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    if cmd == "chat-id":
        return cmd_chat_id()
    fn = {"discover": cmd_discover, "test": cmd_test, "run": cmd_run,
          "once": cmd_once, "export": cmd_export}.get(cmd)
    if not fn:
        sys.exit(__doc__)
    client = make_client()
    with client:  # first time: asks for your phone number + login code in the terminal
        client.loop.run_until_complete(ensure_chat_id(client))
        client.loop.run_until_complete(fn(client))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.")
