import os
import asyncio
from datetime import datetime, timezone
import discord
from discord import app_commands
from discord.ext import commands
from aiohttp import web
from dotenv import load_dotenv
from google.genai.errors import APIError

from config import (
    config, save_config, ALL_MODELS, get_buffer_total_chars,
    generate_config_text, parse_config_text
)
import gemini_client
from views import create_status_embed, InteractiveStatusView, is_owner

load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
PORT = int(os.getenv("PORT", 8080))

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

dashboard_lock = asyncio.Lock()

# ----------------- AUTO-BIND CHAT CHANNEL -----------------

def auto_bind_chat_channel(channel_id: int):
    """If no chat channel has been configured, bind exclusively to the first channel used."""
    if not config["chat_channel_id"]:
        config["chat_channel_id"] = channel_id
        save_config()
        print(f"Auto-bound exclusive chat channel to ID: {channel_id}")

# ----------------- LOGGING & DASHBOARD -----------------

async def update_persistent_dashboard():
    async with dashboard_lock:
        if not config["log_channel_id"]:
            return
        channel = bot.get_channel(config["log_channel_id"])
        if not channel:
            return

        content_text = generate_config_text()
        embed = create_status_embed()
        view = InteractiveStatusView(bot, on_change_callback=send_log, refresh_callback=update_persistent_dashboard)

        if config.get("status_message_id"):
            try:
                old_msg = await channel.fetch_message(config["status_message_id"])
                await old_msg.delete()
            except Exception:
                pass

        try:
            new_msg = await channel.send(content=content_text, embed=embed, view=view)
            config["status_message_id"] = new_msg.id
            save_config()
        except Exception as e:
            print(f"Failed to post bottom dashboard: {e}")

async def send_log(message: str, error: bool = False):
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
                print(f"Log dispatch error: {e}")
    await update_persistent_dashboard()

# ----------------- SLASH COMMANDS -----------------

@bot.tree.command(name="set_chat_channel", description="Set the exclusive channel for Gemini chat.")
@app_commands.describe(channel="Select the text channel")
async def set_chat_channel(interaction: discord.Interaction, channel: discord.TextChannel):
    if not await is_owner(bot, interaction):
        return await interaction.response.send_message("❌ Administrator permissions required.", ephemeral=True)

    config["chat_channel_id"] = channel.id
    save_config()
    await interaction.response.send_message(f"✅ Chat channel set exclusively to {channel.mention}.", ephemeral=True)
    await send_log(f"Chat channel changed to {channel.mention} by {interaction.user.mention}.")

@bot.tree.command(name="set_log_channel", description="Set the channel where system logs and status board live.")
@app_commands.describe(channel="Select the log channel")
async def set_log_channel(interaction: discord.Interaction, channel: discord.TextChannel):
    if not await is_owner(bot, interaction):
        return await interaction.response.send_message("❌ Administrator permissions required.", ephemeral=True)

    config["log_channel_id"] = channel.id
    config["status_message_id"] = None
    save_config()
    await interaction.response.send_message(f"✅ Log channel set to {channel.mention}.", ephemeral=True)
    await send_log(f"Log channel changed to {channel.mention} by {interaction.user.mention}.")

@bot.tree.command(name="set_summary_channel", description="Set the channel where automated summaries are posted.")
@app_commands.describe(channel="Select the summary channel")
async def set_summary_channel(interaction: discord.Interaction, channel: discord.TextChannel):
    if not await is_owner(bot, interaction):
        return await interaction.response.send_message("❌ Administrator permissions required.", ephemeral=True)

    config["summary_channel_id"] = channel.id
    save_config()
    await interaction.response.send_message(f"✅ Summary channel set to {channel.mention}.", ephemeral=True)
    await send_log(f"Summary channel changed to {channel.mention} by {interaction.user.mention}.")

