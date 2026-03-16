import os
import asyncio
import datetime
import logging

from dotenv import load_dotenv
from pyrogram import Client, filters, enums
from pyrogram.types import Message, MessageEntity
from pyrogram.errors import UserNotParticipant, InviteHashInvalid, InviteHashExpired, FloodWait
from openai import AsyncOpenAI
from fastapi import FastAPI, Request
import uvicorn
from database import init_db, log_user, get_available_trial_key, add_trial_keys, update_lead_status, update_last_followup, get_users_for_followup, get_users_for_trial_followup, mark_trial_followup_sent

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("bot")

# Load environment variables
load_dotenv(override=True)

API_ID = os.getenv("API_ID")
API_HASH = os.getenv("API_HASH")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL")
AI_MODEL = os.getenv("AI_MODEL", "sur-mistral")
OWNER_ID = os.getenv("OWNER_ID")
if OWNER_ID:
    OWNER_ID = int(OWNER_ID)

# Ensure required config exists
if not API_ID or not API_HASH:
    logger.critical("API_ID or API_HASH missing from environment! Bot cannot start.")
    exit(1)

if not OPENAI_API_KEY:
    logger.critical("OPENAI_API_KEY missing from environment! AI will not work.")
    exit(1)

# Initialize database
init_db()

SYSTEM_PROMPT = ""
# Using standard professional emojis
EMOJI_SECURITY = "🔒"
EMOJI_FAST = "⚡"
EMOJI_CHECK = "✅"
EMOJI_JOIN = "📥"
EMOJI_SAD = "🥺"
EMOJI_LINK = "🔗"
EMOJI_VIP = "👑"
EMOJI_HACK = "🛠"

def load_system_prompt():
    global SYSTEM_PROMPT
    if not os.path.exists("system_prompt.txt"):
        with open("system_prompt.txt", "w", encoding="utf-8") as f:
            f.write("You are an AI sales assistant. Be polite and helpful.")

    with open("system_prompt.txt", "r", encoding="utf-8") as f:
        base_system_prompt = f.read()

    learned_knowledge = ""
    if os.path.exists("learned_knowledge.txt"):
        with open("learned_knowledge.txt", "r", encoding="utf-8") as f:
            learned_knowledge = f"\n\n### LEARNED BEHAVIORS FROM OWNER OVERRIDES:\n{f.read()}\n"

    # Add instruction for granting trial, CRM tagging, and sales strategy
    SYSTEM_PROMPT = base_system_prompt + learned_knowledge + """
    
### SALES STRATEGY & PERSONALITY:
- **SOFT SELL**: Never push hacks or buying to the user immediately.
- Act as a professional gaming assistant.
- Only discuss hacks, features, or pricing if the user explicitly asks about them or expresses interest in purchasing.
- Use a helpful, non-aggressive tone.
- Formatting: Use **bold** for emphasis and `code blocks` for keys or specific instructions.

### LEAD STATUS RULES:
- If a user just chats normally without interest in buying, keep them [STATUS_NEW].
- Only mark as [STATUS_INTERESTED] if they ask about buying, prices, or trials.

CRITICAL INSTRUCTION FOR GRANTING TRIALS:
If the user asks for a trial and you decide to grant it based on the business logic, you MUST include the exact string:
[GRANT_TRIAL]
anywhere in your response. The system will detect this string, fetch a real trial key from the database, and inject it along with download instructions. You do not need to provide the apk links yourself, just include [GRANT_TRIAL] in a natural conversational flow.

CRITICAL INSTRUCTION FOR LEAD TRACKING:
You are also a CRM. You must silently classify the user's lead status and append exactly one of the following tags at the VERY END of your message (after a newline):
[STATUS_NEW] - if they are just exploring
[STATUS_INTERESTED] - if they are highly interested, asked for price/payment, or took a trial
[STATUS_BOUGHT] - if they have completed a purchase or sent a payment screenshot
[STATUS_DEAD] - if they clearly stated they do not want it, or are abusive/unresponsive
Do not explain the tag, just append it.
"""

load_system_prompt()

# Initialize AI Client
ai_client_kwargs = {"api_key": OPENAI_API_KEY}
if OPENAI_BASE_URL:
    ai_client_kwargs["base_url"] = OPENAI_BASE_URL

