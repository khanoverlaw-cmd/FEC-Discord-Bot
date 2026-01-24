# bot.py
import os
import random
import traceback
import asyncio
from datetime import datetime, timezone
from typing import Optional, Sequence, List, Dict

import discord
from discord.ext import commands
from discord import app_commands

import asyncpg

# =========================
# CONFIG
# =========================
DEV_GUILD_ID = 1419829129573957724

FEC_ROLE_ID = 1462754795684233343
AMERICAN_CITIZEN_ROLE_ID = 1419829130043723911

LOG_CHANNEL_NAME = "interaction-logs"
BALLOT_CHANNEL_ID = 1464147534887915593  # ballot-counting

RESULTS_CHANNEL_NAME = "election-results"  # live updates go here (by name)
ALLOWED_ANNOUNCE_CHANNELS = {"fec-announcements", "election-results", "fec-public-records"}

HEADER_PREFIX = "<:FEC:123456789012345678>"

HOUSE_MAX_PICKS = 3
SENATE_MAX_PICKS = 2

MAX_SELECT_OPTIONS = 25
DISCORD_MESSAGE_LIMIT = 2000

AUTO_UPDATE_MIN_SECONDS = 30  # throttle for results edits

STATE_CODES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA",
    "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD",
    "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ",
    "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC",
    "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY"
}

# =========================
# HELPERS
# =========================
def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def make_case_reference() -> str:
    yy = now_utc().strftime("%y")
    mmdd = now_utc().strftime("%m%d")
    rand4 = random.randint(1000, 9999)
    return f"FEC-{yy}-{mmdd}-{rand4}"


def fmt_header(title: str) -> str:
    return f"# {HEADER_PREFIX} | {title}"


def normalize_message_text(text: str) -> str:
    return text.replace("\\n", "\n")


def has_role_id(member: discord.Member, role_id: int) -> bool:
    return any(r.id == role_id for r in member.roles)


def bar(pct: float, width: int = 18) -> str:
    pct = max(0.0, min(100.0, pct))
    filled = int(round((pct / 100.0) * width))
    return "█" * filled + "░" * (width - filled)


def normalize_db_url(url: str) -> str:
    if url.startswith("postgres://"):
        return "postgresql://" + url[len("postgres://"):]
    return url


def chunk_lines(lines: list[str], limit: int = DISCORD_MESSAGE_LIMIT) -> list[str]:
    chunks: list[str] = []
    cur: list[str] = []
    cur_len = 0
    for line in lines:
        add_len = len(line) + 1
        if cur and (cur_len + add_len > limit):
            chunks.append("\n".join(cur))
            cur = [line]
            cur_len = len(line)
        else:
            cur.append(line)
            cur_len += add_len
    if cur:
        chunks.append("\n".join(cur))
    return chunks


def candidate_label(office: str, name: str, party: str, state: Optional[str], district: Optional[int]) -> str:
    if office == "HOUSE" and state and district:
        return f"{name} ({party}) — {state}-{district:02d}"
    if office == "SENATE" and state:
        return f"{name} ({party}) — {state} SEN"
    if office == "PRESIDENT":
        return f"{name} ({party}) — POTUS"
    return f"{name} ({party})"


def pretty_party(p: str) -> str:
    p = (p or "").upper().strip()
    return {"DEM": "Democratic", "REP": "Republican", "IND": "Independent", "LIB": "Libertarian", "GRN": "Green"}.get(p, p or "—")


def office_badge(office: str) -> str:
    return {"HOUSE": "🏛️ House", "SENATE": "🏛️ Senate", "PRESIDENT": "🇺🇸 President"}.get(office, office)


def election_status_badge(status: str) -> str:
    s = (status or "").upper()
    return {"DRAFT": "📝 DRAFT", "OPEN": "🟢 OPEN", "CLOSED": "🔴 CLOSED", "CERTIFIED": "✅ CERTIFIED"}.get(s, s)


async def safe_log_event(guild: discord.Guild, text: str) -> None:
    try:
        ch = discord.utils.get(guild.text_channels, name=LOG_CHANNEL_NAME)
        if ch:
            await ch.send(text)
    except Exception:
        pass


# =========================
# DB SETUP
# =========================
DB_POOL: Optional[asyncpg.Pool] = None
DB_LOCK = asyncio.Lock()


def get_database_url() -> str:
    raw = (os.getenv("DATABASE_URL") or "").strip()
    if not raw:
        raise RuntimeError("DATABASE_URL env var is missing/empty (Render Postgres).")
    return normalize_db_url(raw)


async def ensure_schema(pool: asyncpg.Pool) -> None:
    stmts = [
        """
        CREATE TABLE IF NOT EXISTS elections (
            election_id TEXT PRIMARY KEY,
            election_type TEXT NOT NULL,      -- SPECIAL / GENERAL / MIDTERMS
            include_house BOOLEAN NOT NULL,
            include_senate BOOLEAN NOT NULL,
            include_pres BOOLEAN NOT NULL,
            status TEXT NOT NULL DEFAULT 'DRAFT',  -- DRAFT / OPEN / CLOSED / CERTIFIED
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """,
        """
        CREATE TABLE IF NOT EXISTS candidates (
            candidate_id SERIAL PRIMARY KEY,
            election_id TEXT NOT NULL REFERENCES elections(election_id) ON DELETE CASCADE,
            office TEXT NOT NULL,             -- HOUSE / SENATE / PRESIDENT
            rp_name TEXT NOT NULL,
            party TEXT NOT NULL,
            state TEXT,
            district INT,
            UNIQUE (election_id, office, rp_name)
        );
        """,
        """
        CREATE TABLE IF NOT EXISTS ballots (
            ballot_id BIGSERIAL PRIMARY KEY,
            election_id TEXT NOT NULL REFERENCES elections(election_id) ON DELETE CASCADE,
            voter_id BIGINT NOT NULL,
            voter_username TEXT NOT NULL,
            voter_nickname TEXT,
            house_choices INT[] NOT NULL DEFAULT '{}',
            senate_choices INT[] NOT NULL DEFAULT '{}',
            pres_choice INT,
            status TEXT NOT NULL DEFAULT 'PENDING',  -- PENDING / ACCEPTED / REJECTED
            reviewed_by BIGINT,
            reviewed_at TIMESTAMPTZ,
            reject_reason TEXT,
            submitted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (election_id, voter_id)
        );
        """,
        "ALTER TABLE elections ADD COLUMN IF NOT EXISTS results_message_id BIGINT;",
        "ALTER TABLE elections ADD COLUMN IF NOT EXISTS last_results_update_at TIMESTAMPTZ;",
        """
        CREATE TABLE IF NOT EXISTS election_certifications (
            election_id TEXT PRIMARY KEY REFERENCES elections(election_id) ON DELETE CASCADE,
            certified_by BIGINT NOT NULL,
            certified_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            submitted_ballots INT NOT NULL,
            accepted_ballots INT NOT NULL,
            house_snapshot JSONB,
            senate_snapshot JSONB,
            pres_snapshot JSONB,
            notes TEXT
        );
        """,
    ]

    async with pool.acquire() as conn:
        async with conn.transaction():
            for s in stmts:
                await conn.execute(s)


