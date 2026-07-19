import asyncio
import logging
import sys
import os
import time
from telethon import TelegramClient, errors, functions, types, events

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("claimer.log"),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

# --- CONFIGURATION ---
API_ID = os.getenv('TG_API_ID')
API_HASH = os.getenv('TG_API_HASH')
PHONE = os.getenv('TG_PHONE')
TWOFA_PASS = os.getenv('TG_TWOFA_PASS', '')
ALT_USERNAME = "infuckable"  # Alt account that can send commands and receives notifications
CHECK_INTERVAL = int(os.getenv('TG_INTERVAL', '120'))  # Default 2 minutes
# ---------------------

# Global state
targets = []
pending_command = None  # Stores (command_type, sender_user_id, sender_peer)
claiming = False  # Whether the monitoring loop is active


def load_usernames(filepath='usernames.txt'):
    """Loads usernames from a text file, one per line."""
    if not os.path.exists(filepath):
        return []

    with open(filepath, 'r') as f:
        usernames = [line.strip().replace('@', '') for line in f if line.strip()]
    return usernames


def save_usernames(usernames, filepath='usernames.txt'):
    """Saves usernames to a text file."""
    with open(filepath, 'w') as f:
        for u in usernames:
            f.write(u + '\n')


async def send_notification(client, message):
    """Sends a notification message to the alt account @infuckable."""
    try:
        alt = await client.get_entity(ALT_USERNAME)
        await client.send_message(alt, message)
        logger.info(f"Notification sent to @{ALT_USERNAME}: {message}")
    except Exception as e:
        logger.error(f"Failed to send notification to @{ALT_USERNAME}: {e}")


async def check_and_claim(client, username):
    """Checks if a username is available and attempts to claim it."""
    try:
        logger.info(f"Checking availability of @{username}...")

        result = await client(functions.account.CheckUsernameRequest(username=username))

        if result:
            logger.info(f"SUCCESS! @{username} is available. Attempting to claim...")
            try:
                await client(functions.account.UpdateUsernameRequest(username=username))
                logger.info(f"CONGRATULATIONS! @{username} has been claimed!")
                await send_notification(client, f"🎉 **CLAIMED!**\n\nSuccessfully acquired **@{username}**\nTime: {time.strftime('%Y-%m-%d %H:%M:%S')}")
                return True
            except errors.rpcerrorlist.UsernameOccupiedError:
                logger.warning(f"Failed to claim @{username}: Username occupied (someone was faster).")
            except Exception as e:
                logger.error(f"Error while claiming @{username}: {e}")
        else:
            logger.info(f"@{username} is still taken.")

    except errors.FloodWaitError as e:
        logger.warning(f"Flood wait: Must wait {e.seconds} seconds.")
        await send_notification(client, f"⚠️ **Flood Wait**\n\nChecking @{username} triggered a flood wait of **{e.seconds} seconds**.\nMonitoring paused until then.")
        await asyncio.sleep(e.seconds)
    except Exception as e:
        logger.error(f"Error checking @{username}: {e}")

    return False


async def send_reply(client, peer, message_text, reply_to=None):
    """Helper to reply to a message or send a message."""
    try:
        await client.send_message(peer, message_text, reply_to=reply_to)
    except Exception as e:
        logger.error(f"Failed to send reply: {e}")


async def monitoring_loop(client):
    """Main loop that checks usernames periodically."""
    global claiming
    claiming = True
    logger.info("Monitoring loop started.")
    await send_notification(client, "🟢 **Monitoring Started**\n\nUsername claimer is now running and checking targets.")

    while claiming and targets:
        for username in targets[:]:
            if not claiming:
                break
            claimed = await check_and_claim(client, username)
            if claimed:
                targets.remove(username)
                save_usernames(targets)

            await asyncio.sleep(5)

        if claiming and targets:
            logger.info(f"Waiting {CHECK_INTERVAL} seconds for next check...")
            await asyncio.sleep(CHECK_INTERVAL)
            targets = load_usernames()
        else:
            break

    if not targets:
        logger.info("All target usernames have been claimed or list is empty.")
        await send_notification(client, "✅ **All Done!**\n\nAll target usernames have been claimed or the list is empty.\nMonitoring stopped.")

    claiming = False