@bot.tree.command(name="set_checkpoint", description="Reset context memory; the next message establishes the checkpoint.")
async def set_checkpoint(interaction: discord.Interaction):
    auto_bind_chat_channel(interaction.channel_id)
    if not await is_owner(bot, interaction):
        return await interaction.response.send_message("❌ Administrator permissions required.", ephemeral=True)

    config["summary_messages"] = []
    config["checkpoint"] = None
    save_config()
    await interaction.response.send_message("✅ Memory checkpoint reset. The next incoming message begins the new context.", ephemeral=True)
    await send_log(f"🔄 Checkpoint reset by {interaction.user.mention}.")

@bot.tree.command(name="summarize_now", description="Force summarize from current checkpoint to latest message.")
async def summarize_now(interaction: discord.Interaction):
    auto_bind_chat_channel(interaction.channel_id)
    if not await is_owner(bot, interaction):
        return await interaction.response.send_message("❌ Administrator permissions required.", ephemeral=True)
    if not config["summary_messages"]:
        return await interaction.response.send_message("⚠️ Summary buffer is currently empty.", ephemeral=True)
    if not config["summary_channel_id"]:
        return await interaction.response.send_message("⚠️ Please set a summary channel first via `/set_summary_channel`.", ephemeral=True)

    await interaction.response.defer(ephemeral=True)
    asyncio.create_task(run_summarization())
    await interaction.followup.send("✅ Summary process triggered.")

async def run_summarization():
    if not config["summary_channel_id"] or not config["summary_messages"]:
        return
    channel = bot.get_channel(config["summary_channel_id"])
    if not channel:
        return

    msgs = list(config["summary_messages"])
    cp = config.get("checkpoint")
    config["summary_messages"] = []
    config["checkpoint"] = None
    save_config()

    try:
        summary_post = await gemini_client.generate_summary(msgs, cp)
        await channel.send(summary_post)
        await send_log(f"Auto-summary posted ({len(summary_post)} chars).")
    except Exception as e:
        await send_log(f"Summarization error: {e}", error=True)

@bot.tree.command(name="bot_status", description="Display status board (Read-only view).")
async def bot_status(interaction: discord.Interaction):
    auto_bind_chat_channel(interaction.channel_id)
    await interaction.response.send_message(
        content=generate_config_text(),
        embed=create_status_embed(),
        ephemeral=True
    )