async def ensure_db_pool() -> asyncpg.Pool:
    global DB_POOL
    if DB_POOL is not None:
        return DB_POOL
    async with DB_LOCK:
        if DB_POOL is not None:
            return DB_POOL
        dsn = get_database_url()
        DB_POOL = await asyncpg.create_pool(dsn=dsn, min_size=1, max_size=5)
        await ensure_schema(DB_POOL)
        return DB_POOL


async def db() -> asyncpg.Pool:
    return await ensure_db_pool()


# =========================
# DISCORD BOT SETUP
# =========================
intents = discord.Intents.default()
intents.members = True  # IMPORTANT: role checks + nicknames

class FECBot(commands.Bot):
    async def setup_hook(self) -> None:
        try:
            await ensure_db_pool()
            print("✅ DB pool initialized.")
        except Exception as e:
            print("❌ DB init failed (bot will still start):")
            traceback.print_exception(type(e), e, e.__traceback__)

        try:
            guild_obj = discord.Object(id=DEV_GUILD_ID)
            self.tree.copy_global_to(guild=guild_obj)
            synced = await self.tree.sync(guild=guild_obj)
            print(f"✅ Synced {len(synced)} commands to DEV guild {DEV_GUILD_ID}.")
        except Exception as e:
            print("❌ Command sync failed (continuing; bot will still run):")
            traceback.print_exception(type(e), e, e.__traceback__)

    async def on_ready(self):
        if getattr(self, "_ready_once", False):
            return
        self._ready_once = True
        print(f"✅ Logged in as {self.user}")


bot = FECBot(command_prefix=None, intents=intents)


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    print("❌ App command error:", repr(error))
    traceback.print_exception(type(error), error, error.__traceback__)

    msg = "❌ Something broke while running that command. The error was logged."
    try:
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    except Exception:
        pass


# =========================
# PERMISSIONS
# =========================
async def require_guild(interaction: discord.Interaction) -> Optional[discord.Guild]:
    if interaction.guild is None:
        if not interaction.response.is_done():
            await interaction.response.send_message("❌ Server-only.", ephemeral=True)
        return None
    return interaction.guild


async def require_fec(interaction: discord.Interaction) -> Optional[discord.Member]:
    g = await require_guild(interaction)
    if g is None:
        return None
    member = interaction.user
    if not isinstance(member, discord.Member):
        member = g.get_member(interaction.user.id)
    if member is None or not has_role_id(member, FEC_ROLE_ID):
        msg = "❌ FEC only."
        if not interaction.response.is_done():
            await interaction.response.send_message(msg, ephemeral=True)
        else:
            await interaction.followup.send(msg, ephemeral=True)
        return None
    return member


async def require_voter(interaction: discord.Interaction) -> Optional[discord.Member]:
    g = await require_guild(interaction)
    if g is None:
        return None
    member = interaction.user
    if not isinstance(member, discord.Member):
        member = g.get_member(interaction.user.id)
    if member is None or not has_role_id(member, AMERICAN_CITIZEN_ROLE_ID):
        msg = "❌ You are not eligible to vote (American Citizen role required)."
        if not interaction.response.is_done():
            await interaction.response.send_message(msg, ephemeral=True)
        else:
            await interaction.followup.send(msg, ephemeral=True)
        return None
    return member


async def fetch_election(pool: asyncpg.Pool, election_id: str):
    return await pool.fetchrow("SELECT * FROM elections WHERE election_id=$1", election_id)


async def get_results_channel(guild: discord.Guild) -> Optional[discord.TextChannel]:
    ch = discord.utils.get(guild.text_channels, name=RESULTS_CHANNEL_NAME)
    return ch if isinstance(ch, discord.TextChannel) else None


# =========================
# RESULTS / TABULATION
# =========================
async def reporting_stats(pool: asyncpg.Pool, election_id: str) -> tuple[int, int, float]:
    submitted = await pool.fetchval("SELECT COUNT(*) FROM ballots WHERE election_id=$1", election_id) or 0
    accepted = await pool.fetchval(
        "SELECT COUNT(*) FROM ballots WHERE election_id=$1 AND status='ACCEPTED'",
        election_id
    ) or 0
    pct = (accepted / submitted * 100.0) if submitted else 0.0
    return int(submitted), int(accepted), float(pct)


async def office_results(pool: asyncpg.Pool, election_id: str, office: str) -> tuple[list[tuple[str, int, float]], int]:
    if office == "PRESIDENT":
        recs = await pool.fetch(
            """
            SELECT c.rp_name, c.party, c.state, c.district, COUNT(*)::int AS votes
            FROM ballots b
            JOIN candidates c ON c.candidate_id = b.pres_choice
            WHERE b.election_id=$1 AND b.status='ACCEPTED' AND c.office='PRESIDENT'
            GROUP BY c.rp_name, c.party, c.state, c.district
            ORDER BY votes DESC, c.rp_name ASC
            """,
            election_id
        )
    else:
        col = "house_choices" if office == "HOUSE" else "senate_choices"
        recs = await pool.fetch(
            f"""
            SELECT c.rp_name, c.party, c.state, c.district, COUNT(*)::int AS votes
            FROM ballots b
            JOIN LATERAL unnest(b.{col}) AS cid ON TRUE
            JOIN candidates c ON c.candidate_id = cid
            WHERE b.election_id=$1 AND b.status='ACCEPTED' AND c.office=$2
            GROUP BY c.rp_name, c.party, c.state, c.district
            ORDER BY votes DESC, c.rp_name ASC
            """,
            election_id, office
        )

    total = sum(r["votes"] for r in recs) or 0
    out: list[tuple[str, int, float]] = []
    for r in recs:
        label = candidate_label(office, r["rp_name"], r["party"], r["state"], r["district"])
        pct = (r["votes"] / total * 100.0) if total else 0.0
        out.append((label, int(r["votes"]), float(pct)))
    return out, int(total)