async def main():
    global targets, pending_command, claiming

    if not API_ID or not API_HASH:
        logger.error("API_ID and API_HASH must be set in environment variables.")
        return

    # Initialize the Telegram User Bot client
    client = TelegramClient('claimer_session', int(API_ID), API_HASH)

    try:
        # Start with 2FA password if set
        if TWOFA_PASS:
            await client.start(phone=PHONE, password=TWOFA_PASS)
        else:
            await client.start(phone=PHONE)
        logger.info("User bot client started successfully.")

        # Load initial targets
        targets = load_usernames()
        if targets:
            logger.info(f"Loaded {len(targets)} usernames from file.")
            await send_notification(client, f"📋 **Initial Targets Loaded**\n\n{len(targets)} usernames loaded from file:\n" + "\n".join(f"• @{u}" for u in targets))

        # ── MESSAGE HANDLER ──────────────────────────────────────────
        @client.on(events.NewMessage)
        async def handler(event):
            global pending_command, claiming, targets

            # Get sender info
            sender = await event.get_sender()
            if not sender or not hasattr(sender, 'username') or not sender.username:
                return  # Ignore messages from users without usernames

            sender_username = sender.username.lower()

            # Only accept messages from the main account itself or the alt account
            me = await client.get_me()
            allowed_users = {me.username.lower() if me.username else "", ALT_USERNAME.lower()}

            if sender_username not in allowed_users:
                return  # Ignore everyone else

            # ── If waiting for a username input (2-phase flow) ──
            if pending_command:
                cmd_type, cmd_sender_id, cmd_peer = pending_command

                # Only accept the follow-up from the same sender
                if sender.id != cmd_sender_id:
                    return

                username_input = event.message.text.strip().replace('@', '')

                if cmd_type == 'add':
                    if not username_input or '/' in username_input:
                        await send_reply(client, event.chat_id, "❌ Invalid username. Please send a valid username (e.g. `myname`).", reply_to=event.message.id)
                        pending_command = None
                        return

                    # Check if already in targets
                    if username_input.lower() in [u.lower() for u in targets]:
                        await send_reply(client, event.chat_id, f"⚠️ @{username_input} is already in the target list.", reply_to=event.message.id)
                    else:
                        targets.append(username_input)
                        save_usernames(targets)
                        await send_reply(client, event.chat_id, f"✅ **Added!**\n\n**@{username_input}** has been added to the target list.\n\nTotal targets: {len(targets)}", reply_to=event.message.id)
                        logger.info(f"Added @{username_input} to targets via Telegram command.")

                elif cmd_type == 'remove':
                    original = username_input.lower()
                    found = False
                    for i, u in enumerate(targets):
                        if u.lower() == original:
                            removed = targets.pop(i)
                            save_usernames(targets)
                            await send_reply(client, event.chat_id, f"🗑️ **Removed!**\n\n**@{removed}** has been removed from the target list.\n\nRemaining targets: {len(targets)}", reply_to=event.message.id)
                            logger.info(f"Removed @{removed} from targets via Telegram command.")
                            found = True
                            break
                    if not found:
                        await send_reply(client, event.chat_id, f"❌ @{username_input} was not found in the target list.", reply_to=event.message.id)

                pending_command = None
                return

            # ── Parse commands ───────────────────────────────────────
            msg_text = event.message.text.strip()

            if msg_text.startswith('/add'):
                # Check if username is provided inline
                parts = msg_text.split(maxsplit=1)
                if len(parts) > 1:
                    username_input = parts[1].strip().replace('@', '')
                    if username_input.lower() in [u.lower() for u in targets]:
                        await send_reply(client, event.chat_id, f"⚠️ @{username_input} is already in the target list.", reply_to=event.message.id)
                    else:
                        targets.append(username_input)
                        save_usernames(targets)
                        await send_reply(client, event.chat_id, f"✅ **Added!**\n\n**@{username_input}** has been added to the target list.\n\nTotal targets: {len(targets)}", reply_to=event.message.id)
                        logger.info(f"Added @{username_input} via inline /add command.")
                else:
                    # 2-phase: wait for username
                    pending_command = ('add', sender.id, event.chat_id)
                    await send_reply(client, event.chat_id, "📝 **Send the username** you want to add (without the @):\n\n_Example: mynewname_", reply_to=event.message.id)

            elif msg_text.startswith('/remove') or msg_text.startswith('/delete'):
                parts = msg_text.split(maxsplit=1)
                if len(parts) > 1:
                    username_input = parts[1].strip().replace('@', '')
                    original = username_input.lower()
                    found = False
                    for i, u in enumerate(targets):
                        if u.lower() == original:
                            removed = targets.pop(i)
                            save_usernames(targets)
                            await send_reply(client, event.chat_id, f"🗑️ **Removed!**\n\n**@{removed}** has been removed from the target list.\n\nRemaining targets: {len(targets)}", reply_to=event.message.id)
                            logger.info(f"Removed @{removed} via inline /remove command.")
                            found = True
                            break
                    if not found:
                        await send_reply(client, event.chat_id, f"❌ @{username_input} was not found in the target list.", reply_to=event.message.id)
                else:
                    # 2-phase: wait for username
                    pending_command = ('remove', sender.id, event.chat_id)
                    await send_reply(client, event.chat_id, "🗑️ **Send the username** you want to remove (without the @):\n\n_Example: oldname_", reply_to=event.message.id)

            elif msg_text.startswith('/list') or msg_text.startswith('/targets'):
                if targets:
                    target_list = "\n".join(f"  {i+1}. @{u}" for i, u in enumerate(targets))
                    status = "🟢 Running" if claiming else "🔴 Stopped"
                    await send_reply(client, event.chat_id, f"📋 **Target List** ({len(targets)} usernames)\n\n{target_list}\n\nStatus: {status}", reply_to=event.message.id)
                else:
                    await send_reply(client, event.chat_id, "📭 **No targets in the list.**\n\nUse `/add` to add usernames to monitor.", reply_to=event.message.id)

            elif msg_text.startswith('/start'):
                if claiming:
                    await send_reply(client, event.chat_id, "⚠️ Monitoring is already running.", reply_to=event.message.id)
                else:
                    current_targets = load_usernames()
                    if not current_targets:
                        await send_reply(client, event.chat_id, "❌ No targets in the list. Add some with `/add` first.", reply_to=event.message.id)
                    else:
                        # Start monitoring in background
                        asyncio.ensure_future(monitoring_loop(client))
                        await send_reply(client, event.chat_id, f"🟢 **Monitoring Started!**\n\nNow watching {len(current_targets)} username(s).\nYou will be notified on @{ALT_USERNAME} when one is claimed.", reply_to=event.message.id)

            elif msg_text.startswith('/stop'):
                if claiming:
                    claiming = False
                    await send_reply(client, event.chat_id, "🔴 **Monitoring Stopped.**", reply_to=event.message.id)
                    await send_notification(client, "🔴 **Monitoring Stopped**\n\nUsername claimer has been stopped.")
                else:
                    await send_reply(client, event.chat_id, "⚠️ Monitoring is not currently running.", reply_to=event.message.id)

            elif msg_text.startswith('/status'):
                status = "🟢 Running" if claiming else "🔴 Stopped"
                target_count = len(targets)
                pending = "Yes (waiting for input)" if pending_command else "No"
                await send_reply(client, event.chat_id, f"📊 **Bot Status**\n\nMonitoring: {status}\nTargets: {target_count}\nPending input: {pending}", reply_to=event.message.id)

            elif msg_text.startswith('/help'):
                help_text = """🤖 **Telegram Username Claimer**

**Commands:**

`/add [username]` — Add a username to the target list
  • Send `/add` alone, then send the username in the next message (2-phase)
  • Or send `/add username` directly

`/remove [username]` — Remove a username from the target list
  • Same 2-phase flow as /add

`/list` — Show all current target usernames

`/start` — Start monitoring (checks usernames periodically)

`/stop` — Stop monitoring

`/status` — Check current bot status

`/help` — Show this help message

**Notes:**
• Only you and **@infuckable** can control this bot
• When a username is claimed, you'll get a notification on **@infuckable**
• Targets are saved to `usernames.txt`
• Check interval: **""" + str(CHECK_INTERVAL) + """ seconds**"""
                await send_reply(client, event.chat_id, help_text, reply_to=event.message.id)

        # ── Start the client event loop ─────────────────────────────
        logger.info("Client is running. Waiting for messages...")
        await client.run_until_disconnected()

    except Exception as e:
        logger.error(f"An unexpected error occurred: {e}")
    finally:
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