# ----------------- MESSAGE HANDLING -----------------

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    await bot.process_commands(message)

    # STRICT: Bot only responds if directly @mentioned
    if message.mention_everyone or bot.user not in message.mentions:
        return

    is_thread = isinstance(message.channel, discord.Thread)

    # AUTO-BIND: First time interacting establishes the exclusive chat channel
    if not config["chat_channel_id"]:
        target_bind = message.channel.parent_id if is_thread else message.channel.id
        auto_bind_chat_channel(target_bind)

    # EXCLUSIVE CHECK: Only the designated channel or its threads can talk to the bot
    valid_channel = message.channel.id == config["chat_channel_id"]
    valid_thread = is_thread and (message.channel.parent_id == config["chat_channel_id"])

    if not (valid_channel or valid_thread):
        return

    # Checkpoint recording for USER message
    now_iso = datetime.now(timezone.utc).isoformat()
    if not config["checkpoint"]:
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

    # Reactions
    await message.add_reaction("✅")
    await message.add_reaction("⏳")

    try:
        user_clean = message.clean_content.replace(f"@{bot.user.name}", "").strip()

        if is_thread:
            # Full thread context
            history = []
            async for h in message.channel.history(limit=60, oldest_first=True):
                role = "model" if h.author == bot.user else "user"
                clean = h.clean_content.replace(f"@{bot.user.name}", "").strip()
                if clean:
                    history.append(f"{h.author.display_name} ({role}): {clean}")
            context = "Thread Conversation History:\n" + "\n".join(history)
        else:
            # Normal Channel: STRICTLY from Checkpoint forward
            cp_id = config["checkpoint"]["id"]
            history = []
            async for h in message.channel.history(limit=50, after=discord.Object(id=cp_id - 1), oldest_first=True):
                clean = h.clean_content.replace(f"@{bot.user.name}", "").strip()
                if clean:
                    role = "model" if h.author == bot.user else "user"
                    history.append(f"{h.author.display_name} ({role}): {clean}")

            past_notes = ""
            if config.get("past_summaries"):
                summaries_text = "\n---\n".join([s["text"] for s in config["past_summaries"][-3:]])
                past_notes = f"[Past Summaries Context for Recall]:\n{summaries_text}\n\n"

            context = f"{past_notes}Recent chat since checkpoint:\n" + "\n".join(history)

        # Call Gemini via Chat session
        reply = await gemini_client.call_gemini_chat(context, user_clean)

        await message.remove_reaction("✅", bot.user)
        await message.remove_reaction("⏳", bot.user)

        # Send response
        sent_msg = None
        if len(reply) <= 2000:
            sent_msg = await message.reply(reply, mention_author=False)
        else:
            chunks = [reply[i:i+1900] for i in range(0, len(reply), 1900)]
            for idx, c in enumerate(chunks):
                if idx == 0:
                    sent_msg = await message.reply(c, mention_author=False)
                else:
                    sent_msg = await message.channel.send(c)

        # Record BOT message into character buffer & checkpoint counter
        if sent_msg:
            config["summary_messages"].append({
                "id": sent_msg.id,
                "url": sent_msg.jump_url,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "author": f"{bot.user.name} (Gemini)",
                "content": reply,
                "length": len(reply)
            })
            save_config()

        # Trigger auto-summary if 10,000 +- 500 chars (>= 9,500 threshold)
        if get_buffer_total_chars() >= 9500:
            asyncio.create_task(run_summarization())

        asyncio.create_task(update_persistent_dashboard())

    except APIError as e:
        await message.remove_reaction("✅", bot.user)
        await message.remove_reaction("⏳", bot.user)
        await message.add_reaction("❌")
        await message.reply(f"⚠️ **API Error ({e.code}):** {e.message}", mention_author=False)
        await send_log(f"API Error: {e.message}", error=True)
    except Exception as e:
        await message.remove_reaction("✅", bot.user)
        await message.remove_reaction("⏳", bot.user)
        await message.add_reaction("❌")
        await message.reply(f"⚠️ **Unexpected Error:** {e}", mention_author=False)
        await send_log(f"Exception: {e}", error=True)

# ----------------- RECOVERY SCAN ON STARTUP -----------------

async def scan_and_recover_config():
    """Scans text channels on boot for an existing [GEMINI-CONFIG] message to recover state."""
    print("Initiating Discord state recovery scan...")
    for guild in bot.guilds:
        for channel in guild.text_channels:
            try:
                # Check recent messages in each channel
                async for msg in channel.history(limit=10):
                    if msg.author == bot.user and "[GEMINI-CONFIG]" in msg.content:
                        success = parse_config_text(msg.content)
                        if success:
                            config["log_channel_id"] = channel.id
                            config["status_message_id"] = msg.id
                            save_config()
                            print(f"Recovered configuration from #{channel.name} ({channel.id})!")
                            return
            except Exception:
                continue

# ----------------- HEALTH SERVER & BOOTSTRAP -----------------

async def start_health_server():
    async def ping(request):
        return web.Response(text="Gemini Bot Operational")
    app = web.Application()
    app.router.add_get("/", ping)
    app.router.add_get("/healthz", ping)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()

@bot.event
async def on_ready():
    print(f"Bot connected as {bot.user} (ID: {bot.user.id})")

    # FIX DUPLICATE COMMANDS:
    # Clear local guild commands to avoid duplicates against global registry
    for guild in bot.guilds:
        try:
            bot.tree.clear_commands(guild=guild)
            await bot.tree.sync(guild=guild)
        except Exception as e:
            print(f"Guild clear error: {e}")

    # Register only once globally
    try:
        synced = await bot.tree.sync()
        print(f"Cleanly synced {len(synced)} global slash commands.")
    except Exception as e:
        print(f"Global sync error: {e}")

    # Attempt state recovery from Discord history
    await scan_and_recover_config()
    await update_persistent_dashboard()
    await send_log(f"Bot awakened and online using model `{config['current_model']}`.")

async def main():
    async with bot:
        await start_health_server()
        await bot.start(DISCORD_TOKEN)

if __name__ == "__main__":
    asyncio.run(main())
