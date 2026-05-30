# US News → Telegram Agent

A Python agent that pulls news from major US news RSS feeds, deduplicates items, optionally enriches them with scraped article snippets, and sends a digest to a Telegram channel.

## What it does

- Monitors major US news sources from `sources.json`
- Collects recent items from the last `LOOKBACK_HOURS` (default: 24)
- Deduplicates repeated headlines across outlets
- Optionally fetches article pages for better snippets
- Sends a daily digest to your Telegram channel or chat
- Remembers what it already sent using `state.json`

## Files

- `news_agent.py` — main agent
- `sources.json` — editable list of feeds
- `.env.example` — environment variables template
- `requirements.txt` — Python dependencies

## 1) Create your Telegram bot

1. Open Telegram and message **@BotFather**.
2. Run `/newbot` and follow the prompts.
3. Copy the bot token.

## 2) Connect the bot to your channel

### Public channel

1. Add the bot to your channel.
2. Promote it to **Admin**.
3. Set `TELEGRAM_CHAT_ID=@Bahlrajesh_ainewbot`

### Private channel

1. Add the bot to the private channel.
2. Promote it to **Admin**.
3. Post a message in the channel.
4. Open this URL in your browser, replacing the token:

```text
https://api.telegram.org/bot<8856823898:AAGQOlLZjKq_tLOkh55McfNBmuQtQOXYWmk>/getUpdates
