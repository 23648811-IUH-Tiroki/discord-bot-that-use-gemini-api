import discord
from datetime import datetime, timezone
from config import config, save_config, ALL_MODELS, get_buffer_total_chars, generate_config_text

def create_status_embed() -> discord.Embed:
    chat_ch = f"<#{config['chat_channel_id']}>" if config["chat_channel_id"] else "None (Auto-binds on first use)"
    log_ch = f"<#{config['log_channel_id']}>" if config["log_channel_id"] else "None"
    sum_ch = f"<#{config['summary_channel_id']}>" if config["summary_channel_id"] else "None"
    total_chars = get_buffer_total_chars()
    msg_count = len(config["summary_messages"])

    if config.get("checkpoint"):
        cp = config["checkpoint"]
        cp_val = f"📌 [Jump to Checkpoint]({cp['url']}) `(ID: {cp['id']})`"
    else:
        cp_val = "None (Awaiting message to establish checkpoint)"

    embed = discord.Embed(title="⚙️ Gemini Bot Status Board", color=discord.Color.blue())
    embed.add_field(name="Current Model", value=f"`{config['current_model']}`", inline=False)
    embed.add_field(name="Current Checkpoint", value=cp_val, inline=False)
    embed.add_field(name="Chat Channel (Exclusive)", value=chat_ch, inline=False)
    embed.add_field(name="Log Channel", value=log_ch, inline=True)
    embed.add_field(name="Summary Channel", value=sum_ch, inline=True)
    embed.add_field(
        name="Summary Buffer",
        value=f"**{total_chars:,} / 10,000 chars** ({msg_count} messages recorded)",
        inline=False
    )
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    embed.set_footer(text=f"Last updated: {now_utc}")
    return embed

async def is_owner(bot, interaction: discord.Interaction) -> bool:
    """Robust owner check: Administrator permissions, guild owner, or bot application owner."""
    if interaction.user.guild_permissions.administrator:
        return True
    if interaction.guild and interaction.user.id == interaction.guild.owner_id:
        return True
    try:
        return await bot.is_owner(interaction.user)
    except Exception:
        return False

class QuickModelSelect(discord.ui.Select):
    def __init__(self, bot, on_change_callback):
        self.bot = bot
        self.on_change_callback = on_change_callback
        quick_options = [
            "gemini-3.6-flash", "gemini-3.7-flash", "gemini-3.8-flash", "gemini-3.5-flash",
            "gemini-3.5-flash-lite", "gemini-3.1-pro-preview", "gemini-3.1-flash-lite",
            "gemini-flash-latest", "gemini-pro-latest", "gemini-omni-1.1-flash"
        ]
        options = [
            discord.SelectOption(label=m, value=m, default=(m == config["current_model"]))
            for m in quick_options if m in ALL_MODELS
        ]
        super().__init__(placeholder="Switch Model (Quick Dropdown)...", min_values=1, max_values=1, options=options, row=0)

    async def callback(self, interaction: discord.Interaction):
        if not await is_owner(self.bot, interaction):
            return await interaction.response.send_message("❌ Only administrators or bot owners can change this.", ephemeral=True)
        config["current_model"] = self.values[0]
        save_config()
        await interaction.response.defer()
        await self.on_change_callback(f"🔔 **Model Changed:** `{self.values[0]}` by {interaction.user.mention}")

class ChannelPickerSelect(discord.ui.ChannelSelect):
    def __init__(self, bot, target_type: str, placeholder: str, row: int, on_change_callback):
        self.bot = bot
        self.target_type = target_type
        self.on_change_callback = on_change_callback
        super().__init__(placeholder=placeholder, channel_types=[discord.ChannelType.text], min_values=1, max_values=1, row=row)

    async def callback(self, interaction: discord.Interaction):
        if not await is_owner(self.bot, interaction):
            return await interaction.response.send_message("❌ Only administrators or bot owners can change channels.", ephemeral=True)

        channel = self.values[0]
        cid = channel.id

        if self.target_type == "chat":
            config["chat_channel_id"] = cid
            msg = f"Chat channel set exclusively to {channel.mention}."
        elif self.target_type == "log":
            config["log_channel_id"] = cid
            config["status_message_id"] = None
            msg = f"System log channel set to {channel.mention}."
        elif self.target_type == "summary":
            config["summary_channel_id"] = cid
            msg = f"Summary channel set to {channel.mention}."

        save_config()
        await interaction.response.defer()
        await self.on_change_callback(f"⚙️ {msg} (by {interaction.user.mention})")

class InteractiveStatusView(discord.ui.View):
    def __init__(self, bot, on_change_callback, refresh_callback):
        super().__init__(timeout=None)
        self.refresh_callback = refresh_callback
        self.add_item(QuickModelSelect(bot, on_change_callback))
        self.add_item(ChannelPickerSelect(bot, "chat", "Set Exclusive Chat Channel...", 1, on_change_callback))
        self.add_item(ChannelPickerSelect(bot, "log", "Set Log Channel...", 2, on_change_callback))
        self.add_item(ChannelPickerSelect(bot, "summary", "Set Summary Channel...", 3, on_change_callback))

    @discord.ui.button(label="🔄 Refresh Dashboard", style=discord.ButtonStyle.secondary, row=4)
    async def refresh_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        await self.refresh_callback()
