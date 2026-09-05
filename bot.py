import os
import json
import asyncio
from datetime import datetime
import discord
from discord import app_commands
from discord.ext import commands
from aiohttp import web
from dotenv import load_dotenv
from google import genai
from google.genai.errors import APIError

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
PORT = int(os.getenv("PORT", 8080))
CONFIG_FILE = "bot_config.json"

# Initialize Gemini Client
gemini_client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None
GEMINI_MODEL = "gemini-2.0-flash"

# Intents setup
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# ----------------- CONFIG & MEMORY STORAGE -----------------

config = {
    "chat_channels": [],       # Allowed regular chat channels
    "log_channel_id": None,    # Channel for system and error logs
    "summary_channel_id": None # Channel for automated summaries
}

# Buffer for automated summarization: ~10,000 chars +- 500 chars (9500 to 10500)
summary_buffer = {
    "total_chars": 0,
    "messages": []  # [{"id": id, "url": url, "timestamp": dt, "author": str, "content": str}]
}

def load_config():
    global config
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r") as f:
                config.update(json.load(f))
        except Exception as e:
            print(f"Error loading config: {e}")

def save_config():
    try:
        with open(CONFIG_FILE, "w") as f:
            json.dump(config, f, indent=2)
    except Exception as e:
        print(f"Error saving config: {e}")

load_config()

# ----------------- LOGGING HELPER -----------------

async def send_log(message: str, error: bool = False):
    """Prints to console and forwards to Discord log channel if configured."""
    timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    prefix = "[ERROR]" if error else "[INFO]"
    formatted = f"`{timestamp}` **{prefix}** {message}"
    print(f"[{timestamp}] {prefix} {message}")

    if config["log_channel_id"]:
        channel = bot.get_channel(config["log_channel_id"])
        if channel:
            try:
                # Discord 2000-char safety check
                await channel.send(formatted[:1950])
            except Exception as e:
                print(f"Failed to send to log channel: {e}")

# ----------------- REACTION HELPERS -----------------

EMOJI_BED = "🛏️"
EMOJI_ZZZ = "💤"
EMOJI_YAWN = "🥱"
EMOJI_CHECK = "✅"
EMOJI_HOURGLASS = "⏳"
EMOJI_ERROR = "❌"

async def safe_remove_reaction(message: discord.Message, emoji_str: str):
    try:
        await message.remove_reaction(emoji_str, bot.user)
    except Exception:
        pass

async def safe_add_reaction(message: discord.Message, emoji_str: str):
    try:
        await message.add_reaction(emoji_str)
    except Exception:
        pass

# ----------------- DISCORD SLASH COMMANDS -----------------

@bot.tree.command(name="set_chat_channel", description="Toggle or set an allowed channel for Gemini chat.")
@app_commands.describe(channel="Select the text channel")
async def set_chat_channel(interaction: discord.Interaction, channel: discord.TextChannel):
    if channel.id in config["chat_channels"]:
        config["chat_channels"].remove(channel.id)
        action = "removed from"
    else:
        config["chat_channels"].append(channel.id)
        action = "added to"
    save_config()
    await interaction.response.send_message(f"Channel {channel.mention} {action} allowed chat channels.", ephemeral=True)
    await send_log(f"Chat channel updated: {channel.name} ({channel.id}) {action} list.")

@bot.tree.command(name="set_log_channel", description="Set the channel where system and error logs are sent.")
@app_commands.describe(channel="Select the log channel")
async def set_log_channel(interaction: discord.Interaction, channel: discord.TextChannel):
    config["log_channel_id"] = channel.id
    save_config()
    await interaction.response.send_message(f"Log channel set to {channel.mention}.", ephemeral=True)
    await send_log(f"System log channel changed to {channel.name} ({channel.id}).")

@bot.tree.command(name="set_summary_channel", description="Set the channel where automated summaries are posted.")
@app_commands.describe(channel="Select the summary channel")
async def set_summary_channel(interaction: discord.Interaction, channel: discord.TextChannel):
    config["summary_channel_id"] = channel.id
    save_config()
    await interaction.response.send_message(f"Summary channel set to {channel.mention}.", ephemeral=True)
    await send_log(f"Summary channel changed to {channel.name} ({channel.id}).")

