# bot.py
import os
import random
import traceback
from datetime import datetime, UTC

import discord
from discord.ext import commands
from discord import app_commands

# =========================
# CONFIG
# =========================
FEC_ROLE_ID = 1462754795684233343  # replace with the actual role ID
LOG_CHANNEL_NAME = "interaction-logs"

# Channel names can't contain spaces; use hyphens.
ALLOWED_ANNOUNCE_CHANNELS = {"fec-announcements", "election-results", "fec-public-records"}

# ✅ Reliable header prefix (always renders)
HEADER_PREFIX = "<:FEC:123456789012345678>"

# If you want your custom Discord emoji, replace HEADER_PREFIX with the FULL emoji tag:
# Example: HEADER_PREFIX = "<:FEC:123456789012345678>"
# To get it: type \:FEC: in Discord and copy the result.


# =========================
# HELPERS
# =========================
def make_case_reference() -> str:
    """
    Example: FEC-26-0122-8130
    YY-MMDD-RAND4
    """
    now = datetime.now(UTC)
    yy = now.strftime("%y")
    mmdd = now.strftime("%m%d")
    rand4 = random.randint(1000, 9999)
    return f"FEC-{yy}-{mmdd}-{rand4}"


def fmt_header(title: str) -> str:
    return f"# {HEADER_PREFIX} | {title}"


async def get_text_channel_by_name(guild: discord.Guild, name: str) -> discord.TextChannel | None:
    return discord.utils.get(guild.text_channels, name=name)


async def log_event(guild: discord.Guild, text: str) -> None:
    try:
        ch = await get_text_channel_by_name(guild, LOG_CHANNEL_NAME)
        if ch:
            await ch.send(text)
    except Exception:
        # Never allow logging to crash the bot
        pass


def user_has_fec_role(member: discord.Member) -> bool:
    return any(role.id == FEC_ROLE_ID for role in member.roles)


def unauthorized_notice(case_ref: str) -> str:
    return (
        "⚠️ **FEC NOTICE — UNAUTHORIZED ELECTION ACTIVITY**\n\n"
        "Pursuant to **FEC Administrative Code §1.04(b)**, you are not authorized to issue "
        "official election communications.\n\n"
        f"**Case Reference:** `{case_ref}`\n\n"
        "This action has been logged. Continued attempts may result in administrative review."
    )


# =========================
# DISCORD SETUP
# =========================
intents = discord.Intents.default()
# Message content intent is NOT required for slash commands.
bot = commands.Bot(command_prefix="!", intents=intents)


@bot.event
async def on_ready():
    try:
        synced = await bot.tree.sync()
        print(f"✅ Synced {len(synced)} commands.")
    except Exception as e:
        print(f"❌ Command sync failed: {e}")
    print(f"Logged in as {bot.user}")


# =========================
# UI: CHANNEL PICKER
# =========================
def get_bot_member(guild: discord.Guild) -> discord.Member | None:
    if bot.user is None:
        return None
    return guild.get_member(bot.user.id)


def eligible_announce_channels(guild: discord.Guild) -> list[discord.TextChannel]:
    bot_member = get_bot_member(guild)
    if bot_member is None:
        return []

    eligible: list[discord.TextChannel] = []
    for ch in guild.text_channels:
        if ch.name not in ALLOWED_ANNOUNCE_CHANNELS:
            continue

        perms = ch.permissions_for(bot_member)
        # If you later move to embeds, add perms.embed_links here too.
        if perms.view_channel and perms.send_messages:
            eligible.append(ch)

    return eligible


