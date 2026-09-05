import os
import json
import asyncio
from datetime import datetime, timezone
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

ALL_MODELS = [
    "gemini-2.5-flash-preview-tts", "gemini-2.5-pro-preview-tts", "gemma-4-26b-a4b-it",
    "gemma-4-31b-it", "gemini-flash-latest", "gemini-flash-lite-latest",
    "gemini-pro-latest", "gemini-2.5-flash-image", "gemini-3-flash-preview",
    "gemini-3.1-pro-preview", "gemini-3.1-pro-preview-customtools",
    "gemini-3.1-flash-lite-preview", "gemini-3.1-flash-lite", "gemini-3-pro-image-preview",
    "gemini-3-pro-image", "nano-banana-pro-preview", "gemini-3.1-flash-image-preview",
    "gemini-3.1-flash-image", "gemini-3.1-flash-lite-image", "gemini-3.5-flash",
    "gemini-3.5-flash-lite", "gemini-omni-flash-preview", "gemini-omni-1.1-flash",
    "gemini-3.5-transcribe", "gemini-3.6-flash", "gemini-3.7-flash", "gemini-3.8-flash",
    "lyria-3-clip-preview", "lyria-3-pro-preview", "lyria-3.5",
    "gemini-3.1-flash-tts-preview", "gemini-robotics-er-2-preview",
    "gemini-2.5-computer-use-preview-10-2025", "antigravity-preview-05-2026",
    "deep-research-max-preview-04-2026", "deep-research-preview-04-2026",
    "deep-research-pro-preview-12-2025", "gemini-embedding-001", "gemini-embedding-2-preview",
    "gemini-embedding-2", "gemini-3.5-transcribe-live", "gemini-2.5-flash-native-audio-latest",
    "gemini-2.5-flash-native-audio-preview-09-2025", "gemini-2.5-flash-native-audio-preview-12-2025",
    "gemini-3.1-flash-live-preview", "gemini-robotics-er-2-streaming-preview",
    "gemini-3.5-live-translate-preview"
]

gemini_client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# ----------------- CONFIG & STORAGE -----------------

config = {
    "current_model": "gemini-3.6-flash",
    "chat_channels": [],
    "log_channel_id": None,
    "summary_channel_id": None,
    "status_message_id": None,
    # Checkpoint tracking
    "checkpoint": None, # {"id": int, "url": str, "timestamp": str}
    "summary_messages": [] # list of {"id": int, "url": str, "author": str, "content": str, "length": int}
}

dashboard_lock = asyncio.Lock()

def load_config():
    global config
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                config.update(json.load(f))
        except Exception as e:
            print(f"Error loading config: {e}")

def save_config():
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"Error saving config: {e}")

load_config()

def get_buffer_total_chars() -> int:
    return sum(m.get("length", len(m.get("content", ""))) for m in config["summary_messages"])

# ----------------- REACTION EMOJIS -----------------

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

async def is_authorized_owner(interaction: discord.Interaction) -> bool:
    app_info = await bot.application_info()
    if interaction.user.id == app_info.owner.id:
        return True
    if interaction.guild and interaction.user.id == interaction.guild.owner_id:
        return True
    return False

# ----------------- STATUS EMBED BUILDER -----------------

def create_status_embed() -> discord.Embed:
    chat_ch = ", ".join([f"<#{cid}>" for cid in config["chat_channels"]]) or "None"
    log_ch = f"<#{config['log_channel_id']}>" if config["log_channel_id"] else "None"
    sum_ch = f"<#{config['summary_channel_id']}>" if config["summary_channel_id"] else "None"
    total_chars = get_buffer_total_chars()
    msg_count = len(config["summary_messages"])

    # Checkpoint representation
    if config.get("checkpoint"):
        cp = config["checkpoint"]
        cp_val = f"📌 [Jump to Message]({cp['url']}) `(ID: {cp['id']})`"
    else:
        cp_val = "None (Waiting for next message)"

    embed = discord.Embed(title="⚙️ Gemini Bot Status Board", color=discord.Color.blue())
    embed.add_field(name="Current Model", value=f"`{config['current_model']}`", inline=False)
    embed.add_field(name="Current Checkpoint", value=cp_val, inline=False)
    embed.add_field(name="Chat Channels", value=chat_ch, inline=False)
    embed.add_field(name="Log Channel", value=log_ch, inline=True)
    embed.add_field(name="Summary Channel", value=sum_ch, inline=True)
    embed.add_field(
        name="Summary Buffer",
        value=f"**{total_chars:,} / 10,000 chars** ({msg_count} messages recorded)",
        inline=False
    )
    # Using timezone-aware UTC datetime
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    embed.set_footer(text=f"Last updated: {now_utc}")
    return embed

