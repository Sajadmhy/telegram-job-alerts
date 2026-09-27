"""Tailored resumes from your job alerts, through the same bot.

Two ways in:
  * Tap "📝 Tailor resume" under any alert.
  * Send the bot a job post yourself: paste the text, or send a link.

Either way the job goes to the resume backend (the `unemployed` repo's
POST /tailor, running on the same machine) and the PDF comes back as a
document in the chat. The backend runs a local model, so each resume takes a
few minutes; requests are handled one at a time, in order.

Only you can use it: an update is acted on only when it comes from the
NOTIFY_CHAT_ID chat AND, if ALLOWED_USERS is set, from one of those Telegram
accounts (@username or numeric id). Anyone else gets no reply at all. Anyone can
find and message a bot, and this one writes resumes from your experience.

Settings (.env):
  RESUME_API=http://localhost:8000   # where the backend listens; empty turns this off
  RESUME_TIMEOUT_MIN=20              # give up on one resume after this long
"""
import json
import os
import queue
import re
import threading
import time
from pathlib import Path
from urllib.parse import unquote

import requests

HERE = Path(__file__).resolve().parent
POSTS_FILE = HERE / "posts.json"
MAX_POSTS = 300  # alerts kept for the button; older ones fall off

# Read in start(): this module is imported before job_alert.py loads .env.
RESUME_API = ""
RESUME_TIMEOUT_MIN = 20.0

_lock = threading.Lock()
_jobs: "queue.Queue[tuple[str, dict]]" = queue.Queue()
_URL_RE = re.compile(r"https?://\S+")

_cfg = {"token": "", "chat_id": "", "proxies": None}


def enabled() -> bool:
    return bool(RESUME_API and _cfg["token"] and _cfg["chat_id"])


# ---- posts shown in alerts, so the button can find the text later ------------
def remember_post(key: str, text: str) -> None:
    with _lock:
        posts = _load_posts()
        posts[key] = text
        if len(posts) > MAX_POSTS:
            for old in list(posts)[: len(posts) - MAX_POSTS]:
                del posts[old]
        POSTS_FILE.write_text(json.dumps(posts, ensure_ascii=False))


def _load_posts() -> dict:
    try:
        return json.loads(POSTS_FILE.read_text())
    except (FileNotFoundError, ValueError):
        return {}


def button(key: str) -> dict:
    """reply_markup for an alert. callback_data is capped at 64 bytes by Telegram."""
    return {"inline_keyboard": [[{"text": "📝 Tailor resume", "callback_data": f"t:{key}"[:64]}]]}


# ---- Telegram Bot API ---------------------------------------------------------
def _api(method: str, timeout: float = 30, **kwargs):
    url = f"https://api.telegram.org/bot{_cfg['token']}/{method}"
    return requests.post(url, proxies=_cfg["proxies"], timeout=timeout, **kwargs)


def _say(text: str, reply_to: int | None = None) -> None:
    body = {"chat_id": _cfg["chat_id"], "text": text, "disable_web_page_preview": True}
    if reply_to:
        body["reply_to_message_id"] = reply_to
        body["allow_sending_without_reply"] = True
    try:
        _api("sendMessage", json=body)
    except requests.RequestException as e:
        print("tailor: couldn't send message:", e)


def _send_pdf(content: bytes, filename: str, caption: str, reply_to: int | None) -> None:
    data = {"chat_id": _cfg["chat_id"], "caption": caption[:1000]}
    if reply_to:
        data["reply_to_message_id"] = reply_to
        data["allow_sending_without_reply"] = "true"
    _api("sendDocument", timeout=120, data=data,
         files={"document": (filename, content, "application/pdf")})


def _filename(disposition: str) -> str | None:
    """The file name from a Content-Disposition header.

    A name with spaces or non-ASCII letters arrives as filename*=utf-8''<%-encoded>
    (RFC 5987) rather than filename="...", so both forms are read.
    """
    star = re.search(r"filename\*=(?:utf-8|UTF-8)''([^;]+)", disposition)
    if star:
        return unquote(star.group(1).strip())
    plain = re.search(r'filename="([^"]+)"|filename=([^;]+)', disposition)
    if plain:
        return (plain.group(1) or plain.group(2)).strip()
    return None


