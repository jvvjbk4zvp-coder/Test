# Telegram EA Control Bot

Lets your clients log in via Telegram and manage their open MT5 trades with
one-tap buttons: **SL to BE**, **Partials 25/50/75%**, **Close All**, **De-risk**.

## Why a "bridge" is needed

MT4/MT5 terminals don't expose an API that Telegram can call directly.
The reliable, widely-used pattern is:

```
Telegram Bot (Python)  --writes-->  command file  --polled by-->  MQL5 EA on MT5
```

This repo gives you both halves.

## Files

- `bot.py` — the Telegram bot (login, session mapping, buttons, command queue)
- `ea_bridge.mq5` — the MT5 Expert Advisor that polls for commands and executes them
- `requirements.txt` — Python deps

## Setup

### 1. Create your bot
Talk to **@BotFather** on Telegram, run `/newbot`, copy the token.

### 2. Install deps
```bash
pip install -r requirements.txt
```

### 3. Configure `bot.py`
- Set `TELEGRAM_BOT_TOKEN` env var (or edit `BOT_TOKEN` directly — not recommended for production).
- Fill in the `CLIENTS` dict with each client's username/password and their MT5 account id.
  **In production, replace this with a real database and hashed passwords** (e.g. `bcrypt`),
  and consider 2FA given this controls real money.

### 4. Wire up the command file location
`bot.py` writes to `./commands/<account_id>.json`. The EA reads a file named
by `CommandFileName` from the terminal's `MQL5/Files/` folder (or `FILE_COMMON`
for the shared `Terminal/Common/Files/` folder, which is easier if the bot and
terminal are on the same machine).

**Simplest reliable setup:** run the bot on the *same machine/VPS* as each
client's MT5 terminal (or one bot per client VPS), and point `COMMANDS_DIR`
at that terminal's `MQL5/Files/` directory so the EA can read it with zero
extra networking. For multiple clients on separate machines, replace the
file-drop with a small HTTP endpoint the EA polls via `WebRequest()`
(add your VPS's domain to MT5's allowed URLs list).

### 5. Install the EA
- Copy `ea_bridge.mq5` into `MQL5/Experts/`, compile in MetaEditor.
- Attach it to any chart in the client's MT5 terminal.
- Enable **AutoTrading** and, in EA properties → Common, check **"Allow file access"**
  (and "Allow WebRequest" if you switch to the HTTP variant).

### 6. Run the bot
```bash
python bot.py
```

## Using it

1. Client opens a DM with your bot, sends `/start`, then `/login`.
2. Bot asks for username, then password (bot deletes the password message after use).
3. On success, client gets the control panel:

   ```
   [ SL to BE ]
   [ Partial 25% ] [ Partial 50% ] [ Partial 75% ]
   [ ⚠️ DE-RISK ]
   [ 🔴 CLOSE ALL ]
   ```
4. Pressing a button queues a command tied to *that client's* linked MT5 account.
   **Close All** requires a second tap to confirm.
5. `/panel` re-shows the buttons any time; `/logout` ends the session.

## Security recommendations before going live

- Replace the in-memory `CLIENTS`/`SESSIONS` dicts with a real database.
- Hash passwords (bcrypt/argon2) — never store plaintext.
- Rate-limit login attempts.
- Log every command with chat_id, username, account_id, and timestamp for audit.
- Restrict the bot to a private chat only (it already ignores group chats implicitly
  since sessions are per chat_id, but add an explicit `update.effective_chat.type == "private"` check if needed).
- Consider requiring a confirmation step on **De-risk** too, not just Close All.
- If using `WebRequest` instead of files, run it over HTTPS with a shared secret/token
  header so the EA only accepts commands actually from your bot.