ai_client = AsyncOpenAI(**ai_client_kwargs)

# Initialize Pyrogram Client
SESSION_STRING = os.getenv("SESSION_STRING", "")
app = Client(
    "sales_assistant_session",
    session_string=SESSION_STRING,
    api_id=int(API_ID),
    api_hash=API_HASH
)

# Initialize FastAPI for Render health checks
fastapi_app = FastAPI()

@fastapi_app.get("/")
async def health_check():
    return {"status": "Rino Mods Bot is LIVE!", "user": "Rino"}

# Simple in-memory history tracker (store last 10 messages max per user)
user_histories = {}
PAUSED_USERS = {} # user_id: datetime of last manual owner message
bot_sent_messages = set() # Store messages sent by bot to ignore in outgoing handler

async def render_and_send(chat_id, text, parse_mode=enums.ParseMode.MARKDOWN):
    """Sends a formatted message with standard Markdown support."""
    return await app.send_message(
        chat_id=chat_id,
        text=text,
        parse_mode=parse_mode,
        disable_web_page_preview=True
    )

async def learn_from_owner(owner_text):
    prompt = f"You are a master AI sales strategist. The human owner of the BGMI mod business just manually replied this to a client: '{owner_text}'. Extract any new sales strategies, behavior tones, or pricing rules from this message that isn't already obvious. Return a concise 1-2 sentence rule to add to our AI behavior guide. If it's just a generic greeting, short confirmation, or 'ok', return 'IGNORE'."
    try:
        response = await ai_client.chat.completions.create(
            model=AI_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
            max_tokens=100
        )
        rule = response.choices[0].message.content.strip()
        if rule and rule != "IGNORE" and not rule.upper().startswith("IGNORE"):
            with open("learned_knowledge.txt", "a", encoding="utf-8") as f:
                f.write(f"- {rule}\n")
            logger.info(f"Learned new rule from owner interaction: {rule}")
            load_system_prompt()
    except Exception as e:
        logger.error(f"Failed to learn from owner message: {e}")

def update_history(user_id, role, content):
    if user_id not in user_histories:
        user_histories[user_id] = [{"role": "system", "content": SYSTEM_PROMPT}]
    user_histories[user_id].append({"role": role, "content": content})
    # Keep system prompt + last 10 messages to save context limit
    if len(user_histories[user_id]) > 11:
        # keep system prompt, then the last 10
        user_histories[user_id] = [user_histories[user_id][0]] + user_histories[user_id][-10:]

# TRACKING FOR SMART PAUSE
PENDING_REPLIES = {} # user_id: message_id (to verify if still the last message)

async def get_ai_reply(user_id):
    history = user_histories.get(user_id, [{"role": "system", "content": SYSTEM_PROMPT}])
    try:
        response = await ai_client.chat.completions.create(
            model=AI_MODEL,
            messages=history,
            temperature=0.6, # slightly lower temp for more concise answers
            max_tokens=100 # Strictly enforce short responses
        )
        return response.choices[0].message.content
    except Exception as e:
        logger.error(f"Detailed OpenAI Error: {e}")
        return "I am currently having some issues connecting to my database. Let me help you shortly."

@app.on_chat_member_updated()
async def handle_member_update(client: Client, update):
    # Only track our specific channel
    channel_id_or_link = os.getenv("CHANNEL_ID", "https://t.me/+tOPAVpqhvp5jMTI9")
    if str(channel_id_or_link).startswith("-100"):
        channel_id_or_link = int(channel_id_or_link)
    
    if update.chat.id != channel_id_or_link:
        return

    # Check if user left
    if update.old_chat_member and update.new_chat_member:
        if update.old_chat_member.status not in [enums.ChatMemberStatus.LEFT, enums.ChatMemberStatus.BANNED] and \
           update.new_chat_member.status == enums.ChatMemberStatus.LEFT:
            
            user_id = update.new_chat_member.user.id
            username = getattr(update.new_chat_member.user, 'first_name', 'Bhai')
            
            print(f"User {user_id} left the channel.")
            update_lead_status(user_id, "DEAD")
            
            retention_text = (
                f"**Bhai {username}, Aapne Channel Kyun Chhoda?** {EMOJI_SAD}\n\n"
                f"Mere channel `Rino Mods` par hi saare `Daily Proofs` aur `Safe Hacks` milte hain.\n\n"
                "Ek baar phir soch lo aur wapas join kar lo taaki aap updates miss na karo! 🤝\n\n"
                f"{EMOJI_LINK} **Join Back Now:** https://t.me/+tOPAVpqhvp5jMTI9\n\n"
                "Kuch puchna ho toh yahi batana."
            )
            try:
                await render_and_send(user_id, retention_text)
                logger.info(f"Sent retention DM to user {user_id} who left the channel.")
            except Exception as e:
                logger.warning(f"Failed to send retention DM to {user_id}: {e}")

