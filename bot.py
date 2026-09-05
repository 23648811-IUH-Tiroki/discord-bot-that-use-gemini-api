import os
import asyncio
from datetime import datetime, timezone
import discord
from discord import app_commands
from discord.ext import commands
from aiohttp import web
from dotenv import load_dotenv
from google.genai.errors import APIError

from config import config, save_config, ALL_MODELS, get_buffer_total_chars
import gemini_client
from views import create_status_embed, InteractiveStatusView, is_owner

load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
PORT = int(os.getenv("PORT", 8080))

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

dashboard_lock = asyncio.Lock()

# ----------------- LOGGING & DASHBOARD -----------------

async def update_persistent_dashboard():
    async with dashboard_lock:
        if not config["log_channel_id"]:
            return
        channel = bot.get_channel(config["log_channel_id"])
        if not channel:
            return

        embed = create_status_embed()
        view = InteractiveStatusView(bot, on_change_callback=send_log, refresh_callback=update_persistent_dashboard)

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
            print(f"Failed to post dashboard: {e}")

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

@bot.tree.command(name="set_checkpoint", description="Reset context memory and mark the next message as the checkpoint.")
async def set_checkpoint(interaction: discord.Interaction):
    if not await is_owner(bot, interaction):
        return await interaction.response.send_message("❌ Owner only.", ephemeral=True)
    config["summary_messages"] = []
    config["checkpoint"] = None
    save_config()
    await interaction.response.send_message("✅ Memory reset. The next incoming message establishes the new checkpoint.", ephemeral=True)
    await send_log(f"🔄 Checkpoint reset by {interaction.user.mention}.")

@bot.tree.command(name="summarize_now", description="Force summarize from the current checkpoint to the latest message.")
async def summarize_now(interaction: discord.Interaction):
    if not await is_owner(bot, interaction):
        return await interaction.response.send_message("❌ Owner only.", ephemeral=True)
    if not config["summary_messages"]:
        return await interaction.response.send_message("⚠️ Buffer is empty.", ephemeral=True)
    if not config["summary_channel_id"]:
        return await interaction.response.send_message("⚠️ Set a summary channel first.", ephemeral=True)

    await interaction.response.defer(ephemeral=True)
    asyncio.create_task(run_summarization())
    await interaction.followup.send("✅ Summary started.")

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

@bot.tree.command(name="bot_status", description="Check current status (Read-only view).")
async def bot_status(interaction: discord.Interaction):
    await interaction.response.send_message(embed=create_status_embed(), ephemeral=True)

# Admin command to force instant slash command sync
@bot.command(name="sync")
@commands.is_owner()
async def sync_cmd(ctx):
    synced = await bot.tree.sync(guild=ctx.guild)
    await ctx.send(f"✅ Synced {len(synced)} slash commands directly to this server!")

# ----------------- MESSAGE HANDLING -----------------

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    await bot.process_commands(message)

    # 1. STRICT: Bot only responds if directly @pinged
    if message.mention_everyone or bot.user not in message.mentions:
        return

    is_thread = isinstance(message.channel, discord.Thread)
    is_allowed = message.channel.id in config["chat_channels"]
    thread_allowed = is_thread and (message.channel.parent_id in config["chat_channels"])

    if config["chat_channels"] and not (is_allowed or thread_allowed):
        return

    # 2. Record checkpoint if none exists
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

    if get_buffer_total_chars() >= 9500:
        asyncio.create_task(run_summarization())
    asyncio.create_task(update_persistent_dashboard())

    # 3. Reactions
    await message.add_reaction("✅")
    await message.add_reaction("⏳")

    try:
        # CONTEXT COMPILATION:
        if is_thread:
            # Thread: Deep conversation context
            history = []
            async for h in message.channel.history(limit=60, oldest_first=True):
                role = "model" if h.author == bot.user else "user"
                clean = h.clean_content.replace(f"@{bot.user.name}", "").strip()
                if clean:
                    history.append(f"{h.author.display_name} ({role}): {clean}")
            context = "Thread history:\n" + "\n".join(history) + "\n\nRespond to the latest message."
        else:
            # Normal Channel: STRICTLY messages from Checkpoint ID forward
            cp_id = config["checkpoint"]["id"]
            history = []
            async for h in message.channel.history(limit=50, after=discord.Object(id=cp_id - 1), oldest_first=True):
                clean = h.clean_content.replace(f"@{bot.user.name}", "").strip()
                if clean:
                    role = "model" if h.author == bot.user else "user"
                    history.append(f"{h.author.display_name} ({role}): {clean}")

            # Inject recent past summaries so the bot remembers past days when asked
            past_notes = ""
            if config.get("past_summaries"):
                summaries_text = "\n---\n".join([s["text"] for s in config["past_summaries"][-3:]])
                past_notes = f"\n[Archived Past Summaries for reference]:\n{summaries_text}\n"

            context = (
                f"{past_notes}\n"
                f"Current conversation (since checkpoint):\n" + "\n".join(history) +
                f"\n\nRespond to {message.author.display_name}'s latest message."
            )

        reply = await gemini_client.call_gemini(context)

        await message.remove_reaction("✅", bot.user)
        await message.remove_reaction("⏳", bot.user)

        # Discord 2000-char splitting
        if len(reply) <= 2000:
            await message.reply(reply, mention_author=False)
        else:
            chunks = [reply[i:i+1900] for i in range(0, len(reply), 1900)]
            for idx, c in enumerate(chunks):
                if idx == 0:
                    await message.reply(c, mention_author=False)
                else:
                    await message.channel.send(c)

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
        await send_log(f"Error: {e}", error=True)

# ----------------- HEALTH SERVER & BOOTSTRAP -----------------

async def start_health_server():
    async def ping(request):
        return web.Response(text="Bot Operational")
    app = web.Application()
    app.router.add_get("/", ping)
    app.router.add_get("/healthz", ping)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")
    # Instant Guild Sync for all servers the bot is in
    for guild in bot.guilds:
        try:
            bot.tree.copy_global_to(guild=guild)
            await bot.tree.sync(guild=guild)
            print(f"Instant command sync completed for guild: {guild.name} ({guild.id})")
        except Exception as e:
            print(f"Sync failed for {guild.name}: {e}")

    await send_log(f"Bot awakened and online using model `{config['current_model']}`.")

async def main():
    async with bot:
        await start_health_server()
        await bot.start(DISCORD_TOKEN)

if __name__ == "__main__":
    asyncio.run(main())