async def update_persistent_dashboard():
    """Deletes previous status message and sends a new one at the very bottom of the log channel."""
    async with dashboard_lock:
        if not config["log_channel_id"]:
            return

        channel = bot.get_channel(config["log_channel_id"])
        if not channel:
            return

        embed = create_status_embed()
        view = InteractiveStatusView()

        # Delete previous status message so the dashboard is always at the bottom
        if config.get("status_message_id"):
            try:
                old_msg = await channel.fetch_message(config["status_message_id"])
                await old_msg.delete()
            except Exception:
                pass

        try:
            new_msg = await channel.send(embed=embed, view=view)
            config["status_message_id"] = new_msg.id
            save_config()
        except Exception as e:
            print(f"Failed to post bottom dashboard: {e}")

async def send_log(message: str, error: bool = False):
    """Sends log text and ensures the status board stays at the bottom."""
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    prefix = "[ERROR]" if error else "[INFO]"
    formatted = f"`{timestamp}` **{prefix}** {message}"
    print(f"[{timestamp}] {prefix} {message}")

    if config["log_channel_id"]:
        channel = bot.get_channel(config["log_channel_id"])
        if channel:
            try:
                await channel.send(formatted[:1950])
            except Exception as e:
                print(f"Failed to post to log channel: {e}")

    # Keep dashboard below the newly posted log message
    await update_persistent_dashboard()

# ----------------- INTERACTIVE DASHBOARD VIEW -----------------

class QuickModelSelect(discord.ui.Select):
    def __init__(self):
        quick_options = [
            "gemini-3.6-flash", "gemini-3.7-flash", "gemini-3.8-flash", "gemini-3.5-flash",
            "gemini-3.5-flash-lite", "gemini-3.1-pro-preview", "gemini-3.1-flash-lite",
            "gemini-flash-latest", "gemini-pro-latest", "gemini-omni-1.1-flash",
            "gemma-4-31b-it", "deep-research-preview-04-2026"
        ]
        options = [
            discord.SelectOption(
                label=m,
                value=m,
                default=(m == config["current_model"]),
                description="Click to switch model"
            )
            for m in quick_options if m in ALL_MODELS
        ]
        super().__init__(placeholder="Switch Model (Quick Dropdown)...", min_values=1, max_values=1, options=options, row=0)

    async def callback(self, interaction: discord.Interaction):
        if not await is_authorized_owner(interaction):
            return await interaction.response.send_message("❌ Only the bot owner can change the model.", ephemeral=True)

        selected = self.values[0]
        config["current_model"] = selected
        save_config()

        await interaction.response.defer()
        await send_log(f"🔔 **Model Changed:** `{selected}` by {interaction.user.mention}")

class ChannelPickerSelect(discord.ui.ChannelSelect):
    def __init__(self, target_type: str, placeholder: str, row: int):
        self.target_type = target_type
        super().__init__(
            placeholder=placeholder,
            channel_types=[discord.ChannelType.text],
            min_values=1,
            max_values=1,
            row=row
        )

    async def callback(self, interaction: discord.Interaction):
        if not await is_authorized_owner(interaction):
            return await interaction.response.send_message("❌ Only the bot owner can modify channel settings.", ephemeral=True)

        selected_channel = self.values[0]
        cid = selected_channel.id

        if self.target_type == "chat":
            if cid in config["chat_channels"]:
                config["chat_channels"].remove(cid)
                action = "removed from"
            else:
                config["chat_channels"].append(cid)
                action = "added to"
            msg = f"Chat channel {selected_channel.mention} {action} allowed list."
        elif self.target_type == "log":
            config["log_channel_id"] = cid
            config["status_message_id"] = None
            msg = f"System log channel set to {selected_channel.mention}."
        elif self.target_type == "summary":
            config["summary_channel_id"] = cid
            msg = f"Summary channel set to {selected_channel.mention}."

        save_config()
        await interaction.response.defer()
        await send_log(f"⚙️ **Channel Setting Changed:** {msg} (by {interaction.user.mention})")

