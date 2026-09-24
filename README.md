# Telegram Job Alerts

Watches the job channels Mobina shared and pings you through your own bot when a post matches web dev, AI or programming.

Channels found in the chat (on Sep 24):
- Job Finding | Search for your job (@jobs_finding)
- Remote Jobs in Dubai UAE
- Jobs in Dubai, UAE
- Remote IT (Inflow)
- Remote Jobs by Remote OK

The `discover` command picks these up from the forwarded posts. You don't need to type any usernames.

## Setup (about 5 min)

```bash
cd telegram-job-alerts
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

1. **API keys:** go to https://my.telegram.org, open "API development tools" and create an app. Put `api_id` and `api_hash` in `.env`.
2. **Bot:** in Telegram, message @BotFather, send `/newbot` and put the token in `BOT_TOKEN`. Then open your new bot and press **Start**.
3. `python job_alert.py chat-id` prints your chat id. Put it in `NOTIFY_CHAT_ID`.
4. `python job_alert.py discover`. The first time, it asks for your phone number and a login code. Then it scans the Mobina chat and writes `channels.json`. Delete any channels you don't want from that file.
5. `python job_alert.py test` shows which of the last 30 posts in each channel would trigger an alert (✅). Nothing is sent.
6. `python job_alert.py run` starts watching.

## How it works

- Reading uses **your account** in read-only mode, because a bot can't read channels it doesn't admin. It never joins channels, posts anything or marks anything as read.
- It checks each channel every `POLL_MINUTES`. On the first run it starts from the newest post, so you won't get flooded with old ones.
- Each post gets a score from keywords (English and Persian), such as React, Node, Python, backend, AI/LLM, برنامه نویس or فرانت. Non-tech roles like waitress or housekeeping subtract points. Posts with a score of at least `MIN_SCORE` get sent to you with the matched keywords and a link.
- To tune what matches, edit the `POSITIVE` and `NEGATIVE` lists at the top of `job_alert.py`.

## Running on GitHub Actions (free, always on)

`.github/workflows/job-alerts.yml` runs `python job_alert.py once` every 10 minutes. It checks each channel for posts newer than the ones recorded in `state.json`, sends the alerts, then commits the updated `state.json`.

It uses one repo secret, **JOB_ALERTS_ENV**. That's the contents of `github_secret.txt`, which `python job_alert.py export` creates on your Mac. The file contains your API ID and hash, the bot token, your chat ID, and a separate Telegram login made just for GitHub. Nothing secret is stored in the repo itself.

Don't run the Mac version and the GitHub version at the same time, or you'll get every alert twice.

## Keep it running on your Mac

The simplest option is to leave `python job_alert.py run` open in a terminal tab. To run it in the background and survive restarts, use a LaunchAgent. Ask Claude and it can set one up for you.

Keep `.env` and `job_alert.session` private. The session file is a login to your Telegram account.
