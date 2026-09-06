"""
Telegram EA Control Bot
========================
Lets authenticated clients manage open trades via inline buttons:
  - SL to Breakeven
  - Partial close 25% / 50% / 75%
  - Close All
  - De-risk (SL to BE + partial close, e.g. 50%)

ARCHITECTURE
------------
Telegram Bot (this file, Python)
        |
        |  writes a JSON "command file" (or HTTP POST) per user
        v
Command Queue (./commands/<account>.json  OR  a small HTTP server)
        |
        |  polled by an MQL5 Expert Advisor running on the client's MT5 terminal
        v
MT5 Terminal executes the trade action

Telegram bots CANNOT talk to MT4/MT5 directly — there is no socket/API on the
terminal by default. The standard, reliable pattern is a file-drop or local
HTTP bridge that the EA polls every tick/timer. A matching EA (MQL5) is
provided in ea_bridge.mq5 in this same folder.

SETUP
-----
1. pip install python-telegram-bot==21.4
2. Set BOT_TOKEN below (or via env var TELEGRAM_BOT_TOKEN).
3. Fill in CLIENTS with each client's login credentials + their MT account id.
4. Run: python bot.py
5. Copy ea_bridge.mq5 into each client's MT5 (or MT4, adapted) and attach it
   to a chart with AutoTrading enabled. It polls the command file this bot
   writes and executes the action, then deletes/acks it.

SECURITY NOTES
--------------
- Never hardcode real passwords in source control. Use env vars / a secrets
  manager in production. The CLIENTS dict below is for demonstration.
- Only chat_ids that have successfully /login are authorized to press
  action buttons — every callback re-checks this.
- This bot only issues *commands* (close/partial/SL move). It does not
  place new trades, which limits blast radius of a compromised session.
"""

import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# --------------------------------------------------------------------------
# CONFIG
# --------------------------------------------------------------------------

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "PUT_YOUR_BOT_TOKEN_HERE")

# Where command files are dropped for the EA to pick up.
COMMANDS_DIR = Path("./commands")
COMMANDS_DIR.mkdir(exist_ok=True)

# Demo client "database". In production, use a real DB (SQLite/Postgres)
# and hashed passwords (e.g. bcrypt), never plaintext.
CLIENTS = {
    # username: {"password": "...", "account_id": "MT5 account/login number"}
    "client1": {"password": "changeme1", "account_id": "1000001"},
    "client2": {"password": "changeme2", "account_id": "1000002"},
}

# Default lot-reduction for the "De-risk" one-tap action
DERISK_PARTIAL_PCT = 50
DERISK_MOVE_SL_TO_BE = True

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("ea_bot")

# --------------------------------------------------------------------------
# SESSION STATE (in-memory; swap for Redis/DB for multi-instance deployments)
# --------------------------------------------------------------------------


@dataclass
class Session:
    chat_id: int
    username: str
    account_id: str
    logged_in_at: float = field(default_factory=time.time)


# chat_id -> Session
SESSIONS: dict[int, Session] = {}


def is_authorized(chat_id: int) -> bool:
    return chat_id in SESSIONS


def get_session(chat_id: int) -> Session | None:
    return SESSIONS.get(chat_id)


# --------------------------------------------------------------------------
# COMMAND BRIDGE — writes a command file the EA polls
# --------------------------------------------------------------------------


def push_command(account_id: str, action: str, params: dict | None = None) -> str:
    """
    Writes a command for the EA to consume. Each command gets a unique id
    and is appended to that account's queue file. The EA is expected to:
      1. Read the file
      2. Execute each unprocessed command
      3. Mark it processed (or the bot rotates/clears the file on ack)

    Swap this out for an HTTP POST to a local bridge server if you prefer
    push-based delivery instead of polling.
    """
    queue_file = COMMANDS_DIR / f"{account_id}.json"

    cmd = {
        "id": f"{int(time.time() * 1000)}",
        "action": action,
        "params": params or {},
        "status": "pending",
        "created_at": time.time(),
    }

    if queue_file.exists():
        try:
            data = json.loads(queue_file.read_text())
        except json.JSONDecodeError:
            data = []
    else:
        data = []

    data.append(cmd)
    queue_file.write_text(json.dumps(data, indent=2))
    logger.info("Queued command for account %s: %s", account_id, cmd)
    return cmd["id"]


# --------------------------------------------------------------------------
# UI — the control panel keyboard
# --------------------------------------------------------------------------


def control_panel_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("🟢 SL to BE", callback_data="SL_BE")],
        [
            InlineKeyboardButton("Partial 25%", callback_data="PARTIAL_25"),
            InlineKeyboardButton("Partial 50%", callback_data="PARTIAL_50"),
            InlineKeyboardButton("Partial 75%", callback_data="PARTIAL_75"),
        ],
        [InlineKeyboardButton("⚠️ DE-RISK", callback_data="DERISK")],
        [InlineKeyboardButton("🔴 CLOSE ALL", callback_data="CLOSE_ALL")],
    ]
    return InlineKeyboardMarkup(rows)


# --------------------------------------------------------------------------
# LOGIN FLOW
# --------------------------------------------------------------------------

# naive per-chat login state machine: waiting for username -> waiting for password
LOGIN_STATE: dict[int, dict] = {}


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if is_authorized(chat_id):
        await update.message.reply_text(
            "You're already logged in. Here's your control panel:",
            reply_markup=control_panel_keyboard(),
        )
        return
    await update.message.reply_text(
        "Welcome. Use /login to authenticate and access your trade controls."
    )