@bot.tree.command(name="bot_status", description="Check active channels and summary buffer status.")
async def bot_status(interaction: discord.Interaction):
    chat_ch_mentions = ", ".join([f"<#{cid}>" for cid in config["chat_channels"]]) or "None"
    log_ch = f"<#{config['log_channel_id']}>" if config["log_channel_id"] else "None"
    sum_ch = f"<#{config['summary_channel_id']}>" if config["summary_channel_id"] else "None"

    embed = discord.Embed(title="Gemini Bot Status", color=discord.Color.blue())
    embed.add_field(name="Chat Channels", value=chat_ch_mentions, inline=False)
    embed.add_field(name="Log Channel", value=log_ch, inline=True)
    embed.add_field(name="Summary Channel", value=sum_ch, inline=True)
    embed.add_field(name="Summary Buffer", value=f"{summary_buffer['total_chars']} / 10,000 chars", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)

# ----------------- AUTOMATED SUMMARIZATION -----------------

async def process_summary_buffer():
    """Generates a <= 500-char summary when character threshold is reached."""
    if not config["summary_channel_id"] or not summary_buffer["messages"]:
        return

    summary_channel = bot.get_channel(config["summary_channel_id"])
    if not summary_channel:
        return

    messages_to_summarize = list(summary_buffer["messages"])
    # Clear buffer for next cycle
    summary_buffer["messages"] = []
    summary_buffer["total_chars"] = 0

    first_msg = messages_to_summarize[0]
    last_msg = messages_to_summarize[-1]

    start_time = first_msg["timestamp"].strftime("%Y-%m-%d %H:%M")
    end_time = last_msg["timestamp"].strftime("%Y-%m-%d %H:%M")

    # Header containing links and timestamps
    header = f"🔗 [Start]({first_msg['url']}) ({start_time}) ➔ [End]({last_msg['url']}) ({end_time})\n"
    
    # Calculate strict remaining character budget (Must be <= 500 characters TOTAL)
    max_summary_length = 500 - len(header) - 10
    if max_summary_length < 50:
        max_summary_length = 50

    # Build conversation dump for Gemini
    transcript = "\n".join([f"{m['author']}: {m['content']}" for m in messages_to_summarize])

    prompt = (
        f"Provide a concise summary of the following conversation. "
        f"STRICT LIMIT: The summary MUST NOT exceed {max_summary_length} characters. "
        f"Be direct and capture the core points.\n\nConversation:\n{transcript}"
    )

    try:
        response = await asyncio.to_thread(
            gemini_client.models.generate_content,
            model=GEMINI_MODEL,
            contents=prompt
        )
        summary_text = (response.text or "No summary generated.").strip()

        # Hard trim to ensure absolute <= 500 char compliance
        total_post = header + summary_text
        if len(total_post) > 500:
            total_post = total_post[:497] + "..."

        await summary_channel.send(total_post)
        await send_log(f"Auto-summary posted ({len(total_post)} chars).")

    except Exception as e:
        await send_log(f"Error during summarization: {e}", error=True)