async def build_results_embed(pool: asyncpg.Pool, election) -> discord.Embed:
    election_id = election["election_id"]
    submitted, accepted, pct = await reporting_stats(pool, election_id)

    em = discord.Embed(
        title=f"🗳️ Election Night Update — {election_id}",
        description=(
            f"**Status:** {election_status_badge(election['status'])}\n"
            f"**Reporting:** {bar(pct)} **{pct:.1f}%**\n"
            f"Accepted **{accepted}** / Submitted **{submitted}**"
        ),
        timestamp=now_utc()
    )
    em.set_footer(text="Federal Elections Commission • Live Reporting")

    if election["include_house"]:
        res, _ = await office_results(pool, election_id, "HOUSE")
        top = "\n".join([f"• {label} — **{votes}** ({p:.1f}%)" for label, votes, p in res[:3]]) if res else "— No accepted votes yet."
        em.add_field(name="🏛️ House (Top 3)", value=top, inline=False)

    if election["include_senate"]:
        res, _ = await office_results(pool, election_id, "SENATE")
        top = "\n".join([f"• {label} — **{votes}** ({p:.1f}%)" for label, votes, p in res[:3]]) if res else "— No accepted votes yet."
        em.add_field(name="🏛️ Senate (Top 3)", value=top, inline=False)

    if election["include_pres"]:
        res, _ = await office_results(pool, election_id, "PRESIDENT")
        top = "\n".join([f"• {label} — **{votes}** ({p:.1f}%)" for label, votes, p in res[:3]]) if res else "— No accepted votes yet."
        em.add_field(name="🇺🇸 President (Top 3)", value=top, inline=False)

    return em


async def post_or_edit_auto_update(guild: discord.Guild, election_id: str) -> None:
    pool = await db()
    election = await fetch_election(pool, election_id)
    if not election:
        return

    ch = await get_results_channel(guild)
    if not ch:
        return

    last = election["last_results_update_at"]
    if last is not None:
        elapsed = (now_utc() - last).total_seconds()
        if elapsed < AUTO_UPDATE_MIN_SECONDS:
            return

    embed = await build_results_embed(pool, election)
    msg_id = election["results_message_id"]

    try:
        if msg_id:
            try:
                msg = await ch.fetch_message(int(msg_id))
                await msg.edit(embed=embed, content=None)
            except (discord.NotFound, discord.Forbidden):
                sent = await ch.send(embed=embed)
                await pool.execute(
                    "UPDATE elections SET results_message_id=$2 WHERE election_id=$1",
                    election_id, sent.id
                )
        else:
            sent = await ch.send(embed=embed)
            await pool.execute(
                "UPDATE elections SET results_message_id=$2 WHERE election_id=$1",
                election_id, sent.id
            )

        await pool.execute(
            "UPDATE elections SET last_results_update_at=NOW() WHERE election_id=$1",
            election_id
        )

    except discord.HTTPException as e:
        print(f"⚠️ Auto-update failed: {e}")


# =========================
# ANNOUNCEMENTS
# =========================
def get_bot_member(guild: discord.Guild) -> Optional[discord.Member]:
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
        if perms.view_channel and perms.send_messages:
            eligible.append(ch)
    return eligible


class AnnounceChannelSelect(discord.ui.Select):
    def __init__(self, title: str, message: str, channels: list[discord.TextChannel]):
        options = [
            discord.SelectOption(label=f"#{ch.name}", value=str(ch.id), description="Approved FEC channel")
            for ch in channels[:25]
        ]
        super().__init__(placeholder="Select an approved channel:", min_values=1, max_values=1, options=options)
        self.title = title
        self.message = message

    async def callback(self, interaction: discord.Interaction):
        try:
            if interaction.guild is None:
                await interaction.response.send_message("❌ Server-only.", ephemeral=True)
                return

            channel_id = int(self.values[0])
            chosen_channel = interaction.guild.get_channel(channel_id)
            if not isinstance(chosen_channel, discord.TextChannel):
                await interaction.response.send_message("❌ Invalid channel.", ephemeral=True)
                return

            if chosen_channel.name not in ALLOWED_ANNOUNCE_CHANNELS:
                await interaction.response.send_message(
                    f"❌ Channel not authorized. Case `{make_case_reference()}`",
                    ephemeral=True
                )
                return

            body = normalize_message_text(self.message)
            content = f"{fmt_header(self.title)}\n\n{body}"
            await chosen_channel.send(content)
            await interaction.response.send_message("✅ Announcement posted.", ephemeral=True)

        except Exception as e:
            traceback.print_exception(type(e), e, e.__traceback__)
            try:
                if interaction.response.is_done():
                    await interaction.followup.send("❌ Failed (logged).", ephemeral=True)
                else:
                    await interaction.response.send_message("❌ Failed (logged).", ephemeral=True)
            except Exception:
                pass


class AnnounceChannelPicker(discord.ui.View):
    def __init__(self, title: str, message: str, channels: list[discord.TextChannel]):
        super().__init__(timeout=300)
        self.add_item(AnnounceChannelSelect(title, message, channels))


@bot.tree.command(name="announce", description="Post an FEC-formatted announcement to an approved channel.")
@app_commands.describe(title="Announcement title", message="Text. Use \\n for line breaks if Shift+Enter fails.")
async def announce(interaction: discord.Interaction, title: str, message: app_commands.Range[str, 1, 4000]):
    member = await require_fec(interaction)
    if member is None:
        return
    await interaction.response.defer(ephemeral=True)
    channels = eligible_announce_channels(interaction.guild)
    if not channels:
        await interaction.followup.send("❌ No approved announce channels available for me.", ephemeral=True)
        return
    await interaction.followup.send(
        "Select the channel for this announcement:",
        view=AnnounceChannelPicker(title, str(message), channels),
        ephemeral=True
    )


# =========================
# CHOICES (<=25 only)
# =========================
ELECTION_TYPES = [
    app_commands.Choice(name="Special", value="SPECIAL"),
    app_commands.Choice(name="General", value="GENERAL"),
    app_commands.Choice(name="Midterms", value="MIDTERMS"),
]

OFFICES = [
    app_commands.Choice(name="House", value="HOUSE"),
    app_commands.Choice(name="Senate", value="SENATE"),
    app_commands.Choice(name="President", value="PRESIDENT"),
]