class InteractiveStatusView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(QuickModelSelect())
        self.add_item(ChannelPickerSelect("chat", "Toggle Chat Channel...", row=1))
        self.add_item(ChannelPickerSelect("log", "Set Log Channel...", row=2))
        self.add_item(ChannelPickerSelect("summary", "Set Summary Channel...", row=3))

    @discord.ui.button(label="🔄 Refresh Dashboard", style=discord.ButtonStyle.secondary, row=4)
    async def refresh_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        await update_persistent_dashboard()

# ----------------- SLASH COMMANDS -----------------

async def model_autocomplete(interaction: discord.Interaction, current: str):
    return [
        app_commands.Choice(name=m, value=m)
        for m in ALL_MODELS if current.lower() in m.lower()
    ][:25]

@bot.tree.command(name="set_model", description="Switch the active Gemini model.")
@app_commands.autocomplete(model=model_autocomplete)
@app_commands.describe(model="Search and select a Gemini model")
async def set_model(interaction: discord.Interaction, model: str):
    if not await is_authorized_owner(interaction):
        return await interaction.response.send_message("❌ Only the bot owner can change the model.", ephemeral=True)

    if model not in ALL_MODELS:
        return await interaction.response.send_message("❌ Invalid model choice.", ephemeral=True)

    config["current_model"] = model
    save_config()

    await interaction.response.send_message(f"✅ Active model switched to `{model}`.", ephemeral=True)
    await send_log(f"🔔 **Model Changed:** `{model}` by {interaction.user.mention}")

@bot.tree.command(name="set_chat_channel", description="Toggle or set an allowed channel for Gemini chat.")
@app_commands.describe(channel="Select the text channel")
async def set_chat_channel(interaction: discord.Interaction, channel: discord.TextChannel):
    if not await is_authorized_owner(interaction):
        return await interaction.response.send_message("❌ Only the bot owner can change channels.", ephemeral=True)

    if channel.id in config["chat_channels"]:
        config["chat_channels"].remove(channel.id)
        action = "removed from"
    else:
        config["chat_channels"].append(channel.id)
        action = "added to"
    save_config()

    await interaction.response.send_message(f"Channel {channel.mention} {action} allowed list.", ephemeral=True)
    await send_log(f"Chat channel updated: {channel.name} ({channel.id}) {action} list by {interaction.user.mention}.")

@bot.tree.command(name="set_log_channel", description="Set the channel where system and error logs are sent.")
@app_commands.describe(channel="Select the log channel")
async def set_log_channel(interaction: discord.Interaction, channel: discord.TextChannel):
    if not await is_authorized_owner(interaction):
        return await interaction.response.send_message("❌ Only the bot owner can change channels.", ephemeral=True)

    config["log_channel_id"] = channel.id
    config["status_message_id"] = None
    save_config()

    await interaction.response.send_message(f"Log channel set to {channel.mention}.", ephemeral=True)
    await send_log(f"System log channel changed to {channel.name} ({channel.id}) by {interaction.user.mention}.")

@bot.tree.command(name="set_summary_channel", description="Set the channel where automated summaries are posted.")
@app_commands.describe(channel="Select the summary channel")
async def set_summary_channel(interaction: discord.Interaction, channel: discord.TextChannel):
    if not await is_authorized_owner(interaction):
        return await interaction.response.send_message("❌ Only the bot owner can change channels.", ephemeral=True)

    config["summary_channel_id"] = channel.id
    save_config()

    await interaction.response.send_message(f"Summary channel set to {channel.mention}.", ephemeral=True)
    await send_log(f"Summary channel changed to {channel.name} ({channel.id}) by {interaction.user.mention}.")