# ----------------- MESSAGE HANDLING & CONTEXT -----------------

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    # Process slash commands
    await bot.process_commands(message)

    is_thread = isinstance(message.channel, discord.Thread)
    is_allowed_chat = message.channel.id in config["chat_channels"]
    thread_parent_allowed = is_thread and (message.channel.parent_id in config["chat_channels"])

    # Only process if in allowed channel, thread under allowed channel, or bot is directly mentioned
    should_respond = is_allowed_chat or thread_parent_allowed or bot.user.mentioned_in(message)
    if not should_respond:
        return

    # Track message characters for automated summarization
    summary_buffer["total_chars"] += len(message.content)
    summary_buffer["messages"].append({
        "id": message.id,
        "url": message.jump_url,
        "timestamp": message.created_at,
        "author": message.author.display_name,
        "content": message.content
    })

    # Check trigger threshold: 10,000 +- 500 chars (>= 9500)
    if summary_buffer["total_chars"] >= 9500:
        asyncio.create_task(process_summary_buffer())

    # --- REACTION LIFECYCLE SEQUENCE ---
    # 1. Awake from sleep
    await safe_add_reaction(message, EMOJI_YAWN)
    await asyncio.sleep(0.3)

    # 2. Chat received & acknowledged (remove yawn, add checkmark)
    await safe_remove_reaction(message, EMOJI_YAWN)
    await safe_add_reaction(message, EMOJI_CHECK)

    # 3. Processing (add hourglass)
    await safe_add_reaction(message, EMOJI_HOURGLASS)

    try:
        # Build prompt & context
        contents = []

        if is_thread:
            # Thread Context: fetch as much context as reasonably possible (e.g. up to 60 messages)
            messages_history = []
            async for h_msg in message.channel.history(limit=60, oldest_first=True):
                role = "model" if h_msg.author == bot.user else "user"
                clean_text = h_msg.clean_content.replace(f"@{bot.user.name}", "").strip()
                if clean_text:
                    messages_history.append(f"{h_msg.author.display_name} ({role}): {clean_text}")

            context_block = "\n".join(messages_history)
            prompt = f"The following is the conversation history in this thread:\n{context_block}\n\nPlease respond to the latest query."
            contents = prompt
        else:
            # Main channel context: fetch last 10 messages
            recent_msgs = []
            async for h_msg in message.channel.history(limit=10, oldest_first=True):
                role = "model" if h_msg.author == bot.user else "user"
                clean_text = h_msg.clean_content.replace(f"@{bot.user.name}", "").strip()
                if clean_text:
                    recent_msgs.append(f"{h_msg.author.display_name}: {clean_text}")

            context_block = "\n".join(recent_msgs)
            contents = f"Recent context:\n{context_block}\n\nRespond to the latest message by {message.author.display_name}."

        # Call Gemini in worker thread
        response = await asyncio.to_thread(
            gemini_client.models.generate_content,
            model=GEMINI_MODEL,
            contents=contents
        )

        reply_text = response.text or "[Empty Response]"

        # Remove checkmark and hourglass on completion
        await safe_remove_reaction(message, EMOJI_CHECK)
        await safe_remove_reaction(message, EMOJI_HOURGLASS)

        # Reply handling Discord's 2000-character limit
        if len(reply_text) <= 2000:
            await message.reply(reply_text, mention_author=False)
        else:
            # Chunk response into safe blocks of 1900 chars
            chunks = [reply_text[i:i+1900] for i in range(0, len(reply_text), 1900)]
            for idx, chunk in enumerate(chunks):
                if idx == 0:
                    await message.reply(chunk, mention_author=False)
                else:
                    await message.channel.send(chunk)

    except APIError as e:
        # Error handling: Remove working reactions, add cross, and report
        await safe_remove_reaction(message, EMOJI_CHECK)
        await safe_remove_reaction(message, EMOJI_HOURGLASS)
        await safe_add_reaction(message, EMOJI_ERROR)

        err_msg = f"API Error ({e.code}): {e.message}"
        await message.reply(f"⚠️ **Error:** {err_msg}", mention_author=False)
        await send_log(f"API Error on user {message.author}: {err_msg}", error=True)

    except Exception as e:
        await safe_remove_reaction(message, EMOJI_CHECK)
        await safe_remove_reaction(message, EMOJI_HOURGLASS)
        await safe_add_reaction(message, EMOJI_ERROR)

        err_msg = f"{type(e).__name__}: {str(e)}"
        await message.reply(f"⚠️ **Unexpected Error:** {err_msg}", mention_author=False)
        await send_log(f"Exception on message {message.id}: {err_msg}", error=True)

# ----------------- RENDER HEALTH CHECK SERVER -----------------

async def start_health_server():
    """Runs a minimal HTTP server so Render registers a healthy Web Service on $PORT."""
    async def ping_handler(request):
        return web.Response(text="Gemini Discord Bot is Running!")

    app = web.Application()
    app.router.add_get("/", ping_handler)
    app.router.add_get("/healthz", ping_handler)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    print(f"Render HTTP health check server running on port {PORT}")

# ----------------- BOT BOOTSTRAP -----------------

@bot.event
async def on_ready():
    print(f"Bot logged in as {bot.user} (ID: {bot.user.id})")
    try:
        synced = await bot.tree.sync()
        print(f"Successfully synced {len(synced)} application slash commands.")
    except Exception as e:
        print(f"Failed to sync slash commands: {e}")

    await send_log(f"Bot awakened and connected to Discord Gateway as `{bot.user}`.")

async def main():
    async with bot:
        # Start the background HTTP server for Render alongside Discord Gateway
        await start_health_server()
        await bot.start(DISCORD_TOKEN)

if __name__ == "__main__":
    if not DISCORD_TOKEN or not GEMINI_API_KEY:
        print("ERROR: Please set DISCORD_TOKEN and GEMINI_API_KEY in your environment.")
        exit(1)
    asyncio.run(main())