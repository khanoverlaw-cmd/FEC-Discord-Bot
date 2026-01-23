# bot.py
import os
import random
import traceback
from datetime import datetime, UTC
from typing import Optional, Sequence

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

RESULTS_CHANNEL_NAME = "election-results"  # auto updates go here by channel name

ALLOWED_ANNOUNCE_CHANNELS = {"fec-announcements", "election-results", "fec-public-records"}

# Custom emoji tag for header (replace ID with your real emoji id)
HEADER_PREFIX = "<:FEC:123456789012345678>"

HOUSE_MAX_PICKS = 3
SENATE_MAX_PICKS = 2

MAX_SELECT_OPTIONS = 25
DISCORD_MESSAGE_LIMIT = 2000

# =========================
# HELPERS
# =========================
def make_case_reference() -> str:
    now = datetime.now(UTC)
    yy = now.strftime("%y")
    mmdd = now.strftime("%m%d")
    rand4 = random.randint(1000, 9999)
    return f"FEC-{yy}-{mmdd}-{rand4}"


def fmt_header(title: str) -> str:
    return f"# {HEADER_PREFIX} | {title}"


def normalize_message_text(text: str) -> str:
    """
    Bulletproof paragraph breaks:
    - If Discord strips Shift+Enter newlines, user can type \\n or \\n\\n.
    - We convert literal sequences into real newlines.
    """
    return text.replace("\\n", "\n")


def has_role_id(member: discord.Member, role_id: int) -> bool:
    return any(r.id == role_id for r in member.roles)


def unauthorized_notice(case_ref: str) -> str:
    return (
        "⚠️ **FEC NOTICE — UNAUTHORIZED ELECTION ACTIVITY**\n\n"
        "Pursuant to **FEC Administrative Code §1.04(b)**, you are not authorized to issue "
        "official election communications.\n\n"
        f"**Case Reference:** `{case_ref}`\n\n"
        "This action has been logged. Continued attempts may result in administrative review."
    )


def bar(pct: float, width: int = 18) -> str:
    pct = max(0.0, min(100.0, pct))
    filled = int(round((pct / 100.0) * width))
    return "█" * filled + "░" * (width - filled)


def normalize_db_url(url: str) -> str:
    # asyncpg prefers postgresql://
    if url.startswith("postgres://"):
        return "postgresql://" + url[len("postgres://") :]
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


async def safe_log_event(guild: discord.Guild, text: str) -> None:
    try:
        ch = discord.utils.get(guild.text_channels, name=LOG_CHANNEL_NAME)
        if ch:
            await ch.send(text)
    except Exception:
        pass


# =========================
# DISCORD SETUP
# =========================
intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

DB_POOL: Optional[asyncpg.Pool] = None


async def db() -> asyncpg.Pool:
    if DB_POOL is None:
        raise RuntimeError("DB pool not initialized.")
    return DB_POOL


async def ensure_schema(pool: asyncpg.Pool) -> None:
    await pool.execute(
        """
        CREATE TABLE IF NOT EXISTS elections (
            election_id TEXT PRIMARY KEY,
            election_type TEXT NOT NULL,      -- SPECIAL / GENERAL / MIDTERMS
            include_house BOOLEAN NOT NULL,
            include_senate BOOLEAN NOT NULL,
            include_pres BOOLEAN NOT NULL,
            status TEXT NOT NULL DEFAULT 'DRAFT',  -- DRAFT / OPEN / CLOSED
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );

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
        """
    )


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")


async def init_db_and_sync():
    global DB_POOL
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL env var is missing (Render Postgres).")

    DB_POOL = await asyncpg.create_pool(
        dsn=normalize_db_url(database_url),
        min_size=1,
        max_size=5,
    )
    await ensure_schema(DB_POOL)

    # Fast guild sync to avoid "command is outdated"
    guild_obj = discord.Object(id=DEV_GUILD_ID)
    bot.tree.copy_global_to(guild=guild_obj)
    synced = await bot.tree.sync(guild=guild_obj)
    print(f"✅ Synced {len(synced)} commands to DEV guild {DEV_GUILD_ID}.")


@bot.event
async def setup_hook():
    await init_db_and_sync()


# =========================
# PERMISSION HELPERS
# =========================
async def require_fec(interaction: discord.Interaction) -> Optional[discord.Member]:
    if interaction.guild is None:
        await interaction.response.send_message("❌ Server-only.", ephemeral=True)
        return None
    member = interaction.user
    if not isinstance(member, discord.Member):
        member = interaction.guild.get_member(interaction.user.id)
    if member is None or not has_role_id(member, FEC_ROLE_ID):
        await interaction.response.send_message("❌ FEC only.", ephemeral=True)
        return None
    return member