@bot.tree.command(name="bot_status", description="Check current status (Read-only view).")
async def bot_status(interaction: discord.Interaction):
    embed = create_status_embed()
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="set_checkpoint", description="Reset the character counter and start a new checkpoint.")
async def set_checkpoint(interaction: discord.Interaction):
    """Resets counter; the next message received becomes the starting checkpoint."""
    if not await is_authorized_owner(interaction):
        return await interaction.response.send_message("❌ Only the bot owner can reset the checkpoint.", ephemeral=True)

    config["summary_messages"] = []
    config["checkpoint"] = None
    save_config()

    await interaction.response.send_message("✅ Checkpoint reset. The next incoming message will establish the new checkpoint.", ephemeral=True)
    await send_log(f"🔄 Checkpoint reset manually by {interaction.user.mention}.")

@bot.tree.command(name="summarize_now", description="Force summarize from the current checkpoint to the latest message.")
async def summarize_now(interaction: discord.Interaction):
    """Summarizes immediately from checkpoint to latest message."""
    if not await is_authorized_owner(interaction):
        return await interaction.response.send_message("❌ Only the bot owner can run this command.", ephemeral=True)

    if not config["summary_messages"]:
        return await interaction.response.send_message("⚠️ The summary buffer is currently empty. Nothing to summarize.", ephemeral=True)

    if not config["summary_channel_id"]:
        return await interaction.response.send_message("⚠️ Please set a summary channel first using `/set_summary_channel`.", ephemeral=True)

    await interaction.response.defer(ephemeral=True)
    asyncio.create_task(process_summary_buffer())
    await interaction.followup.send("✅ Summary generation triggered.")

# ----------------- SUMMARIZATION PROCESSING -----------------

async def process_summary_buffer():
    if not config["summary_channel_id"] or not config["summary_messages"]:
        return

    summary_channel = bot.get_channel(config["summary_channel_id"])
    if not summary_channel:
        return

    messages_to_summarize = list(config["summary_messages"])
    checkpoint_data = config.get("checkpoint")

    # Reset buffer and checkpoint
    config["summary_messages"] = []
    config["checkpoint"] = None
    save_config()

    first_msg = checkpoint_data if checkpoint_data else messages_to_summarize[0]
    last_msg = messages_to_summarize[-1]

    start_time = first_msg.get("timestamp", "")[:16].replace("T", " ")
    end_time = last_msg.get("timestamp", "")[:16].replace("T", " ")

    header = f"🔗 [Start]({first_msg['url']}) ({start_time}) ➔ [End]({last_msg['url']}) ({end_time})\n"
    max_summary_length = 500 - len(header) - 10
    if max_summary_length < 50:
        max_summary_length = 50

    transcript = "\n".join([f"{m['author']}: {m['content']}" for m in messages_to_summarize])

    prompt = (
        f"Provide a concise summary of this conversation. "
        f"STRICT LIMIT: Must be under {max_summary_length} characters total.\n\n"
        f"Conversation:\n{transcript}"
    )

    try:
        response = await asyncio.to_thread(
            gemini_client.models.generate_content,
            model=config["current_model"],
            contents=prompt
        )
        summary_text = (response.text or "No summary generated.").strip()

        total_post = header + summary_text
        if len(total_post) > 500:
            total_post = total_post[:497] + "..."

        await summary_channel.send(total_post)
        await send_log(f"Auto-summary posted ({len(total_post)} chars).")

    except Exception as e:
        await send_log(f"Summarization error: {e}", error=True)