async def login_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if is_authorized(chat_id):
        await update.message.reply_text(
            "Already logged in.", reply_markup=control_panel_keyboard()
        )
        return
    LOGIN_STATE[chat_id] = {"stage": "username"}
    await update.message.reply_text("Please enter your username:")


async def logout_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    SESSIONS.pop(chat_id, None)
    LOGIN_STATE.pop(chat_id, None)
    await update.message.reply_text("Logged out.")


async def text_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the plain-text steps of the login flow (username, then password)."""
    chat_id = update.effective_chat.id
    state = LOGIN_STATE.get(chat_id)
    if not state:
        return  # not mid-login, ignore stray text

    text = update.message.text.strip()

    if state["stage"] == "username":
        state["username"] = text
        state["stage"] = "password"
        # Delete the username message isn't strictly needed; but we DO
        # want to encourage deleting the password message after use.
        await update.message.reply_text("Now enter your password:")
        return

    if state["stage"] == "password":
        username = state.get("username")
        password = text

        record = CLIENTS.get(username)
        if record and record["password"] == password:
            SESSIONS[chat_id] = Session(
                chat_id=chat_id,
                username=username,
                account_id=record["account_id"],
            )
            LOGIN_STATE.pop(chat_id, None)
            await update.message.reply_text(
                f"✅ Login successful. Linked to account {record['account_id']}."
            )
            # Best practice: try to delete the password message from the chat
            try:
                await update.message.delete()
            except Exception:
                pass
            await context.bot.send_message(
                chat_id=chat_id,
                text="Your trade control panel:",
                reply_markup=control_panel_keyboard(),
            )
        else:
            LOGIN_STATE.pop(chat_id, None)
            await update.message.reply_text(
                "❌ Invalid username or password. Use /login to try again."
            )
        return


# --------------------------------------------------------------------------
# BUTTON CALLBACKS
# --------------------------------------------------------------------------


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    chat_id = query.message.chat_id

    if not is_authorized(chat_id):
        await query.answer("Please /login first.", show_alert=True)
        return

    session = get_session(chat_id)
    action = query.data

    # Map button -> (action name for EA, human confirmation text)
    actions = {
        "SL_BE": ("SL_TO_BE", "Moving Stop Loss to Breakeven..."),
        "PARTIAL_25": ("PARTIAL_CLOSE", "Closing 25% of position..."),
        "PARTIAL_50": ("PARTIAL_CLOSE", "Closing 50% of position..."),
        "PARTIAL_75": ("PARTIAL_CLOSE", "Closing 75% of position..."),
        "CLOSE_ALL": ("CLOSE_ALL", "Closing ALL open positions..."),
        "DERISK": ("DERISK", "De-risking: SL→BE + partial close..."),
    }

    if action not in actions:
        await query.answer("Unknown action.")
        return

    ea_action, human_text = actions[action]

    params = {}
    if ea_action == "PARTIAL_CLOSE":
        pct = int(action.split("_")[1])
        params = {"percent": pct}
    elif ea_action == "DERISK":
        params = {
            "percent": DERISK_PARTIAL_PCT,
            "move_sl_to_be": DERISK_MOVE_SL_TO_BE,
        }

    # Extra confirmation for destructive actions
    if ea_action == "CLOSE_ALL":
        await query.answer()
        await query.message.reply_text(
            "⚠️ Are you sure you want to CLOSE ALL positions?",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "Yes, close everything", callback_data="CONFIRM_CLOSE_ALL"
                        ),
                        InlineKeyboardButton("Cancel", callback_data="CANCEL"),
                    ]
                ]
            ),
        )
        return

    await query.answer(human_text)
    cmd_id = push_command(session.account_id, ea_action, params)
    await query.message.reply_text(f"✅ {human_text}\n(command id: {cmd_id})")


async def confirm_close_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    chat_id = query.message.chat_id

    if not is_authorized(chat_id):
        await query.answer("Please /login first.", show_alert=True)
        return

    session = get_session(chat_id)
    await query.answer("Closing all positions...")
    cmd_id = push_command(session.account_id, "CLOSE_ALL", {})
    await query.message.reply_text(f"🔴 CLOSE ALL sent. (command id: {cmd_id})")


async def cancel_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("Cancelled.")
    await query.message.reply_text("Action cancelled.")


async def panel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not is_authorized(chat_id):
        await update.message.reply_text("Please /login first.")
        return
    await update.message.reply_text(
        "Your trade control panel:", reply_markup=control_panel_keyboard()
    )


# --------------------------------------------------------------------------
# MAIN
# --------------------------------------------------------------------------


def main():
    if BOT_TOKEN == "PUT_YOUR_BOT_TOKEN_HERE":
        raise SystemExit(
            "Set TELEGRAM_BOT_TOKEN env var or edit BOT_TOKEN in bot.py before running."
        )

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("login", login_cmd))
    app.add_handler(CommandHandler("logout", logout_cmd))
    app.add_handler(CommandHandler("panel", panel_cmd))

    app.add_handler(CallbackQueryHandler(confirm_close_all, pattern="^CONFIRM_CLOSE_ALL$"))
    app.add_handler(CallbackQueryHandler(cancel_action, pattern="^CANCEL$"))
    app.add_handler(CallbackQueryHandler(button_handler))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router))

    logger.info("Bot starting...")
    app.run_polling()


if __name__ == "__main__":
    main()