async def require_voter(interaction: discord.Interaction) -> Optional[discord.Member]:
    if interaction.guild is None:
        await interaction.response.send_message("❌ Server-only.", ephemeral=True)
        return None
    member = interaction.user
    if not isinstance(member, discord.Member):
        member = interaction.guild.get_member(interaction.user.id)
    if member is None or not has_role_id(member, AMERICAN_CITIZEN_ROLE_ID):
        await interaction.response.send_message(
            "❌ You are not eligible to vote (American Citizen role required).",
            ephemeral=True
        )
        return None
    return member


async def fetch_election(pool: asyncpg.Pool, election_id: str):
    return await pool.fetchrow("SELECT * FROM elections WHERE election_id=$1", election_id)


async def get_results_channel(guild: discord.Guild) -> Optional[discord.TextChannel]:
    ch = discord.utils.get(guild.text_channels, name=RESULTS_CHANNEL_NAME)
    return ch if isinstance(ch, discord.TextChannel) else None


# =========================
# TABULATION / RESULTS
# =========================
async def reporting_stats(pool: asyncpg.Pool, election_id: str) -> tuple[int, int, float]:
    submitted = await pool.fetchval("SELECT COUNT(*) FROM ballots WHERE election_id=$1", election_id) or 0
    accepted = await pool.fetchval(
        "SELECT COUNT(*) FROM ballots WHERE election_id=$1 AND status='ACCEPTED'",
        election_id
    ) or 0
    pct = (accepted / submitted * 100.0) if submitted else 0.0
    return submitted, accepted, pct


async def office_results(pool: asyncpg.Pool, election_id: str, office: str) -> tuple[list[tuple[str, int, float]], int]:
    """
    Returns ([(label, votes, pct_of_office_total), ...], total_votes)
    For HOUSE/SENATE: each at-large selection counts as one vote.
    """
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
        out.append((label, r["votes"], pct))
    return out, total


async def post_auto_update(guild: discord.Guild, election_id: str) -> None:
    pool = await db()
    election = await fetch_election(pool, election_id)
    if not election:
        return

    ch = await get_results_channel(guild)
    if not ch:
        return

    submitted, accepted, pct = await reporting_stats(pool, election_id)

    lines: list[str] = []
    lines.append(f"🗳️ **Election Night Update — `{election_id}`**")
    lines.append(f"Reporting: {bar(pct)} **{pct:.1f}%**  (Accepted {accepted} / Submitted {submitted})")

    if election["include_house"]:
        res, _ = await office_results(pool, election_id, "HOUSE")
        if res:
            lines.append("")
            lines.append("**House (Top 3)**")
            for label, votes, p in res[:3]:
                lines.append(f"- {label}: **{votes}** ({p:.1f}%)")

    if election["include_senate"]:
        res, _ = await office_results(pool, election_id, "SENATE")
        if res:
            lines.append("")
            lines.append("**Senate (Top 3)**")
            for label, votes, p in res[:3]:
                lines.append(f"- {label}: **{votes}** ({p:.1f}%)")

    await ch.send("\n".join(lines))


# =========================
# ANNOUNCEMENTS UI
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
        if perms.view_channel and perms.send_messages:
            eligible.append(ch)
    return eligible