# ---- work ----------------------------------------------------------------------
def _worker() -> None:
    while True:
        label, job = _jobs.get()
        reply_to = job.pop("_reply_to", None)
        started = time.time()
        try:
            # The backend is on this machine: never send it through the Telegram proxy.
            r = requests.post(f"{RESUME_API}/tailor", json=job, timeout=RESUME_TIMEOUT_MIN * 60,
                              proxies={"http": None, "https": None})
            if r.status_code == 200:
                name = _filename(r.headers.get("content-disposition", ""))
                title = r.headers.get("X-Job-Title", "")
                company = r.headers.get("X-Company", "")
                mins = (time.time() - started) / 60
                caption = f"📄 {title} · {company}".strip(" ·") + f"\n({mins:.0f} min)"
                _send_pdf(r.content, name or "resume.pdf", caption, reply_to)
            else:
                try:
                    detail = r.json().get("detail", r.text)
                except ValueError:
                    detail = r.text
                _say(f"❌ Couldn't make that resume: {str(detail)[:500]}", reply_to)
        except requests.Timeout:
            _say(f"❌ Gave up after {RESUME_TIMEOUT_MIN:g} min. The model may be too slow for this "
                 "post; try again, or set a smaller OLLAMA_MODEL on the backend.", reply_to)
        except requests.ConnectionError:
            _say(f"❌ The resume backend isn't reachable at {RESUME_API}. "
                 "Is the unemployed-api service running?", reply_to)
        except Exception as e:  # noqa: BLE001 - keep the worker alive
            _say(f"❌ Something went wrong: {e}", reply_to)
        finally:
            _jobs.task_done()


def _enqueue(label: str, job: dict, reply_to: int | None) -> None:
    ahead = _jobs.unfinished_tasks  # waiting + the one running
    job["_reply_to"] = reply_to
    _jobs.put((label, job))
    wait = f" ({ahead} ahead of it)" if ahead else ""
    _say(f"⏳ Tailoring your resume{wait}. Usually under a minute (a few minutes if the backend runs a local model).", reply_to)


def _allowed(chat: dict, sender: dict) -> bool:
    """From the configured chat, and (when ALLOWED_USERS is set) from an allowed account."""
    if str(chat.get("id", "")) != _cfg["chat_id"]:
        return False
    allowed = _cfg.get("allowed") or set()
    if not allowed:
        return True
    uid = str(sender.get("id", ""))
    uname = (sender.get("username") or "").lower()
    return uid in allowed or (uname and uname in allowed)


def _handle(update: dict) -> None:
    if "callback_query" in update:
        cq = update["callback_query"]
        if not _allowed(cq.get("message", {}).get("chat", {}), cq.get("from", {})):
            print("tailor: ignored button press from", cq.get("from", {}).get("username") or cq.get("from", {}).get("id"))
            return
        key = (cq.get("data") or "")[2:]
        text = _load_posts().get(key)
        try:
            _api("answerCallbackQuery", json={
                "callback_query_id": cq["id"],
                "text": "Queued ✅" if text else "That post is too old, send its text instead.",
            })
        except requests.RequestException:
            pass
        if text:
            _enqueue(key, {"text": text}, cq["message"].get("message_id"))
        return

    msg = update.get("message") or {}
    if not _allowed(msg.get("chat", {}), msg.get("from", {})):
        sender = msg.get("from", {})
        print("tailor: ignored message from", sender.get("username") or sender.get("id"))
        return
    text = (msg.get("text") or msg.get("caption") or "").strip()
    if not text:
        return
    if text.startswith("/"):
        _say("Send me a job post (the text, or a link to it) and I'll send back a resume "
             "tailored to it. You can also tap “📝 Tailor resume” under any alert.")
        return
    urls = _URL_RE.findall(text)
    if len(text) < 50 and not urls:
        _say("That's too short to be a job post. Paste the whole post, or send a link.")
        return
    job = {"text": text}
    if urls and len(text) < 300:
        job = {"url": urls[0]}
    _enqueue("message", job, msg.get("message_id"))


def _poll() -> None:
    offset = None
    while True:
        try:
            r = _api("getUpdates", timeout=70, json={
                "timeout": 50, "offset": offset, "allowed_updates": ["message", "callback_query"],
            })
            data = r.json()
            if not data.get("ok"):
                print("tailor: getUpdates failed:", data)
                time.sleep(30)
                continue
            for update in data["result"]:
                offset = update["update_id"] + 1
                try:
                    _handle(update)
                except Exception as e:  # noqa: BLE001 - one bad update must not stop the loop
                    print("tailor: update failed:", e)
        except (requests.RequestException, ValueError) as e:
            print("tailor: polling error:", e)
            time.sleep(15)


def start(token: str, chat_id: str, proxies) -> bool:
    """Start the listener and the worker in background threads. Returns whether it started."""
    global RESUME_API, RESUME_TIMEOUT_MIN
    RESUME_API = os.getenv("RESUME_API", "http://localhost:8000").strip().rstrip("/")
    RESUME_TIMEOUT_MIN = float(os.getenv("RESUME_TIMEOUT_MIN", "20"))
    allowed = {u.strip().lstrip("@").lower() for u in os.getenv("ALLOWED_USERS", "").split(",") if u.strip()}
    _cfg.update(token=token, chat_id=str(chat_id), proxies=proxies, allowed=allowed)
    if not enabled():
        return False
    threading.Thread(target=_worker, name="tailor-worker", daemon=True).start()
    threading.Thread(target=_poll, name="tailor-poll", daemon=True).start()
    return True
