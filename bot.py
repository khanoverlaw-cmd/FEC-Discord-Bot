# bot.py
import os
import random
import traceback
from datetime import datetime, UTC

import discord
from discord.ext import commands

# =========================
# CONFIG
# =========================
FEC_ROLE_ID = 1462754795684233343  # replace with the actual role ID
LOG_CHANNEL_NAME = "interaction-logs"

ALLOWED_ANNOUNCE_CHANNELS = {"fec-announcements", "election results", "fec-public-records"}

# Optional: if you want a specific emoji name like :FEC:
# The message format will still work even if the emoji doesn't exist, it’ll just display text.
HEADER_PREFIX = ":FEC:"


# =========================
# HELPERS
# =========================
def make_case_reference() -> str:
    """
    Example: FEC-26-0119-8130
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
# Message content intent is NOT required for slash commands. Keep it off.
# intents.message_content = True

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
class AnnounceChannelSelect(discord.ui.ChannelSelect):
    def __init__(self, title: str, message: str):
        super().__init__(
            placeholder="Select the channel for this announcement:",
            channel_types=[discord.ChannelType.text, discord.ChannelType.news],
            min_values=1,
            max_values=1,
        )
        self.title = title
        self.message = message

    async def callback(self, interaction: discord.Interaction):
        try:
            chosen_channel = self.values[0]

            # Safety: ensure guild exists
            if interaction.guild is None:
                await interaction.response.send_message("❌ This command can only be used in a server.", ephemeral=True)
                return

            # Only allow specific channels by name
            if chosen_channel.name not in ALLOWED_ANNOUNCE_CHANNELS:
                case_ref = make_case_reference()
                await interaction.response.send_message(
                    f"❌ That channel isn’t authorized for FEC announcements.\n"
                    f"Allowed: {', '.join(sorted(ALLOWED_ANNOUNCE_CHANNELS))}\n"
                    f"Case Reference: `{case_ref}`",
                    ephemeral=True,
                )
                await log_event(
                    interaction.guild,
                    f"⚠️ **Blocked announce channel selection**\n"
                    f"User: {interaction.user.mention} (`{interaction.user.id}`)\n"
                    f"Channel: #{chosen_channel.name}\n"
                    f"Case: `{case_ref}`"
                )
                return

            # Post announcement
            content = f"{fmt_header(self.title)}\n\n{self.message}"
            await chosen_channel.send(content)

            # Confirm + log
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
    def __init__(self, title: str, message: str):
        super().__init__(timeout=300)
        self.add_item(AnnounceChannelSelect(title, message))

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
    await interaction.response.send_message("✅ Online.", ephemeral=True)


@bot.tree.command(name="announce", description="Post an FEC-formatted announcement to an approved channel.")
async def announce(interaction: discord.Interaction, title: str, message: str):
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

    # Send picker UI
    await interaction.response.send_message(
        "Select the channel for this announcement:",
        view=AnnounceChannelPicker(title, message),
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
