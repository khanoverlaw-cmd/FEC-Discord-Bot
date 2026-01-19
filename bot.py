import discord
from discord.ext import commands
from datetime import datetime
import random

# =========================
# CONFIG (edit these)
# =========================
FEC_ROLE_NAME = "The Federal Election Commission"  # EXACT role name (case-sensitive)
LOG_CHANNEL_NAME = "interaction-log"               # EXACT channel name (case-sensitive)

# Only allow posting into these channels (by channel NAME, not #mention)
ALLOWED_ANNOUNCE_CHANNELS = [
    "fec-announcements",
    "fec-public-records"
]

# =========================
# BOT SETUP
# =========================
intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

def has_fec_role(member: discord.Member) -> bool:
    return any(role.name == FEC_ROLE_NAME for role in member.roles)

def generate_case_number() -> str:
    # Example: FEC-26-0119-4821 (YY-MMDD-RAND)
    year_part = datetime.utcnow().strftime("%y")
    date_part = datetime.utcnow().strftime("%m%d")
    rand_part = random.randint(1000, 9999)
    return f"FEC-{year_part}-{date_part}-{rand_part}"

def house_style_announcement(title: str, message: str) -> str:
    return f"# :FEC: | {title}\n\n{message}"

async def log_unauthorized_attempt(
    guild: discord.Guild,
    channel: discord.abc.GuildChannel,
    user: discord.abc.User,
    case_id: str,
    command_name: str
) -> None:
    log_channel = discord.utils.get(guild.text_channels, name=LOG_CHANNEL_NAME)
    if not log_channel:
        return

    embed = discord.Embed(
        title="🚨 Unauthorized Command Attempt Logged",
        color=discord.Color.orange(),
        timestamp=datetime.utcnow()
    )
    embed.add_field(name="Case ID", value=f"`{case_id}`", inline=False)
    embed.add_field(name="User", value=f"{user} ({user.id})", inline=False)
    embed.add_field(name="Command", value=command_name, inline=True)

    # channel might not be a TextChannel object, so guard mention
    try:
        channel_display = channel.mention
    except Exception:
        channel_display = str(channel)

    embed.add_field(name="Issued In", value=channel_display, inline=True)
    embed.set_footer(text="Federal Election Commission • Interaction Log")

    await log_channel.send(embed=embed)

# =========================
# UI: Channel picker
# =========================
class AnnounceChannelSelect(discord.ui.ChannelSelect):
    def __init__(self, title: str, message: str):
        self.announcement_title = title
        self.announcement_message = message
        super().__init__(
            placeholder="Select a channel to post the announcement…",
            min_values=1,
            max_values=1,
            channel_types=[discord.ChannelType.text]
        )

    async def callback(self, interaction: discord.Interaction):
        chosen_channel = self.values[0]  # a TextChannel

        # Optional restriction
        if ALLOWED_ANNOUNCE_CHANNELS and chosen_channel.name not in ALLOWED_ANNOUNCE_CHANNELS:
            allowed_fmt = ", ".join(f"#{name}" for name in ALLOWED_ANNOUNCE_CHANNELS)
            await interaction.response.send_message(
                f"❌ That channel isn’t authorized for FEC announcements.\n"
                f"Allowed channels: {allowed_fmt}",
                ephemeral=True
            )
            return

        # Post the house-style announcement
        formatted = house_style_announcement(self.announcement_title, self.announcement_message)
        await chosen_channel.send(formatted)

        await interaction.response.send_message(
            f"✅ Announcement posted in {chosen_channel.mention}.",
            ephemeral=True
        )

        # Stop the view after success
        if self.view:
            self.view.stop()

class AnnounceChannelPicker(discord.ui.View):
    def __init__(self, title: str, message: str):
        super().__init__(timeout=60)
        self.add_item(AnnounceChannelSelect(title, message))

# =========================
# EVENTS / COMMANDS
# =========================
@bot.event
async def on_ready():
    guild = discord.Object(id=1419829129573957724)
    await bot.tree.sync(guild=guild)
    print(f"Logged in as {bot.user} (guild synced)")

@bot.tree.command(name="ping", description="Check if the FEC bot is online.")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message("✅ FEC bot is online.", ephemeral=True)

@bot.tree.command(name="announce", description="Create an official FEC announcement (FEC only).")
async def announce(interaction: discord.Interaction, title: str, message: str):
    # Must be used inside a server
    if not interaction.guild:
        await interaction.response.send_message("❌ This command can only be used in a server.", ephemeral=True)
        return

    # Permission check (FEC role)
    member = interaction.user if isinstance(interaction.user, discord.Member) else None
    if not member or not has_fec_role(member):
        case_id = generate_case_number()

        await interaction.response.send_message(
            "⚠️ **FEC NOTICE — UNAUTHORIZED ELECTION ACTIVITY**\n\n"
            "Pursuant to *FEC Administrative Code §1.04(b)*, you are not authorized "
            "to issue official election communications.\n\n"
            f"**Case Reference:** `{case_id}`\n\n"
            "This action has been logged. Continued attempts may result in administrative review.",
            ephemeral=True
        )

        await log_unauthorized_attempt(
            guild=interaction.guild,
            channel=interaction.channel,
            user=interaction.user,
            case_id=case_id,
            command_name="/announce"
        )
        return

    # Authorized: show channel dropdown privately
    await interaction.response.send_message(
        "Select the channel for this announcement:",
        ephemeral=True,
        view=AnnounceChannelPicker(title, message)
    )

# =========================
# RUN (Render-safe)
# =========================
import os
import asyncio
import discord

async def runner():
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise RuntimeError("DISCORD_TOKEN env var is missing.")

    backoff = 10  # seconds
    while True:
        try:
            async with bot:
                await bot.start(token)
        except discord.HTTPException as e:
            # If Discord/Cloudflare blocks the host, don't crash/restart rapidly
            print(f"[HTTPException] {e} — backing off {backoff}s")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 600)  # cap at 10 minutes
            continue
        except Exception as e:
            print(f"[Fatal] {e}")
            raise

if __name__ == "__main__":
    asyncio.run(runner())