# ----------------- MESSAGE HANDLING -----------------

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    await bot.process_commands(message)

    # STRICT: Bot only responds if directly @pinged
    if message.mention_everyone or bot.user not in message.mentions:
        return

    is_thread = isinstance(message.channel, discord.Thread)
    is_allowed_chat = message.channel.id in config["chat_channels"]
    thread_parent_allowed = is_thread and (message.channel.parent_id in config["chat_channels"])

    if config["chat_channels"] and not (is_allowed_chat or thread_parent_allowed):
        return

    # Checkpoint recording: set checkpoint if buffer was empty
    now_iso = datetime.now(timezone.utc).isoformat()
    if not config["summary_messages"]:
        config["checkpoint"] = {
            "id": message.id,
            "url": message.jump_url,
            "timestamp": now_iso
        }

    config["summary_messages"].append({
        "id": message.id,
        "url": message.jump_url,
        "timestamp": now_iso,
        "author": message.author.display_name,
        "content": message.clean_content,
        "length": len(message.clean_content)
    })
    save_config()

    # Trigger automatic summary if 10,000 +- 500 chars (>= 9,500 threshold)
    if get_buffer_total_chars() >= 9500:
        asyncio.create_task(process_summary_buffer())

    # Update dashboard in log channel
    asyncio.create_task(update_persistent_dashboard())

    # --- REACTION LIFECYCLE (Yawning face removed) ---
    await safe_add_reaction(message, EMOJI_CHECK)
    await safe_add_reaction(message, EMOJI_HOURGLASS)

    try:
        if is_thread:
            thread_history = []
            async for h_msg in message.channel.history(limit=60, oldest_first=True):
                role = "model" if h_msg.author == bot.user else "user"
                clean_text = h_msg.clean_content.replace(f"@{bot.user.name}", "").strip()
                if clean_text:
                    thread_history.append(f"{h_msg.author.display_name} ({role}): {clean_text}")

            contents = f"Thread conversation history:\n" + "\n".join(thread_history) + "\n\nPlease respond to the latest query."
        else:
            recent_msgs = []
            async for h_msg in message.channel.history(limit=10, oldest_first=True):
                role = "model" if h_msg.author == bot.user else "user"
                clean_text = h_msg.clean_content.replace(f"@{bot.user.name}", "").strip()
                if clean_text:
                    recent_msgs.append(f"{h_msg.author.display_name}: {clean_text}")

            contents = f"Recent context:\n" + "\n".join(recent_msgs) + f"\n\nRespond to {message.author.display_name}."

        response = await asyncio.to_thread(
            gemini_client.models.generate_content,
            model=config["current_model"],
            contents=contents
        )

        reply_text = response.text or "[Empty Response]"

        await safe_remove_reaction(message, EMOJI_CHECK)
        await safe_remove_reaction(message, EMOJI_HOURGLASS)

        # Discord 2,000-char message chunking
        if len(reply_text) <= 2000:
            await message.reply(reply_text, mention_author=False)
        else:
            chunks = [reply_text[i:i+1900] for i in range(0, len(reply_text), 1900)]
            for idx, chunk in enumerate(chunks):
                if idx == 0:
                    await message.reply(chunk, mention_author=False)
                else:
                    await message.channel.send(chunk)

    except APIError as e:
        await safe_remove_reaction(message, EMOJI_CHECK)
        await safe_remove_reaction(message, EMOJI_HOURGLASS)
        await safe_add_reaction(message, EMOJI_ERROR)

        err_msg = f"API Error ({e.code}): {e.message}"
        await message.reply(f"⚠️ **Error:** {err_msg}", mention_author=False)
        await send_log(f"API Error on prompt by {message.author}: {err_msg}", error=True)

    except Exception as e:
        await safe_remove_reaction(message, EMOJI_CHECK)
        await safe_remove_reaction(message, EMOJI_HOURGLASS)
        await safe_add_reaction(message, EMOJI_ERROR)

        err_msg = f"{type(e).__name__}: {str(e)}"
        await message.reply(f"⚠️ **Unexpected Error:** {err_msg}", mention_author=False)
        await send_log(f"Exception: {err_msg}", error=True)

# ----------------- HEALTH SERVER -----------------

async def start_health_server():
    async def ping(request):
        return web.Response(text="Gemini Bot Operational")

    app = web.Application()
    app.router.add_get("/", ping)
    app.router.add_get("/healthz", ping)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    print(f"Health check server active on port {PORT}")

# ----------------- BOOTSTRAP -----------------

@bot.event
async def on_ready():
    print(f"Bot authenticated as {bot.user} (ID: {bot.user.id})")
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} slash commands.")
    except Exception as e:
        print(f"Command sync error: {e}")

    await send_log(f"Bot awakened and online using model `{config['current_model']}`.")

async def main():
    async with bot:
        await start_health_server()
        await bot.start(DISCORD_TOKEN)

if __name__ == "__main__":
    if not DISCORD_TOKEN or not GEMINI_API_KEY:
        print("ERROR: DISCORD_TOKEN and GEMINI_API_KEY must be set in environment.")
        exit(1)
    asyncio.run(main())