PARTIES = [
    app_commands.Choice(name="DEM", value="DEM"),
    app_commands.Choice(name="REP", value="REP"),
    app_commands.Choice(name="IND", value="IND"),
    app_commands.Choice(name="LIB", value="LIB"),
    app_commands.Choice(name="GRN", value="GRN"),
]


# =========================
# STATE AUTOCOMPLETE
# =========================
async def state_autocomplete(interaction: discord.Interaction, current: str) -> List[app_commands.Choice[str]]:
    cur = (current or "").strip().upper()
    matches = [s for s in sorted(STATE_CODES) if s.startswith(cur)] if cur else sorted(STATE_CODES)
    matches = matches[:25]
    return [app_commands.Choice(name=s, value=s) for s in matches]


# =========================
# ELECTION COMMANDS
# =========================
@bot.tree.command(name="begin_election", description="(FEC) Create a new election event.")
@app_commands.choices(election_type=ELECTION_TYPES)
@app_commands.describe(
    election_id="Short ID, e.g. January26 (no spaces)",
    election_type="Special / General / Midterms",
    include_house="Include House ballot",
    include_senate="Include Senate ballot",
    include_pres="Include Presidential ballot"
)
async def begin_election(
    interaction: discord.Interaction,
    election_id: str,
    election_type: app_commands.Choice[str],
    include_house: bool = True,
    include_senate: bool = True,
    include_pres: bool = False
):
    member = await require_fec(interaction)
    if member is None:
        return

    pool = await db()
    try:
        await pool.execute(
            """
            INSERT INTO elections (election_id, election_type, include_house, include_senate, include_pres, status)
            VALUES ($1,$2,$3,$4,$5,'DRAFT')
            """,
            election_id, election_type.value, include_house, include_senate, include_pres
        )
        em = discord.Embed(
            title="✅ Election Created",
            description=(
                f"**Election ID:** `{election_id}`\n"
                f"**Type:** `{election_type.value}`\n"
                f"**Status:** {election_status_badge('DRAFT')}\n\n"
                f"Next: `/add_candidate` then `/election_open`."
            ),
            timestamp=now_utc()
        )
        await interaction.response.send_message(embed=em, ephemeral=True)
    except asyncpg.UniqueViolationError:
        await interaction.response.send_message(f"❌ Election `{election_id}` already exists.", ephemeral=True)


@bot.tree.command(name="add_candidate", description="(FEC) Add a candidate to an election.")
@app_commands.choices(office=OFFICES, party=PARTIES)
@app_commands.autocomplete(state=state_autocomplete)
@app_commands.describe(
    election_id="Election ID (e.g. January26)",
    office="House / Senate / President",
    rp_name="Candidate RP name",
    party="Party",
    state="Two-letter state code (e.g. TX). Required for House/Senate.",
    district="House district (required ONLY for House)"
)
async def add_candidate(
    interaction: discord.Interaction,
    election_id: str,
    office: app_commands.Choice[str],
    rp_name: str,
    party: app_commands.Choice[str],
    state: Optional[str] = None,
    district: Optional[int] = None
):
    member = await require_fec(interaction)
    if member is None:
        return

    pool = await db()
    election = await fetch_election(pool, election_id)
    if not election:
        await interaction.response.send_message(f"❌ Election `{election_id}` not found.", ephemeral=True)
        return

    if (election["status"] or "").upper() == "CERTIFIED":
        await interaction.response.send_message("❌ Election is CERTIFIED; candidates cannot be modified.", ephemeral=True)
        return

    off = office.value
    party_val = party.value
    state_val = state.strip().upper() if state else None

    if off in ("HOUSE", "SENATE"):
        if not state_val:
            await interaction.response.send_message("❌ State is required for House/Senate candidates.", ephemeral=True)
            return
        if state_val not in STATE_CODES:
            await interaction.response.send_message("❌ Invalid state. Use a 2-letter code like TX, CA, LA.", ephemeral=True)
            return

    if off == "HOUSE":
        if district is None or district < 1:
            await interaction.response.send_message("❌ District is required for House candidates.", ephemeral=True)
            return
    else:
        district = None

    try:
        row = await pool.fetchrow(
            """
            INSERT INTO candidates (election_id, office, rp_name, party, state, district)
            VALUES ($1,$2,$3,$4,$5,$6)
            RETURNING candidate_id
            """,
            election_id, off, rp_name, party_val, state_val, district
        )
        em = discord.Embed(
            title="✅ Candidate Registered",
            description=(
                f"**Election:** `{election_id}`\n"
                f"**Office:** {office_badge(off)}\n"
                f"**Candidate:** {candidate_label(off, rp_name, party_val, state_val, district)}\n"
                f"**Candidate ID:** `{row['candidate_id']}`"
            ),
            timestamp=now_utc()
        )
        await interaction.response.send_message(embed=em, ephemeral=True)
    except asyncpg.UniqueViolationError:
        await interaction.response.send_message("❌ That candidate already exists for this election/office.", ephemeral=True)


@bot.tree.command(name="election_open", description="(FEC) Open polls for an election.")
async def election_open(interaction: discord.Interaction, election_id: str):
    member = await require_fec(interaction)
    if member is None:
        return
    pool = await db()
    election = await fetch_election(pool, election_id)
    if not election:
        await interaction.response.send_message(f"❌ Election `{election_id}` not found.", ephemeral=True)
        return
    if (election["status"] or "").upper() == "CERTIFIED":
        await interaction.response.send_message("❌ Election is CERTIFIED; cannot reopen.", ephemeral=True)
        return
    if election["status"] == "OPEN":
        await interaction.response.send_message(f"✅ `{election_id}` is already OPEN.", ephemeral=True)
        return
    await pool.execute("UPDATE elections SET status='OPEN' WHERE election_id=$1", election_id)
    await interaction.response.send_message(f"🟢 Polls OPEN for `{election_id}`.", ephemeral=True)
    if interaction.guild:
        await post_or_edit_auto_update(interaction.guild, election_id)


@bot.tree.command(name="election_close", description="(FEC) Close polls for an election.")
async def election_close(interaction: discord.Interaction, election_id: str):
    member = await require_fec(interaction)
    if member is None:
        return
    pool = await db()
    election = await fetch_election(pool, election_id)
    if not election:
        await interaction.response.send_message(f"❌ Election `{election_id}` not found.", ephemeral=True)
        return
    if (election["status"] or "").upper() == "CERTIFIED":
        await interaction.response.send_message("❌ Election is CERTIFIED; cannot change status.", ephemeral=True)
        return
    if election["status"] == "CLOSED":
        await interaction.response.send_message(f"✅ `{election_id}` is already CLOSED.", ephemeral=True)
        return
    await pool.execute("UPDATE elections SET status='CLOSED' WHERE election_id=$1", election_id)
    await interaction.response.send_message(f"🔴 Polls CLOSED for `{election_id}`.", ephemeral=True)
    if interaction.guild:
        await post_or_edit_auto_update(interaction.guild, election_id)