@app.on_message(filters.private & ~filters.me)
async def handle_new_message(client: Client, message: Message):
    user = message.from_user
    if not user or user.is_bot:
        return
        
    user_id = user.id
    username = getattr(user, 'username', 'Unknown')
    text = message.text or message.caption or ""
    text = text.strip()
    
    # Admin commands
    if OWNER_ID and user_id == OWNER_ID:
        if text.startswith("/add"):
            # Supports /addkeys or just /add
            parts = text.split(maxsplit=1)
            if len(parts) > 1:
                # Add support for comma separated or newline separated keys
                keys_raw = parts[1].replace('\\n', ' ').replace(',', ' ')
                keys = [k.strip() for k in keys_raw.split() if k.strip()]
                if keys:
                    added = add_trial_keys(keys)
                    await message.reply_text(f"✅ Master, I successfully added {added} new unique trial keys to the database!")
                else:
                    await message.reply_text("❌ No valid keys found. Format: `/add key1 key2`")
            else:
                await message.reply_text("Usage: `/add key1 key2...` (or keys on new lines/commas)")
            return
            
        if text.startswith("/prompt"):
            parts = text.split(maxsplit=1)
            if len(parts) > 1:
                new_instruction = parts[1].strip()
                with open("system_prompt.txt", "a", encoding="utf-8") as f:
                    f.write(f"\n- NEW OWNER RULE: {new_instruction}")
                load_system_prompt()
                user_histories.clear()
                await message.reply_text(f"✅ Master, I have updated my instructions with: '{new_instruction}'")
            else:
                await message.reply_text("Usage: `/prompt <your new rule here>` (to quickly add a rule without uploading a file)")
            return
            
        if message.document and (text == "/setprompt" or text == "/settraining"):
            if message.document.file_name and message.document.file_name.endswith(".txt"):
                await message.download(file_name="system_prompt.txt")
                load_system_prompt()
                user_histories.clear()
                await message.reply_text("✅ New training data updated successfully! Chat memory reset.")
            else:
                await message.reply_text("⚠️ Please send a `.txt` file with the caption `/settraining` or `/setprompt`.")
            return

        if text.startswith("/status"):
            parts = text.split(maxsplit=2)
            if len(parts) == 3:
                target_user = parts[1]
                new_status = parts[2].upper()
                if new_status in ['NEW', 'INTERESTED', 'BOUGHT', 'DEAD']:
                    update_lead_status(target_user, new_status)
                    await message.reply_text(f"✅ User `{target_user}` status manually updated to `{new_status}`")
                else:
                    await message.reply_text("⚠️ Invalid status. Use NEW, INTERESTED, BOUGHT, or DEAD.")
            else:
                await message.reply_text("Usage: `/status <user_id> <STATUS>`")
            return

        if text == "/autopost" or text.startswith("/autopost"):
            await message.reply_text("ℹ️ Autopost feature has been disabled by the Master.")
            return

    if not text:
        return

    print(f"New message from {username} ({user_id}): {text}")
    print(f"DEBUG: Logging user {user_id}...")
    log_user(user_id, username)

    # --- OWNER ACTIVE CHECK ---
    owner_online = False
    if OWNER_ID:
        try:
            owner = await client.get_users(OWNER_ID)
            if owner.status == enums.UserStatus.ONLINE:
                owner_online = True
        except Exception: pass

    # --- SMART DELAY / PAUSE LOGIC ---
    if user_id in PAUSED_USERS:
        if datetime.datetime.now() < PAUSED_USERS[user_id]:
            print(f"Skipping AI reply for {user_id} - Manually Paused by Owner.")
            return

    if owner_online:
        logger.info(f"Owner is ONLINE. Waiting 10s before AI reply to {user_id} to allow manual response...")
        PENDING_REPLIES[user_id] = message.id
        await asyncio.sleep(10) 
        
        # Check if another message from owner appeared, or if user sent more (we only care about the last one)
        if PENDING_REPLIES.get(user_id) != message.id:
            return # A newer message came or owner replied
            
        # Verify owner didn't reply in the last 5 mins
        async for msg in client.get_chat_history(user_id, limit=1):
            if msg.from_user and msg.from_user.id == OWNER_ID:
                print(f"Owner replied to {user_id} during wait. AI staying silent.")
                PAUSED_USERS[user_id] = datetime.datetime.now() + datetime.timedelta(hours=1)
                return
    else:
        # Owner is offline/recent - small realistic delay
        await asyncio.sleep(5)
    
    # --- CHANNEL MEMBERSHIP CHECK ---
    # We must skip the owner from this check to ensure admin commands always work
    if not (OWNER_ID and user_id == OWNER_ID):
        try:
            # First, check if the admin has set a specific CHANNEL_ID in their env
            # If not, we try to use the invite link (which might fail depending on Pyrogram version/account type)
            channel_id_or_link = os.getenv("CHANNEL_ID", "https://t.me/+tOPAVpqhvp5jMTI9")
            
            # Convert to int if it's a negative ID string like "-100..."
            if str(channel_id_or_link).startswith("-100"):
                channel_id_or_link = int(channel_id_or_link)

            print(f"DEBUG: Checking channel membership for {user_id} in {channel_id_or_link}...")
            member = await app.get_chat_member(chat_id=channel_id_or_link, user_id=user_id)
            print(f"DEBUG: Member status: {member.status}")
            if member.status in [enums.ChatMemberStatus.LEFT, enums.ChatMemberStatus.BANNED]:
                raise ValueError("Not joined")
                
        except (ValueError, UserNotParticipant) as e:
            join_text = (
                f"{EMOJI_SECURITY} **Rino Mods: Entry Restricted** {EMOJI_SECURITY}\n\n"
                "Bhai, aage baat karne se pehle mera **VIP Channel** join karein!\n"
                "Waha aapko saare `Real Proofs` aur `Feedback` mil jayenge.\n\n"
                f"{EMOJI_LINK} **Join Rino Mods:** https://t.me/+tOPAVpqhvp5jMTI9\n\n"
                f"Join karne ke baad msg karo. {EMOJI_CHECK}"
            )
            await render_and_send(user_id, join_text)
            return
        except Exception as e:
            # If we get USERNAME_INVALID, it means Pyrogram can't parse the private invite link as a chat_id.
            # We catch it here so the bot doesn't crash, and we log an error for the owner.
            if "USERNAME_INVALID" in str(e):
                print("⚠️ [CHANNEL CHECK FAILED]: Pyrogram cannot use private invite links as chat IDs. "
                      "Please add CHANNEL_ID=-100xxxxxxxx to your .env file with the actual private channel ID.")
            else:
                print(f"Failed to check channel membership: {e}")
    # --- END CHANNEL CHECK ---

    # Show typing status
    # Pyrogram equivalent for typing action
    await client.send_chat_action(chat_id=user_id, action=enums.ChatAction.TYPING)
    print(f"DEBUG: Getting AI reply for {user_id}...")
    ai_reply = await get_ai_reply(user_id)
    print(f"DEBUG: AI Reply: {ai_reply[:50]}...")
    
    # Check if AI attempted to grant a trial
    if "[GRANT_TRIAL]" in ai_reply:
        print(f"AI requested trial key for {user_id}")
        key_result = get_available_trial_key(user_id)
        
        if key_result == "ALREADY_GIVEN":
            ai_reply = ai_reply.replace(
                "[GRANT_TRIAL]", 
                "Bhai, aapko pehle hi trial key mil chuki hai. Ek user ko sirf ek baar hi trial milta hai. Agar koi setup issue hai toh @rinnosetup check karo."
            )
        elif key_result == "NO_KEYS_AVAILABLE":
            ai_reply = ai_reply.replace(
                "[GRANT_TRIAL]", 
                "Abhi mere paas trial keys khatam ho gayi hain. Master (Rino) ko refill karne do, thoda wait karo."
            )
        else:
            instruction = (
                f"**Aapki Trial Key:** `{key_result}` (5 Hours)\n\n"
                f"{EMOJI_FAST} **APK Download:** https://t.me/+zVnW27n9FnA3Zjdl\n"
                f"{EMOJI_HACK} **Problem in Setup/Login?** Check videos here: https://t.me/rinnosetup\n\n"
                "Setup karke game enjoy karo! 🎮"
            )
            ai_reply = ai_reply.replace("[GRANT_TRIAL]", instruction)
            logger.info(f"Success: Trial key injected for user {user_id}")

    # Extract status tag
    status_tag = None
    for tag, status in [("[STATUS_NEW]", "NEW"), ("[STATUS_INTERESTED]", "INTERESTED"), ("[STATUS_BOUGHT]", "BOUGHT"), ("[STATUS_DEAD]", "DEAD")]:
        if tag in ai_reply:
            status_tag = status
            ai_reply = ai_reply.replace(tag, "").strip()
            break
    
    if status_tag:
        update_lead_status(user_id, status_tag)
        update_last_followup(user_id) # Reset followup timer on any new interaction
        print(f"[{user_id}] Status updated to: {status_tag}")

    bot_sent_messages.add(ai_reply.strip())
    update_history(user_id, "assistant", ai_reply)
    try:
        await message.reply_text(ai_reply)
        logger.info(f"AI replied to {user_id} ({username})")
    except Exception as e:
        logger.error(f"Failed to send message to {user_id}: {e}")