class AnnounceChannelSelect(discord.ui.Select):
    def __init__(self, title: str, message: str, channels: list[discord.TextChannel]):
        options = [
            discord.SelectOption(label=f"#{ch.name}", value=str(ch.id), description="Approved FEC channel")
            for ch in channels[:25]
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
                await interaction.response.send_message("❌ Server-only.", ephemeral=True)
                return

            channel_id = int(self.values[0])
            chosen_channel = interaction.guild.get_channel(channel_id)
            if not isinstance(chosen_channel, discord.TextChannel):
                await interaction.response.send_message("❌ Invalid channel.", ephemeral=True)
                return

            if chosen_channel.name not in ALLOWED_ANNOUNCE_CHANNELS:
                case_ref = make_case_reference()
                await interaction.response.send_message(f"❌ Channel not authorized. Case `{case_ref}`", ephemeral=True)
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
# ELECTION CHOICES (Dyno-like dropdowns)
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

STATES = [
    app_commands.Choice(name=s, value=s) for s in [
        "AL","AK","AZ","AR","CA","CO","CT","DE","FL","GA",
        "HI","ID","IL","IN","IA","KS","KY","LA","ME","MD",
        "MA","MI","MN","MS","MO","MT","NE","NV","NH","NJ",
        "NM","NY","NC","ND","OH","OK","OR","PA","RI","SC",
        "SD","TN","TX","UT","VT","VA","WA","WV","WI","WY"
    ]
]


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
        await interaction.response.send_message(
            f"✅ Election `{election_id}` created as **{election_type.name}** (status: DRAFT).\n"
            "Next: add candidates with `/add_candidate`, then `/election_open`.",
            ephemeral=True
        )
    except asyncpg.UniqueViolationError:
        await interaction.response.send_message(f"❌ Election `{election_id}` already exists.", ephemeral=True)


@bot.tree.command(name="add_candidate", description="(FEC) Add a candidate to an election.")
@app_commands.choices(office=OFFICES, party=PARTIES, state=STATES)
@app_commands.describe(
    election_id="Election ID (e.g. January26)",
    office="House / Senate / President",
    rp_name="Candidate RP name",
    party="Party",
    state="State (required for House/Senate)",
    district="House district (required ONLY for House)"
)
async def add_candidate(
    interaction: discord.Interaction,
    election_id: str,
    office: app_commands.Choice[str],
    rp_name: str,
    party: app_commands.Choice[str],
    state: Optional[app_commands.Choice[str]] = None,
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

    off = office.value
    party_val = party.value
    state_val = state.value if state else None

    # Enforce state for House/Senate
    if off in ("HOUSE", "SENATE") and not state_val:
        await interaction.response.send_message("❌ State is required for House/Senate candidates.", ephemeral=True)
        return

    # Enforce district only for House; ignore for others
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
        await interaction.response.send_message(
            f"✅ Added **{candidate_label(off, rp_name, party_val, state_val, district)}** "
            f"(ID `{row['candidate_id']}`) to `{election_id}`.",
            ephemeral=True
        )
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
    await pool.execute("UPDATE elections SET status='OPEN' WHERE election_id=$1", election_id)
    await interaction.response.send_message(f"🟢 Polls OPEN for `{election_id}`.", ephemeral=True)


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
    await pool.execute("UPDATE elections SET status='CLOSED' WHERE election_id=$1", election_id)
    await interaction.response.send_message(f"🔴 Polls CLOSED for `{election_id}`.", ephemeral=True)


# =========================
# VOTING UI (Dropdowns)
# =========================
class PagedMultiSelect(discord.ui.Select):
    """
    Supports >25 options via paging.
    With your current 11/17 candidates, it will just be one page.
    """
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
        self.page = max(0, page)
        page_opts = self._page_options()
        self.options = page_opts
        self.max_values = min(self.max_picks, len(page_opts)) if page_opts else 0

    def total_pages(self) -> int:
        if not self._all_options:
            return 1
        return ((len(self._all_options) - 1) // MAX_SELECT_OPTIONS) + 1

    async def callback(self, interaction: discord.Interaction):
        await self.on_change(interaction, [int(v) for v in self.values])


class VoteView(discord.ui.View):
    def __init__(
        self,
        election_id: str,
        include_house: bool,
        include_senate: bool,
        house_opts: list[discord.SelectOption],
        senate_opts: list[discord.SelectOption],
    ):
        super().__init__(timeout=300)
        self.election_id = election_id
        self.house_selected: list[int] = []
        self.senate_selected: list[int] = []

        self.house_select: Optional[PagedMultiSelect] = None
        self.senate_select: Optional[PagedMultiSelect] = None

        if include_house:
            async def on_house_change(interaction, values):
                uniq = []
                for v in values:
                    if v not in uniq:
                        uniq.append(v)
                self.house_selected = uniq[:HOUSE_MAX_PICKS]
                await interaction.response.send_message("✅ House selections updated.", ephemeral=True)

            self.house_select = PagedMultiSelect(
                placeholder=f"House (pick up to {HOUSE_MAX_PICKS})",
                max_picks=HOUSE_MAX_PICKS,
                options=house_opts,
                on_change=on_house_change,
            )
            self.add_item(self.house_select)

        if include_senate:
            async def on_senate_change(interaction, values):
                uniq = []
                for v in values:
                    if v not in uniq:
                        uniq.append(v)
                self.senate_selected = uniq[:SENATE_MAX_PICKS]
                await interaction.response.send_message("✅ Senate selections updated.", ephemeral=True)

            self.senate_select = PagedMultiSelect(
                placeholder=f"Senate (pick up to {SENATE_MAX_PICKS})",
                max_picks=SENATE_MAX_PICKS,
                options=senate_opts,
                on_change=on_senate_change,
            )
            self.add_item(self.senate_select)

        self.add_item(self.PageButton(self))
        self.add_item(self.SubmitButton(self))

    class PageButton(discord.ui.Button):
        def __init__(self, parent: "VoteView"):
            super().__init__(label="Next Page", style=discord.ButtonStyle.secondary)
            self.parent = parent

        async def callback(self, interaction: discord.Interaction):
            target = None
            if self.parent.house_select and self.parent.house_select.total_pages() > 1:
                target = self.parent.house_select
            elif self.parent.senate_select and self.parent.senate_select.total_pages() > 1:
                target = self.parent.senate_select

            if not target:
                await interaction.response.send_message("No additional pages.", ephemeral=True)
                return

            next_page = (target.page + 1) % target.total_pages()
            target.set_page(next_page)

            await interaction.response.edit_message(
                content=f"🗳️ **Ballot for `{self.parent.election_id}`**\n"
                        f"House: up to {HOUSE_MAX_PICKS} | Senate: up to {SENATE_MAX_PICKS}\n"
                        f"(Page {next_page + 1}/{target.total_pages()})",
                view=self.parent
            )

    class SubmitButton(discord.ui.Button):
        def __init__(self, parent: "VoteView"):
            super().__init__(label="Submit Ballot", style=discord.ButtonStyle.green)
            self.parent = parent

        async def callback(self, interaction: discord.Interaction):
            voter = await require_voter(interaction)
            if voter is None:
                return

            pool = await db()
            election = await fetch_election(pool, self.parent.election_id)
            if not election:
                await interaction.response.send_message("❌ Election not found.", ephemeral=True)
                return
            if election["status"] != "OPEN":
                await interaction.response.send_message("❌ Polls are not open for this election.", ephemeral=True)
                return

            if election["include_house"] and not self.parent.house_selected and \
               election["include_senate"] and not self.parent.senate_selected:
                await interaction.response.send_message("❌ Your ballot is empty. Select at least one candidate.", ephemeral=True)
                return

            voter_username = str(interaction.user)
            voter_nick = voter.display_name if isinstance(voter, discord.Member) else None

            try:
                row = await pool.fetchrow(
                    """
                    INSERT INTO ballots (election_id, voter_id, voter_username, voter_nickname,
                                        house_choices, senate_choices, status)
                    VALUES ($1,$2,$3,$4,$5,$6,'PENDING')
                    RETURNING ballot_id
                    """,
                    self.parent.election_id,
                    interaction.user.id,
                    voter_username,
                    voter_nick,
                    self.parent.house_selected,
                    self.parent.senate_selected,
                )
            except asyncpg.UniqueViolationError:
                await interaction.response.send_message("❌ You have already submitted a ballot for this election.", ephemeral=True)
                return

            ballot_id = row["ballot_id"]

            if interaction.guild:
                ch = interaction.guild.get_channel(BALLOT_CHANNEL_ID)
                if isinstance(ch, discord.TextChannel):
                    embed = discord.Embed(
                        title="🗳️ Ballot Submitted (PENDING)",
                        description=f"Election: `{self.parent.election_id}`\nBallot ID: `{ballot_id}`",
                        timestamp=datetime.now(UTC)
                    )
                    embed.add_field(name="Discord Username & ID", value=f"{interaction.user} (`{interaction.user.id}`)", inline=False)
                    embed.add_field(name="Server Nickname", value=voter_nick or "N/A", inline=False)
                    embed.set_footer(text="Use /ballots_next to review and accept/reject ballots.")
                    await ch.send(embed=embed)

            await interaction.response.send_message(
                f"✅ Thank you for casting your ballot, {interaction.user.mention}.\n"
                "Your submission has been received and will be processed by election administration.",
                ephemeral=True
            )
            self.parent.stop()


@bot.tree.command(name="vote", description="Cast your ballot (American Citizen role required).")
@app_commands.describe(election_id="Election ID (e.g. January26)")
async def vote(interaction: discord.Interaction, election_id: str):
    voter = await require_voter(interaction)
    if voter is None:
        return

    pool = await db()
    election = await fetch_election(pool, election_id)
    if not election:
        await interaction.response.send_message(f"❌ Election `{election_id}` not found.", ephemeral=True)
        return
    if election["status"] != "OPEN":
        await interaction.response.send_message(f"❌ Polls are not open for `{election_id}`.", ephemeral=True)
        return

    rows = await pool.fetch(
        "SELECT * FROM candidates WHERE election_id=$1 ORDER BY office, state NULLS LAST, district NULLS LAST, rp_name",
        election_id
    )

    house_opts: list[discord.SelectOption] = []
    senate_opts: list[discord.SelectOption] = []

    for r in rows:
        label = candidate_label(r["office"], r["rp_name"], r["party"], r["state"], r["district"])
        opt = discord.SelectOption(label=label[:100], value=str(r["candidate_id"]))
        if r["office"] == "HOUSE":
            house_opts.append(opt)
        elif r["office"] == "SENATE":
            senate_opts.append(opt)

    if election["include_house"] and not house_opts:
        await interaction.response.send_message("❌ This election includes House voting but has no House candidates.", ephemeral=True)
        return
    if election["include_senate"] and not senate_opts:
        await interaction.response.send_message("❌ This election includes Senate voting but has no Senate candidates.", ephemeral=True)
        return

    await interaction.response.send_message(
        f"🗳️ **Ballot for `{election_id}`**\n"
        f"House: up to {HOUSE_MAX_PICKS} | Senate: up to {SENATE_MAX_PICKS}\n"
        "Submit when ready.",
        view=VoteView(
            election_id=election_id,
            include_house=election["include_house"],
            include_senate=election["include_senate"],
            house_opts=house_opts,
            senate_opts=senate_opts,
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
        await interaction.response.send_message(f"❌ Ballot `{self.ballot_id}` rejected.", ephemeral=True)

        if interaction.guild:
            await post_auto_update(interaction.guild, self.election_id)


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
        await interaction.response.send_message(f"✅ Ballot `{self.ballot_id}` accepted.", ephemeral=True)
        self.stop()

        if interaction.guild:
            await post_auto_update(interaction.guild, self.election_id)

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
        SELECT b.*, e.include_house, e.include_senate
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
        lines = []
        for cid in ids:
            r = by_id.get(cid)
            if r:
                lines.append(candidate_label(r["office"], r["rp_name"], r["party"], r["state"], r["district"]))
            else:
                lines.append(f"Unknown Candidate ID {cid}")
        return "\n".join(lines)

    house_text = await names_for(row["house_choices"])
    senate_text = await names_for(row["senate_choices"])

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

    await interaction.response.send_message(embed=embed, view=ReviewView(ballot_id, election_id), ephemeral=True)


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

    submitted, accepted, pct = await reporting_stats(pool, election_id)

    lines: list[str] = []
    lines.append(f"**Election:** `{election_id}` ({election['election_type']}) — Status: **{election['status']}**")
    lines.append(f"**Reporting:** {bar(pct)} **{pct:.1f}%**  (Accepted {accepted} / Submitted {submitted})")

    if election["include_house"]:
        res, _ = await office_results(pool, election_id, "HOUSE")
        lines.append("\n**House (Top 3)**")
        if not res:
            lines.append("— No accepted votes tallied yet.")
        else:
            for label, votes, p in res[:3]:
                lines.append(f"- {label}: **{votes}** ({p:.1f}%)")

    if election["include_senate"]:
        res, _ = await office_results(pool, election_id, "SENATE")
        lines.append("\n**Senate (Top 3)**")
        if not res:
            lines.append("— No accepted votes tallied yet.")
        else:
            for label, votes, p in res[:3]:
                lines.append(f"- {label}: **{votes}** ({p:.1f}%)")

    await interaction.response.send_message("\n".join(lines), ephemeral=True)


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
    lines.append(f"Status: **{election['status']}** | Reporting: {bar(pct)} **{pct:.1f}%** (Accepted {accepted} / Submitted {submitted})")

    if election["include_house"]:
        res, _ = await office_results(pool, election_id, "HOUSE")
        lines.append("")
        lines.append("**House (Full)**")
        if not res:
            lines.append("— No accepted votes tallied yet.")
        else:
            for label, votes, p in res:
                lines.append(f"- {label}: **{votes}** ({p:.1f}%)")

    if election["include_senate"]:
        res, _ = await office_results(pool, election_id, "SENATE")
        lines.append("")
        lines.append("**Senate (Full)**")
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
# RUN (safe retry / backoff)
# =========================
import time
import discord

if __name__ == "__main__":
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise RuntimeError("DISCORD_TOKEN env var is missing. Add it in Render → Environment.")

    backoff = 30          # start with 30s
    max_backoff = 15 * 60 # cap at 15 min

    while True:
        try:
            bot.run(token)
            backoff = 30  # reset if it ever exits cleanly (rare)
        except discord.HTTPException as e:
            # When Discord/Cloudflare rate limits login, prevent restart spam
            if getattr(e, "status", None) == 429:
                print(f"⚠️ Discord login rate-limited (429). Sleeping {backoff}s then retrying...")
                time.sleep(backoff)
                backoff = min(backoff * 2, max_backoff)
                continue
            raise
        except Exception as e:
            print(f"❌ Fatal error: {e}")
            raise