# =========================
# VOTING UI (dropdowns + paging)
# =========================
class PagedMultiSelect(discord.ui.Select):
    def __init__(self, placeholder: str, max_picks: int, options: list[discord.SelectOption], on_change):
        self._all_options = options
        self.page = 0
        self.max_picks = max_picks
        self.on_change = on_change

        page_opts = self._page_options()
        super().__init__(
            placeholder=placeholder,
            min_values=0,
            max_values=min(max_picks, len(page_opts)) if page_opts else 0,
            options=page_opts,
        )

    def _page_options(self) -> list[discord.SelectOption]:
        start = self.page * MAX_SELECT_OPTIONS
        end = start + MAX_SELECT_OPTIONS
        return self._all_options[start:end]

    def set_page(self, page: int):
        self.page = max(0, min(page, self.total_pages() - 1))
        page_opts = self._page_options()
        self.options = page_opts
        self.max_values = min(self.max_picks, len(page_opts)) if page_opts else 0

    def total_pages(self) -> int:
        if not self._all_options:
            return 1
        return ((len(self._all_options) - 1) // MAX_SELECT_OPTIONS) + 1

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await self.on_change(interaction, [int(v) for v in self.values])


class VoteView(discord.ui.View):
    def __init__(
        self,
        election_id: str,
        include_house: bool,
        include_senate: bool,
        include_pres: bool,
        house_opts: list[discord.SelectOption],
        senate_opts: list[discord.SelectOption],
        pres_opts: list[discord.SelectOption],
    ):
        super().__init__(timeout=300)
        self.election_id = election_id
        self.house_selected: list[int] = []
        self.senate_selected: list[int] = []
        self.pres_selected: Optional[int] = None

        self.house_select: Optional[PagedMultiSelect] = None
        self.senate_select: Optional[PagedMultiSelect] = None
        self.pres_select: Optional[discord.ui.Select] = None

        if include_house:
            async def on_house_change(_interaction, values):
                uniq: list[int] = []
                for v in values:
                    if v not in uniq:
                        uniq.append(v)
                self.house_selected = uniq[:HOUSE_MAX_PICKS]

            self.house_select = PagedMultiSelect(
                placeholder=f"House (pick up to {HOUSE_MAX_PICKS})",
                max_picks=HOUSE_MAX_PICKS,
                options=house_opts,
                on_change=on_house_change,
            )
            self.add_item(self.house_select)
            self.add_item(self.HousePrevButton(self))
            self.add_item(self.HouseNextButton(self))

        if include_senate:
            async def on_senate_change(_interaction, values):
                uniq: list[int] = []
                for v in values:
                    if v not in uniq:
                        uniq.append(v)
                self.senate_selected = uniq[:SENATE_MAX_PICKS]

            self.senate_select = PagedMultiSelect(
                placeholder=f"Senate (pick up to {SENATE_MAX_PICKS})",
                max_picks=SENATE_MAX_PICKS,
                options=senate_opts,
                on_change=on_senate_change,
            )
            self.add_item(self.senate_select)
            self.add_item(self.SenatePrevButton(self))
            self.add_item(self.SenateNextButton(self))

        if include_pres:
            options = pres_opts[:25]
            self.pres_select = discord.ui.Select(
                placeholder="President (pick 1)",
                min_values=0,
                max_values=1,
                options=options
            )

            async def pres_callback(interaction: discord.Interaction):
                await interaction.response.defer(ephemeral=True)
                if self.pres_select and self.pres_select.values:
                    self.pres_selected = int(self.pres_select.values[0])
                else:
                    self.pres_selected = None

            self.pres_select.callback = pres_callback  # type: ignore
            self.add_item(self.pres_select)

        self.add_item(self.SubmitButton(self))

    class HousePrevButton(discord.ui.Button):
        def __init__(self, parent: "VoteView"):
            super().__init__(label="◀ House Prev", style=discord.ButtonStyle.secondary)
            self.parent = parent

        async def callback(self, interaction: discord.Interaction):
            if not self.parent.house_select:
                await interaction.response.defer(ephemeral=True)
                return
            self.parent.house_select.set_page(self.parent.house_select.page - 1)
            await interaction.response.edit_message(view=self.parent)

    class HouseNextButton(discord.ui.Button):
        def __init__(self, parent: "VoteView"):
            super().__init__(label="House Next ▶", style=discord.ButtonStyle.secondary)
            self.parent = parent

        async def callback(self, interaction: discord.Interaction):
            if not self.parent.house_select:
                await interaction.response.defer(ephemeral=True)
                return
            self.parent.house_select.set_page(self.parent.house_select.page + 1)
            await interaction.response.edit_message(view=self.parent)

    class SenatePrevButton(discord.ui.Button):
        def __init__(self, parent: "VoteView"):
            super().__init__(label="◀ Senate Prev", style=discord.ButtonStyle.secondary)
            self.parent = parent

        async def callback(self, interaction: discord.Interaction):
            if not self.parent.senate_select:
                await interaction.response.defer(ephemeral=True)
                return
            self.parent.senate_select.set_page(self.parent.senate_select.page - 1)
            await interaction.response.edit_message(view=self.parent)

    class SenateNextButton(discord.ui.Button):
        def __init__(self, parent: "VoteView"):
            super().__init__(label="Senate Next ▶", style=discord.ButtonStyle.secondary)
            self.parent = parent

        async def callback(self, interaction: discord.Interaction):
            if not self.parent.senate_select:
                await interaction.response.defer(ephemeral=True)
                return
            self.parent.senate_select.set_page(self.parent.senate_select.page + 1)
            await interaction.response.edit_message(view=self.parent)

    class SubmitButton(discord.ui.Button):
        def __init__(self, parent: "VoteView"):
            super().__init__(label="Submit Ballot", style=discord.ButtonStyle.green)
            self.parent = parent

        async def callback(self, interaction: discord.Interaction):
            voter = await require_voter(interaction)
            if voter is None:
                return
            await interaction.response.defer(ephemeral=True)

            pool = await db()
            election = await fetch_election(pool, self.parent.election_id)
            if not election:
                await interaction.followup.send("❌ Election not found.", ephemeral=True)
                return
            if (election["status"] or "").upper() != "OPEN":
                await interaction.followup.send("❌ Polls are not open for this election.", ephemeral=True)
                return

            # At least one selection across enabled offices
            empty_house = (not election["include_house"]) or (len(self.parent.house_selected) == 0)
            empty_senate = (not election["include_senate"]) or (len(self.parent.senate_selected) == 0)
            empty_pres = (not election["include_pres"]) or (self.parent.pres_selected is None)

            if empty_house and empty_senate and empty_pres:
                await interaction.followup.send("❌ Your ballot is empty. Select at least one candidate.", ephemeral=True)
                return

            voter_username = str(interaction.user)
            voter_nick = voter.display_name if isinstance(voter, discord.Member) else None

            try:
                row = await pool.fetchrow(
                    """
                    INSERT INTO ballots (election_id, voter_id, voter_username, voter_nickname,
                                        house_choices, senate_choices, pres_choice, status)
                    VALUES ($1,$2,$3,$4,$5,$6,$7,'PENDING')
                    RETURNING ballot_id
                    """,
                    self.parent.election_id,
                    interaction.user.id,
                    voter_username,
                    voter_nick,
                    self.parent.house_selected,
                    self.parent.senate_selected,
                    self.parent.pres_selected,
                )
            except asyncpg.UniqueViolationError:
                await interaction.followup.send("❌ You already submitted a ballot for this election.", ephemeral=True)
                return

            ballot_id = row["ballot_id"]

            if interaction.guild:
                ch = interaction.guild.get_channel(BALLOT_CHANNEL_ID)
                if isinstance(ch, discord.TextChannel):
                    embed = discord.Embed(
                        title="🗳️ Ballot Submitted (PENDING)",
                        description=f"Election: `{self.parent.election_id}`\nBallot ID: `{ballot_id}`",
                        timestamp=now_utc()
                    )
                    embed.add_field(
                        name="Discord Username & ID",
                        value=f"{interaction.user} (`{interaction.user.id}`)",
                        inline=False
                    )
                    embed.add_field(name="Server Nickname", value=voter_nick or "N/A", inline=False)
                    embed.set_footer(text="Use /ballots_next to review and accept/reject ballots.")
                    await ch.send(embed=embed)

            await interaction.followup.send(
                f"✅ Thank you, {interaction.user.mention}. Your ballot is received and pending review.",
                ephemeral=True
            )
            self.parent.stop()


@bot.tree.command(name="vote", description="Cast your ballot (American Citizen role required).")
@app_commands.describe(election_id="Election ID (e.g. January26)")
async def vote(interaction: discord.Interaction, election_id: str):
    voter = await require_voter(interaction)
    if voter is None:
        return

    await interaction.response.defer(ephemeral=True)

    pool = await db()
    election = await fetch_election(pool, election_id)
    if not election:
        await interaction.followup.send(f"❌ Election `{election_id}` not found.", ephemeral=True)
        return
    if (election["status"] or "").upper() != "OPEN":
        await interaction.followup.send(f"❌ Polls are not open for `{election_id}`.", ephemeral=True)
        return

    rows = await pool.fetch(
        "SELECT * FROM candidates WHERE election_id=$1 ORDER BY office, state NULLS LAST, district NULLS LAST, rp_name",
        election_id
    )

    house_opts: list[discord.SelectOption] = []
    senate_opts: list[discord.SelectOption] = []
    pres_opts: list[discord.SelectOption] = []

    for r in rows:
        label = candidate_label(r["office"], r["rp_name"], r["party"], r["state"], r["district"])
        opt = discord.SelectOption(label=label[:100], value=str(r["candidate_id"]))
        if r["office"] == "HOUSE":
            house_opts.append(opt)
        elif r["office"] == "SENATE":
            senate_opts.append(opt)
        elif r["office"] == "PRESIDENT":
            pres_opts.append(opt)

    if election["include_house"] and not house_opts:
        await interaction.followup.send("❌ Election includes House but has no House candidates.", ephemeral=True)
        return
    if election["include_senate"] and not senate_opts:
        await interaction.followup.send("❌ Election includes Senate but has no Senate candidates.", ephemeral=True)
        return
    if election["include_pres"] and not pres_opts:
        await interaction.followup.send("❌ Election includes President but has no Presidential candidates.", ephemeral=True)
        return
    if election["include_pres"] and len(pres_opts) > 25:
        await interaction.followup.send("❌ Too many Presidential candidates (>25).", ephemeral=True)
        return

    description = (
        f"**Election:** `{election_id}`\n"
        f"**House:** up to {HOUSE_MAX_PICKS} • **Senate:** up to {SENATE_MAX_PICKS} • **President:** 1\n"
        "Use the House/Senate Prev/Next buttons if there are many candidates. Submit when ready."
    )
    em = discord.Embed(title="🗳️ Official Ballot", description=description, timestamp=now_utc())
    em.set_footer(text="Federal Elections Commission • Ballot Submission")

    await interaction.followup.send(
        embed=em,
        view=VoteView(
            election_id=election_id,
            include_house=election["include_house"],
            include_senate=election["include_senate"],
            include_pres=election["include_pres"],
            house_opts=house_opts,
            senate_opts=senate_opts,
            pres_opts=pres_opts,
        ),
        ephemeral=True
    )


# =========================
# BALLOT REVIEW (FEC)
# =========================
class RejectModal(discord.ui.Modal, title="Reject Ballot"):
    reason = discord.ui.TextInput(
        label="Rejection reason",
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=500
    )

    def __init__(self, ballot_id: int, election_id: str):
        super().__init__()
        self.ballot_id = ballot_id
        self.election_id = election_id

    async def on_submit(self, interaction: discord.Interaction):
        member = await require_fec(interaction)
        if member is None:
            return

        await interaction.response.defer(ephemeral=True)

        pool = await db()
        await pool.execute(
            """
            UPDATE ballots
            SET status='REJECTED', reviewed_by=$2, reviewed_at=NOW(), reject_reason=$3
            WHERE ballot_id=$1 AND status='PENDING'
            """,
            self.ballot_id,
            interaction.user.id,
            str(self.reason)
        )

        await interaction.followup.send(f"❌ Ballot `{self.ballot_id}` rejected.", ephemeral=True)

        if interaction.guild:
            await post_or_edit_auto_update(interaction.guild, self.election_id)


class ReviewView(discord.ui.View):
    def __init__(self, ballot_id: int, election_id: str):
        super().__init__(timeout=300)
        self.ballot_id = ballot_id
        self.election_id = election_id

    @discord.ui.button(label="ACCEPT", style=discord.ButtonStyle.green)
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        member = await require_fec(interaction)
        if member is None:
            return

        await interaction.response.defer(ephemeral=True)

        pool = await db()
        await pool.execute(
            """
            UPDATE ballots
            SET status='ACCEPTED', reviewed_by=$2, reviewed_at=NOW(), reject_reason=NULL
            WHERE ballot_id=$1 AND status='PENDING'
            """,
            self.ballot_id,
            interaction.user.id
        )

        await interaction.followup.send(f"✅ Ballot `{self.ballot_id}` accepted.", ephemeral=True)
        self.stop()

        if interaction.guild:
            await post_or_edit_auto_update(interaction.guild, self.election_id)

    @discord.ui.button(label="REJECT", style=discord.ButtonStyle.danger)
    async def reject(self, interaction: discord.Interaction, button: discord.ui.Button):
        member = await require_fec(interaction)
        if member is None:
            return
        await interaction.response.send_modal(RejectModal(self.ballot_id, self.election_id))
        self.stop()


@bot.tree.command(name="ballots_next", description="(FEC) Pull the next pending ballot for review.")
@app_commands.describe(election_id="Election ID (e.g. January26)")
async def ballots_next(interaction: discord.Interaction, election_id: str):
    member = await require_fec(interaction)
    if member is None:
        return

    pool = await db()
    row = await pool.fetchrow(
        """
        SELECT b.*, e.include_house, e.include_senate, e.include_pres
        FROM ballots b
        JOIN elections e ON e.election_id=b.election_id
        WHERE b.election_id=$1 AND b.status='PENDING'
        ORDER BY b.submitted_at ASC
        LIMIT 1
        """,
        election_id
    )
    if not row:
        await interaction.response.send_message("✅ No pending ballots.", ephemeral=True)
        return

    ballot_id = row["ballot_id"]

    async def names_for(ids: Sequence[int]) -> str:
        if not ids:
            return "—"
        recs = await pool.fetch(
            "SELECT candidate_id, office, rp_name, party, state, district FROM candidates WHERE candidate_id = ANY($1::int[])",
            list(ids)
        )
        by_id = {r["candidate_id"]: r for r in recs}
        lines: list[str] = []
        for cid in ids:
            r = by_id.get(cid)
            if r:
                lines.append(candidate_label(r["office"], r["rp_name"], r["party"], r["state"], r["district"]))
            else:
                lines.append(f"Unknown Candidate ID {cid}")
        return "\n".join(lines)

    async def name_for_one(cid: Optional[int]) -> str:
        if not cid:
            return "—"
        r = await pool.fetchrow(
            "SELECT office, rp_name, party, state, district FROM candidates WHERE candidate_id=$1",
            cid
        )
        if not r:
            return f"Unknown Candidate ID {cid}"
        return candidate_label(r["office"], r["rp_name"], r["party"], r["state"], r["district"])

    house_text = await names_for(row["house_choices"])
    senate_text = await names_for(row["senate_choices"])
    pres_text = await name_for_one(row["pres_choice"])

    embed = discord.Embed(
        title="🧾 Ballot Review",
        description=f"Election: `{election_id}`\nBallot ID: `{ballot_id}`\nStatus: **PENDING**",
        timestamp=row["submitted_at"]
    )
    embed.add_field(name="Discord Username & ID", value=f"{row['voter_username']} (`{row['voter_id']}`)", inline=False)
    embed.add_field(name="Server Nickname", value=row["voter_nickname"] or "N/A", inline=False)

    if row["include_house"]:
        embed.add_field(name="Ballot: House (up to 3)", value=house_text, inline=False)
    if row["include_senate"]:
        embed.add_field(name="Ballot: Senate (up to 2)", value=senate_text, inline=False)
    if row["include_pres"]:
        embed.add_field(name="Ballot: President (1)", value=pres_text, inline=False)

    await interaction.response.send_message(embed=embed, view=ReviewView(ballot_id, election_id), ephemeral=True)


# =========================
# CERTIFICATION WORKFLOW
# =========================
async def snapshot_office(pool: asyncpg.Pool, election_id: str, office: str) -> Dict:
    res, total = await office_results(pool, election_id, office)
    return {
        "office": office,
        "total_votes": total,
        "results": [{"label": l, "votes": v, "pct": round(p, 2)} for (l, v, p) in res]
    }


@bot.tree.command(name="election_certify", description="(FEC) Certify an election (locks results + stores snapshot).")
@app_commands.describe(election_id="Election ID", notes="Optional certification notes")
async def election_certify(interaction: discord.Interaction, election_id: str, notes: Optional[str] = None):
    member = await require_fec(interaction)
    if member is None:
        return
    await interaction.response.defer(ephemeral=True)

    pool = await db()
    election = await fetch_election(pool, election_id)
    if not election:
        await interaction.followup.send(f"❌ Election `{election_id}` not found.", ephemeral=True)
        return

    status = (election["status"] or "").upper()
    if status != "CLOSED":
        await interaction.followup.send("❌ You can only certify an election after polls are CLOSED.", ephemeral=True)
        return

    submitted, accepted, pct = await reporting_stats(pool, election_id)

    house_snap = await snapshot_office(pool, election_id, "HOUSE") if election["include_house"] else None
    senate_snap = await snapshot_office(pool, election_id, "SENATE") if election["include_senate"] else None
    pres_snap = await snapshot_office(pool, election_id, "PRESIDENT") if election["include_pres"] else None

    await pool.execute(
        """
        INSERT INTO election_certifications
            (election_id, certified_by, submitted_ballots, accepted_ballots, house_snapshot, senate_snapshot, pres_snapshot, notes)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
        ON CONFLICT (election_id)
        DO UPDATE SET
            certified_by=EXCLUDED.certified_by,
            certified_at=NOW(),
            submitted_ballots=EXCLUDED.submitted_ballots,
            accepted_ballots=EXCLUDED.accepted_ballots,
            house_snapshot=EXCLUDED.house_snapshot,
            senate_snapshot=EXCLUDED.senate_snapshot,
            pres_snapshot=EXCLUDED.pres_snapshot,
            notes=EXCLUDED.notes
        """,
        election_id,
        interaction.user.id,
        submitted,
        accepted,
        house_snap,
        senate_snap,
        pres_snap,
        notes
    )

    await pool.execute("UPDATE elections SET status='CERTIFIED' WHERE election_id=$1", election_id)

    if interaction.guild:
        await post_or_edit_auto_update(interaction.guild, election_id)

    em = discord.Embed(
        title="✅ Election Certified",
        description=(
            f"**Election:** `{election_id}`\n"
            f"**Certified by:** <@{interaction.user.id}>\n"
            f"**Ballots:** Accepted **{accepted}** / Submitted **{submitted}**\n"
            f"**Reporting:** {pct:.1f}%\n\n"
            f"Status is now **CERTIFIED**. Candidates & ballots are locked."
        ),
        timestamp=now_utc()
    )
    if notes:
        em.add_field(name="Notes", value=notes[:1024], inline=False)
    await interaction.followup.send(embed=em, ephemeral=True)


@bot.tree.command(name="election_uncertify", description="(FEC) Revert CERTIFIED back to CLOSED (admin-only).")
@app_commands.describe(election_id="Election ID", reason="Reason for reverting certification")
async def election_uncertify(interaction: discord.Interaction, election_id: str, reason: str):
    member = await require_fec(interaction)
    if member is None:
        return
    await interaction.response.defer(ephemeral=True)

    pool = await db()
    election = await fetch_election(pool, election_id)
    if not election:
        await interaction.followup.send(f"❌ Election `{election_id}` not found.", ephemeral=True)
        return

    if (election["status"] or "").upper() != "CERTIFIED":
        await interaction.followup.send("❌ Election is not CERTIFIED.", ephemeral=True)
        return

    await pool.execute("UPDATE elections SET status='CLOSED' WHERE election_id=$1", election_id)

    if interaction.guild:
        await safe_log_event(
            interaction.guild,
            f"⚠️ CERTIFICATION REVERTED: `{election_id}` by <@{interaction.user.id}> — {reason}"
        )

    await interaction.followup.send(
        f"⚠️ `{election_id}` reverted to CLOSED. Reason logged.",
        ephemeral=True
    )


# =========================
# REPORTING + FULL RESULTS
# =========================
@bot.tree.command(name="election_report", description="Show reporting % and leaders for an election.")
@app_commands.describe(election_id="Election ID (e.g. January26)")
async def election_report(interaction: discord.Interaction, election_id: str):
    pool = await db()
    election = await fetch_election(pool, election_id)
    if not election:
        await interaction.response.send_message(f"❌ Election `{election_id}` not found.", ephemeral=True)
        return

    em = await build_results_embed(pool, election)
    await interaction.response.send_message(embed=em, ephemeral=True)


@bot.tree.command(name="election_results", description="Show full results (vote counts + %).")
@app_commands.describe(election_id="Election ID (e.g. January26)")
async def election_results(interaction: discord.Interaction, election_id: str):
    pool = await db()
    election = await fetch_election(pool, election_id)
    if not election:
        await interaction.response.send_message(f"❌ Election `{election_id}` not found.", ephemeral=True)
        return

    submitted, accepted, pct = await reporting_stats(pool, election_id)

    lines: list[str] = []
    lines.append(f"📊 **Full Results — `{election_id}`**")
    lines.append(
        f"Status: **{election_status_badge(election['status'])}** | "
        f"Reporting: {bar(pct)} **{pct:.1f}%** (Accepted {accepted} / Submitted {submitted})"
    )

    if election["include_house"]:
        res, _ = await office_results(pool, election_id, "HOUSE")
        lines.append("")
        lines.append("**🏛️ House (Full)**")
        if not res:
            lines.append("— No accepted votes tallied yet.")
        else:
            for label, votes, p in res:
                lines.append(f"- {label}: **{votes}** ({p:.1f}%)")

    if election["include_senate"]:
        res, _ = await office_results(pool, election_id, "SENATE")
        lines.append("")
        lines.append("**🏛️ Senate (Full)**")
        if not res:
            lines.append("— No accepted votes tallied yet.")
        else:
            for label, votes, p in res:
                lines.append(f"- {label}: **{votes}** ({p:.1f}%)")

    if election["include_pres"]:
        res, _ = await office_results(pool, election_id, "PRESIDENT")
        lines.append("")
        lines.append("**🇺🇸 President (Full)**")
        if not res:
            lines.append("— No accepted votes tallied yet.")
        else:
            for label, votes, p in res:
                lines.append(f"- {label}: **{votes}** ({p:.1f}%)")

    parts = chunk_lines(lines)
    if len(parts) == 1:
        await interaction.response.send_message(parts[0], ephemeral=True)
    else:
        await interaction.response.send_message(f"{parts[0]}\n\n*(Part 1/{len(parts)})*", ephemeral=True)
        for i, part in enumerate(parts[1:], start=2):
            await interaction.followup.send(f"{part}\n\n*(Part {i}/{len(parts)})*", ephemeral=True)


# =========================
# BASIC PING
# =========================
@bot.tree.command(name="ping", description="Check if the bot is online.")
async def ping(interaction: discord.Interaction):
    if interaction.response.is_done():
        await interaction.followup.send("✅ Online.", ephemeral=True)
    else:
        await interaction.response.send_message("✅ Online.", ephemeral=True)


# =========================
# RUN (SAFE BACKOFF)
# =========================
import time

LOGIN_BACKOFF_START = 30
LOGIN_BACKOFF_CAP = 20 * 60  # 20 minutes

if __name__ == "__main__":
    token = (os.getenv("DISCORD_TOKEN") or "").strip()
    if not token:
        raise RuntimeError("DISCORD_TOKEN env var is missing/empty.")

    delay = LOGIN_BACKOFF_START

    while True:
        try:
            print("Starting bot... token length:", len(token))
            bot.run(token)
            # If bot.run returns normally, break (usually it doesn't unless closed)
            break

        except discord.HTTPException as e:
            status = getattr(e, "status", None)

            # 429 = Discord rate limit
            if status == 429:
                sleep_for = delay + random.randint(0, 15)
                print(f"⚠️ Rate limited (429). Sleeping {sleep_for}s then retrying.")
                time.sleep(sleep_for)
                delay = min(delay * 2, LOGIN_BACKOFF_CAP)
                continue

            print(f"❌ HTTPException (status={status}): {repr(e)}")
            time.sleep(60)

        except Exception as e:
            print(f"🔥 Fatal exception: {repr(e)}")
            time.sleep(60)