@app.on_message(filters.me & filters.private)
async def handle_outgoing_message(client: Client, message: Message):
    text = message.text or message.caption or ""
    if not text:
        return
    text = text.strip()
    
    # If this is an exact match for what the bot just sent, ignore it
    if text in bot_sent_messages:
        bot_sent_messages.remove(text)
        return
        
    # Otherwise, it means the OWNER manually typed a message!
    user_id = message.chat.id
    
    # Pause AI for this user for 1 hour since owner is talking
    PAUSED_USERS[user_id] = datetime.datetime.now() + datetime.timedelta(hours=1)
    PENDING_REPLIES[user_id] = 0 # Cancel any pending AI waits
    print(f"Owner manually messaged {user_id}. AI paused for 1 hour.")

    # Commands from owner
    if text.startswith("/"):
        if text.startswith("/add"):
            # Supports /addkeys or just /add
            parts = text.split(maxsplit=1)
            if len(parts) > 1:
                # Add support for comma separated or newline separated keys
                keys_raw = parts[1].replace('\\n', ' ').replace(',', ' ')
                keys = [k.strip() for k in keys_raw.split() if k.strip()]
                if keys:
                    added = add_trial_keys(keys)
                    await message.reply_text(f"✅ Master, I successfully added {added} new unique trial keys to the database!")
                else:
                    await message.reply_text("❌ No valid keys found. Format: `/add key1 key2`")
            else:
                await message.reply_text("Usage: `/add key1 key2...` (or keys on new lines/commas)")
            return
            
        if text.startswith("/prompt"):
            parts = text.split(maxsplit=1)
            if len(parts) > 1:
                new_instruction = parts[1].strip()
                with open("system_prompt.txt", "a", encoding="utf-8") as f:
                    f.write(f"\n- NEW OWNER RULE: {new_instruction}")
                load_system_prompt()
                user_histories.clear()
                await message.reply_text(f"✅ Master, I have updated my instructions with: '{new_instruction}'")
            else:
                await message.reply_text("Usage: `/prompt <your new rule here>` (to quickly add a rule without uploading a file)")
            return
            
        if message.document and (text == "/setprompt" or text == "/settraining"):
            if message.document.file_name and message.document.file_name.endswith(".txt"):
                await message.download(file_name="system_prompt.txt")
                load_system_prompt()
                user_histories.clear()
                await message.reply_text("✅ New training data updated successfully! Chat memory reset.")
            else:
                await message.reply_text("⚠️ Please send a `.txt` file with the caption `/settraining` or `/setprompt`.")
            return

        if text.startswith("/status"):
            parts = text.split(maxsplit=2)
            if len(parts) == 3:
                target_user = parts[1]
                new_status = parts[2].upper()
                if new_status in ['NEW', 'INTERESTED', 'BOUGHT', 'DEAD']:
                    update_lead_status(target_user, new_status)
                    await message.reply_text(f"✅ User `{target_user}` status manually updated to `{new_status}`")
                else:
                    await message.reply_text("⚠️ Invalid status. Use NEW, INTERESTED, BOUGHT, or DEAD.")
            else:
                await message.reply_text("Usage: `/status <user_id> <STATUS>`")
            return

        if text == "/autopost" or text.startswith("/autopost"):
            await message.reply_text("ℹ️ Autopost feature has been disabled by the Master.")
            return

        return
    
    # Learn from this chat silently in the background
    asyncio.create_task(learn_from_owner(text))