class AnnounceChannelSelect(discord.ui.Select):
    def __init__(self, title: str, message: str, channels: list[discord.TextChannel]):
        options = [
            discord.SelectOption(
                label=f"#{ch.name}",
                value=str(ch.id),
                description="Approved FEC channel",
            )
            for ch in channels[:25]  # Discord select options max is 25
        ]

        super().__init__(
            placeholder="Select the channel for this announcement:",
            min_values=1,
            max_values=1,
            options=options,
        )

        self.title = title
        self.message = message

    async def callback(self, interaction: discord.Interaction):
        try:
            if interaction.guild is None:
                await interaction.response.send_message(
                    "❌ This command can only be used in a server.",
                    ephemeral=True
                )
                return

            channel_id = int(self.values[0])
            chosen_channel = interaction.guild.get_channel(channel_id)

            if not isinstance(chosen_channel, discord.TextChannel):
                await interaction.response.send_message("❌ Invalid channel selection.", ephemeral=True)
                return

            # Double-check allowlist (extra safety)
            if chosen_channel.name not in ALLOWED_ANNOUNCE_CHANNELS:
                case_ref = make_case_reference()
                await interaction.response.send_message(
                    f"❌ That channel isn’t authorized.\nCase: `{case_ref}`",
                    ephemeral=True
                )
                await log_event(
                    interaction.guild,
                    f"⚠️ **Blocked announce channel selection**\n"
                    f"User: {interaction.user.mention} (`{interaction.user.id}`)\n"
                    f"Channel: #{chosen_channel.name}\n"
                    f"Case: `{case_ref}`"
                )
                return

            content = f"{fmt_header(self.title)}\n\n{self.message}"
            await chosen_channel.send(content)

            await interaction.response.send_message("✅ Announcement posted.", ephemeral=True)
            await log_event(
                interaction.guild,
                f"✅ **Announcement posted**\n"
                f"User: {interaction.user.mention} (`{interaction.user.id}`)\n"
                f"Channel: #{chosen_channel.name}\n"
                f"Title: {self.title}"
            )

        except Exception as e:
            print("=== UI ERROR START ===")
            traceback.print_exception(type(e), e, e.__traceback__)
            print("=== UI ERROR END ===")

            try:
                if interaction.response.is_done():
                    await interaction.followup.send("❌ Interaction failed. Error logged.", ephemeral=True)
                else:
                    await interaction.response.send_message("❌ Interaction failed. Error logged.", ephemeral=True)
            except Exception:
                pass


class AnnounceChannelPicker(discord.ui.View):
    def __init__(self, title: str, message: str, channels: list[discord.TextChannel]):
        super().__init__(timeout=300)
        self.add_item(AnnounceChannelSelect(title, message, channels))

    async def on_error(self, interaction: discord.Interaction, error: Exception, item):
        print("=== VIEW ERROR START ===")
        traceback.print_exception(type(error), error, error.__traceback__)
        print("=== VIEW ERROR END ===")
        try:
            if interaction.response.is_done():
                await interaction.followup.send("❌ Selection failed. The error was logged.", ephemeral=True)
            else:
                await interaction.response.send_message("❌ Selection failed. The error was logged.", ephemeral=True)
        except Exception:
            pass


# =========================
# SLASH COMMANDS
# =========================
@bot.tree.command(name="ping", description="Check if the FEC bot is online.")
async def ping(interaction: discord.Interaction):
    # Prevents 40060 "already acknowledged" from crashing.
    if interaction.response.is_done():
        await interaction.followup.send("✅ Online.", ephemeral=True)
    else:
        await interaction.response.send_message("✅ Online.", ephemeral=True)


@bot.tree.command(name="announce", description="Post an FEC-formatted announcement to an approved channel.")
@app_commands.describe(
    title="Announcement title",
    message="Full announcement text (supports multiple paragraphs / line breaks)"
)
async def announce(
    interaction: discord.Interaction,
    title: str,
    message: app_commands.Range[str, 1, 4000],  # ✅ makes Discord show a larger multiline input box
):
    if interaction.guild is None:
        await interaction.response.send_message("❌ This command can only be used in a server.", ephemeral=True)
        return

    # Role gate
    member = interaction.user
    if not isinstance(member, discord.Member):
        member = interaction.guild.get_member(interaction.user.id)

    if member is None or not user_has_fec_role(member):
        case_ref = make_case_reference()
        await interaction.response.send_message(unauthorized_notice(case_ref), ephemeral=True)
        await log_event(
            interaction.guild,
            f"⚠️ **Unauthorized /announce attempt**\n"
            f"User: {interaction.user.mention} (`{interaction.user.id}`)\n"
            f"Case: `{case_ref}`\n"
            f"Title attempted: {title}"
        )
        return

    # Defer quickly so Discord never times out (helps with Render lag/cold start)
    await interaction.response.defer(ephemeral=True)

    # Build eligible channel list
    channels = eligible_announce_channels(interaction.guild)

    # Guard: if no channels match or bot lacks perms, explain it clearly
    if not channels:
        await interaction.followup.send(
            "❌ No approved announcement channels found that I can post in.\n\n"
            f"Approved channel names: {', '.join(sorted(ALLOWED_ANNOUNCE_CHANNELS))}\n"
            "Fix: ensure those channels exist (exact names) and that I have **View Channel** + "
            "**Send Messages** permissions in them.",
            ephemeral=True
        )
        return

    # Show picker UI (as followup because we deferred)
    await interaction.followup.send(
        "Select the channel for this announcement:",
        view=AnnounceChannelPicker(title, str(message), channels),
        ephemeral=True
    )


# =========================
# RUN
# =========================
if __name__ == "__main__":
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise RuntimeError("DISCORD_TOKEN env var is missing. Add it in Render → Environment.")
    bot.run(token)