# WEBHOOK ENTRYPOINTS FOR VERCEL
async def init_bot():
    """Initializes Pyrogram client for Webhooks (called from api/index.py)"""
    if not app.is_initialized:
        await app.start()

async def process_webhook_update(update_data: dict):
    """Feeds external dictionary updates from Vercel into Pyrogram dispatcher"""
    try:
        if not app.is_initialized:
            await app.start()
        
        # Pyrogram doesn't have a native "process raw update dict" for userbots easily.
        # We handle this by parsing the update using Pyrogram's internal parser.
        update = app.parse_update(update_data)
        if update:
            await app.dispatcher.handler_worker(update)
    except Exception as e:
        print("Webhook Handle Error:", e)

# If running locally normally via long polling (not Vercel)
async def background_jobs():
    while True:
        # Every 30 minutes, check for trial follow-ups
        await asyncio.sleep(1800)
        print("Checking for 5-hour trial follow-ups...")
        try:
            users_to_followup = get_users_for_trial_followup(hours_threshold=5)
            for user_id, username in users_to_followup:
                followup_text = (
                    f"**Bhai {username}, Kaise chal raha hai hack?** {EMOJI_FAST}\n\n"
                    "Aapka 5-hour trial ab khatam hone wala hoga. Kaisa laga performance?\n\n"
                    "Agar aapko **Premium Access** chahiye toh yahan se buy kar sakte hain:\n"
                    f"{EMOJI_LINK} **Buy Link:** https://t.me/c/2182610245/843\n\n"
                    "Payment karke screenshot bhej dena, main verify karke access de dunga! 🚀"
                )
                try:
                    await render_and_send(user_id, followup_text)
                    mark_trial_followup_sent(user_id)
                    logger.info(f"Sent 5h trial followup to {user_id}")
                except Exception as e:
                    logger.warning(f"Failed trial followup to {user_id}: {e}")
        except Exception as e:
            logger.error(f"Background trial job processing error: {e}")

        # Standard CRM followups (24h+)
        try:
            crm_users = get_users_for_followup(hours_threshold=24)
            # ... process CRM followups if needed ...
        except: pass

async def main():
    print("DEBUG: Starting bot main() function...")
    print("Rino Mods Bot is starting via polling...")
    
    # Ensure the bot account itself is in the channel
    try:
        channel_id_or_link = os.getenv("CHANNEL_ID", "-1001752764171")
        await app.join_chat(channel_id_or_link)
        print(f"Successfully joined/verified channel: {channel_id_or_link}")
    except Exception as e:
        print(f"Note: Bot status in channel: {e}")

    asyncio.create_task(background_jobs())
    
    # Running uvicorn for Render health checks
    port = int(os.getenv("PORT", 10000))
    config = uvicorn.Config(fastapi_app, host="0.0.0.0", port=port)
    server = uvicorn.Server(config)
    
    # Run uvicorn server as a task
    asyncio.create_task(server.serve())
    
    from pyrogram import idle
    await idle()

if __name__ == "__main__":
    import logging
    import pyrogram.utils
    logging.getLogger("pyrogram").setLevel(logging.ERROR)
    
    # Monkey-patch to silently ignore Peer ID Invalid errors
    original_get_peer_type = pyrogram.utils.get_peer_type
    def safe_get_peer_type(peer_id):
        try:
            return original_get_peer_type(peer_id)
        except ValueError as e:
            if "Peer id invalid" in str(e):
                logger.debug(f"Handling Peer ID Invalid for {peer_id}")
                return "channel" 
            raise e
    pyrogram.utils.get_peer_type = safe_get_peer_type

    logger.info("Bot is starting...")
    app.run(main())
