import os
import asyncio
import asyncpg
import discord
from discord import app_commands
from discord.ext import commands, tasks
from datetime import datetime, timedelta
import pytz
from dotenv import load_dotenv
import logging

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

TOKEN = os.getenv("DISCORD_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
GUILD_ID = int(os.getenv("GUILD_ID", "0"))
TZ = pytz.timezone("Europe/Warsaw")

# ─── Bot setup ───────────────────────────────────────────────────────────────

intents = discord.Intents.default()
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)
db: asyncpg.Pool = None

REPEAT_OPTIONS = {
    "co_godzine":    "Co godzinę",
    "co_dzien":      "Co dzień",
    "co_tydzien":    "Co tydzień",
    "co_2_tygodnie": "Co 2 tygodnie",
    "co_miesiac":    "Co miesiąc",
    "jednorazowe":   "Jednorazowe",
}

REMIND_OPTIONS = {
    15:   "15 minut przed",
    30:   "30 minut przed",
    60:   "1 godzinę przed",
    120:  "2 godziny przed",
    1440: "1 dzień przed",
}

# ─── Database ────────────────────────────────────────────────────────────────

async def init_db():
    global db
    db = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=10)
    async with db.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS events (
                id           SERIAL PRIMARY KEY,
                guild_id     BIGINT NOT NULL,
                channel_id   BIGINT NOT NULL,
                name         TEXT NOT NULL,
                description  TEXT,
                next_run     TIMESTAMPTZ NOT NULL,
                repeat_type  TEXT NOT NULL,
                remind_min   INT NOT NULL DEFAULT 30,
                created_by   BIGINT NOT NULL,
                created_at   TIMESTAMPTZ DEFAULT NOW(),
                reminded     BOOLEAN DEFAULT FALSE,
                UNIQUE(guild_id, name)
            );

            CREATE TABLE IF NOT EXISTS subscriptions (
                event_id  INT    NOT NULL REFERENCES events(id) ON DELETE CASCADE,
                user_id   BIGINT NOT NULL,
                PRIMARY KEY (event_id, user_id)
            );

            CREATE TABLE IF NOT EXISTS panel_messages (
                guild_id    BIGINT PRIMARY KEY,
                channel_id  BIGINT NOT NULL,
                message_id  BIGINT NOT NULL
            );
        """)
    log.info("Baza danych gotowa.")


# ─── Helpers ─────────────────────────────────────────────────────────────────

def next_run_after(dt: datetime, repeat_type: str) -> datetime | None:
    if repeat_type == "jednorazowe":
        return None
    elif repeat_type == "co_godzine":
        return dt + timedelta(hours=1)
    elif repeat_type == "co_dzien":
        return dt + timedelta(days=1)
    elif repeat_type == "co_tydzien":
        return dt + timedelta(weeks=1)
    elif repeat_type == "co_2_tygodnie":
        return dt + timedelta(weeks=2)
    elif repeat_type == "co_miesiac":
        # Dodaj miesiąc
        month = dt.month + 1
        year = dt.year + (month - 1) // 12
        month = ((month - 1) % 12) + 1
        try:
            return dt.replace(year=year, month=month)
        except ValueError:
            return dt.replace(year=year, month=month, day=28)
    return None


def format_countdown(dt: datetime) -> str:
    now = datetime.now(TZ)
    diff = dt.astimezone(TZ) - now
    if diff.total_seconds() <= 0:
        return "właśnie teraz!"
    
    days = diff.days
    hours, remainder = divmod(diff.seconds, 3600)
    minutes, _ = divmod(remainder, 60)

    parts = []
    if days > 0:
        parts.append(f"{days}d")
    if hours > 0:
        parts.append(f"{hours}h")
    if minutes > 0 or not parts:
        parts.append(f"{minutes}min")
    return "za " + " ".join(parts)


async def build_panel_embed(guild_id: int) -> discord.Embed:
    async with db.acquire() as conn:
        events = await conn.fetch("""
            SELECT e.*, COUNT(s.user_id) AS sub_count
            FROM events e
            LEFT JOIN subscriptions s ON s.event_id = e.id
            WHERE e.guild_id = $1
            ORDER BY e.next_run ASC
        """, guild_id)

    embed = discord.Embed(
        title="📅 Nadchodzące wydarzenia",
        color=discord.Color.blurple(),
        timestamp=datetime.now(TZ)
    )
    embed.set_footer(text="Ostatnia aktualizacja")

    if not events:
        embed.description = "Brak wydarzeń. Kliknij **➕ Dodaj wydarzenie** żeby dodać pierwsze!"
        return embed

    for e in events:
        next_run = e["next_run"].astimezone(TZ)
        countdown = format_countdown(e["next_run"])
        repeat_label = REPEAT_OPTIONS.get(e["repeat_type"], e["repeat_type"])
        remind_label = REMIND_OPTIONS.get(e["remind_min"], f"{e['remind_min']} min przed")

        value = (
            f"⏰ **{countdown}** ({next_run.strftime('%d.%m.%Y %H:%M')})\n"
            f"🔁 {repeat_label} · 🔔 {remind_label}\n"
            f"👥 {e['sub_count']} subskrybentów"
        )
        if e["description"]:
            value += f"\n📝 {e['description']}"

        embed.add_field(name=f"🎯 {e['name']}", value=value, inline=False)

    return embed


async def update_panel(guild_id: int):
    """Odśwież stały panel na kanale."""
    async with db.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT channel_id, message_id FROM panel_messages WHERE guild_id = $1", guild_id
        )
    if not row:
        return

    channel = bot.get_channel(row["channel_id"])
    if not channel:
        return

    try:
        message = await channel.fetch_message(row["message_id"])
        embed = await build_panel_embed(guild_id)
        view = PanelView(guild_id)
        await message.edit(embed=embed, view=view)
    except discord.NotFound:
        log.warning(f"Panel message not found for guild {guild_id}")


# ─── Views & Modals ──────────────────────────────────────────────────────────

class AddEventModal(discord.ui.Modal, title="➕ Nowe wydarzenie"):
    event_name = discord.ui.TextInput(
        label="Nazwa wydarzenia",
        placeholder="np. Poker Night",
        max_length=50
    )
    description = discord.ui.TextInput(
        label="Opis (opcjonalnie)",
        placeholder="Krótki opis...",
        required=False,
        max_length=200
    )
    date_time = discord.ui.TextInput(
        label="Data i czas (DD.MM.YYYY HH:MM)",
        placeholder="np. 20.06.2026 20:00",
        max_length=16
    )

    def __init__(self, repeat_type: str, remind_min: int, channel_id: int):
        super().__init__()
        self.repeat_type = repeat_type
        self.remind_min = remind_min
        self.channel_id = channel_id

    async def on_submit(self, interaction: discord.Interaction):
        # Parsuj datę
        try:
            naive = datetime.strptime(self.date_time.value.strip(), "%d.%m.%Y %H:%M")
            next_run = TZ.localize(naive)
        except ValueError:
            await interaction.response.send_message(
                "❌ Zły format daty. Użyj: `DD.MM.YYYY HH:MM` np. `20.06.2026 20:00`",
                ephemeral=True
            )
            return

        if next_run < datetime.now(TZ):
            await interaction.response.send_message(
                "❌ Data musi być w przyszłości!",
                ephemeral=True
            )
            return

        name = self.event_name.value.strip()

        try:
            async with db.acquire() as conn:
                await conn.execute("""
                    INSERT INTO events (guild_id, channel_id, name, description, next_run, repeat_type, remind_min, created_by)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                """,
                    interaction.guild_id,
                    self.channel_id,
                    name,
                    self.description.value.strip() or None,
                    next_run,
                    self.repeat_type,
                    self.remind_min,
                    interaction.user.id
                )
        except asyncpg.UniqueViolationError:
            await interaction.response.send_message(
                f"❌ Wydarzenie **{name}** już istnieje!",
                ephemeral=True
            )
            return

        await interaction.response.send_message(
            f"✅ Wydarzenie **{name}** dodane! ({REPEAT_OPTIONS[self.repeat_type]}, przypomnienie {REMIND_OPTIONS.get(self.remind_min, f'{self.remind_min} min')} przed)",
            ephemeral=True
        )
        await update_panel(interaction.guild_id)


class RepeatSelect(discord.ui.Select):
    def __init__(self, channel_id: int):
        self.channel_id = channel_id
        options = [
            discord.SelectOption(label=label, value=value)
            for value, label in REPEAT_OPTIONS.items()
        ]
        super().__init__(placeholder="🔁 Jak często się powtarza?", options=options)

    async def callback(self, interaction: discord.Interaction):
        self.view.repeat_type = self.values[0]
        await interaction.response.edit_message(
            content=f"✅ Powtarzanie: **{REPEAT_OPTIONS[self.values[0]]}**\nTeraz wybierz kiedy przypomnieć:",
            view=RemindSelectView(self.view.repeat_type, self.channel_id)
        )


class RepeatSelectView(discord.ui.View):
    def __init__(self, channel_id: int):
        super().__init__(timeout=120)
        self.repeat_type = None
        self.add_item(RepeatSelect(channel_id))


class RemindSelect(discord.ui.Select):
    def __init__(self, repeat_type: str, channel_id: int):
        self.repeat_type = repeat_type
        self.channel_id = channel_id
        options = [
            discord.SelectOption(label=label, value=str(value))
            for value, label in REMIND_OPTIONS.items()
        ]
        super().__init__(placeholder="🔔 Kiedy przypomnienie?", options=options)

    async def callback(self, interaction: discord.Interaction):
        remind_min = int(self.values[0])
        modal = AddEventModal(self.repeat_type, remind_min, self.channel_id)
        await interaction.response.send_modal(modal)


class RemindSelectView(discord.ui.View):
    def __init__(self, repeat_type: str, channel_id: int):
        super().__init__(timeout=120)
        self.add_item(RemindSelect(repeat_type, channel_id))


class SubscribeSelect(discord.ui.Select):
    def __init__(self, events: list):
        self.events = {str(e["id"]): e["name"] for e in events}
        options = [
            discord.SelectOption(label=e["name"], value=str(e["id"]))
            for e in events[:25]
        ]
        super().__init__(placeholder="Wybierz wydarzenie...", options=options)

    async def callback(self, interaction: discord.Interaction):
        event_id = int(self.values[0])
        event_name = self.events[self.values[0]]

        async with db.acquire() as conn:
            existing = await conn.fetchrow(
                "SELECT 1 FROM subscriptions WHERE event_id=$1 AND user_id=$2",
                event_id, interaction.user.id
            )
            if existing:
                await conn.execute(
                    "DELETE FROM subscriptions WHERE event_id=$1 AND user_id=$2",
                    event_id, interaction.user.id
                )
                msg = f"👋 Wypisałeś się z **{event_name}**."
            else:
                await conn.execute(
                    "INSERT INTO subscriptions (event_id, user_id) VALUES ($1, $2)",
                    event_id, interaction.user.id
                )
                msg = f"✅ Zapisałeś się na **{event_name}**! Dostaniesz ping przed wydarzeniem."

        await interaction.response.send_message(msg, ephemeral=True)
        await update_panel(interaction.guild_id)


class SubscribeView(discord.ui.View):
    def __init__(self, events: list):
        super().__init__(timeout=60)
        self.add_item(SubscribeSelect(events))


class PanelView(discord.ui.View):
    def __init__(self, guild_id: int):
        super().__init__(timeout=None)
        self.guild_id = guild_id

    @discord.ui.button(label="Zapisz się / Wypisz", style=discord.ButtonStyle.primary, emoji="🔔", custom_id="panel_subscribe")
    async def subscribe_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        async with db.acquire() as conn:
            events = await conn.fetch(
                "SELECT id, name FROM events WHERE guild_id=$1 ORDER BY next_run ASC",
                interaction.guild_id
            )
        if not events:
            await interaction.response.send_message("Brak wydarzeń do zapisania.", ephemeral=True)
            return
        view = SubscribeView(list(events))
        await interaction.response.send_message("Wybierz wydarzenie:", view=view, ephemeral=True)

    @discord.ui.button(label="Dodaj wydarzenie", style=discord.ButtonStyle.success, emoji="➕", custom_id="panel_add")
    async def add_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.user.guild_permissions.manage_events:
            await interaction.response.send_message("❌ Potrzebujesz uprawnienia `Zarządzaj wydarzeniami`.", ephemeral=True)
            return
        async with db.acquire() as conn:
            panel = await conn.fetchrow(
                "SELECT channel_id FROM panel_messages WHERE guild_id=$1", interaction.guild_id
            )
        channel_id = panel["channel_id"] if panel else interaction.channel_id
        view = RepeatSelectView(channel_id)
        await interaction.response.send_message("Wybierz jak często wydarzenie ma się powtarzać:", view=view, ephemeral=True)

    @discord.ui.button(label="Usuń wydarzenie", style=discord.ButtonStyle.danger, emoji="🗑️", custom_id="panel_delete")
    async def delete_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.user.guild_permissions.manage_events:
            await interaction.response.send_message("❌ Potrzebujesz uprawnienia `Zarządzaj wydarzeniami`.", ephemeral=True)
            return
        async with db.acquire() as conn:
            events = await conn.fetch(
                "SELECT id, name FROM events WHERE guild_id=$1 ORDER BY name ASC",
                interaction.guild_id
            )
        if not events:
            await interaction.response.send_message("Brak wydarzeń.", ephemeral=True)
            return
        view = DeleteSelectView(list(events))
        await interaction.response.send_message("Wybierz wydarzenie do usunięcia:", view=view, ephemeral=True)


class DeleteSelect(discord.ui.Select):
    def __init__(self, events: list):
        options = [
            discord.SelectOption(label=e["name"], value=str(e["id"]))
            for e in events[:25]
        ]
        super().__init__(placeholder="Wybierz wydarzenie...", options=options)

    async def callback(self, interaction: discord.Interaction):
        event_id = int(self.values[0])
        async with db.acquire() as conn:
            row = await conn.fetchrow(
                "DELETE FROM events WHERE id=$1 AND guild_id=$2 RETURNING name",
                event_id, interaction.guild_id
            )
        if row:
            await interaction.response.send_message(f"🗑️ Usunięto **{row['name']}**.", ephemeral=True)
            await update_panel(interaction.guild_id)
        else:
            await interaction.response.send_message("❌ Nie znaleziono wydarzenia.", ephemeral=True)


class DeleteSelectView(discord.ui.View):
    def __init__(self, events: list):
        super().__init__(timeout=60)
        self.add_item(DeleteSelect(events))


# ─── Background tasks ────────────────────────────────────────────────────────

@tasks.loop(minutes=1)
async def check_events():
    now = datetime.now(TZ)
    async with db.acquire() as conn:
        # Sprawdź przypomnienia
        to_remind = await conn.fetch("""
            SELECT * FROM events
            WHERE reminded = FALSE
            AND next_run - (remind_min * interval '1 minute') <= $1
            AND next_run > $1
        """, now)

        for event in to_remind:
            await send_notification(event, reminder=True)
            await conn.execute("UPDATE events SET reminded=TRUE WHERE id=$1", event["id"])

        # Sprawdź główne eventy
        to_fire = await conn.fetch("""
            SELECT * FROM events WHERE next_run <= $1
        """, now)

        for event in to_fire:
            await send_notification(event, reminder=False)
            next_run = next_run_after(event["next_run"].astimezone(TZ), event["repeat_type"])
            if next_run:
                await conn.execute(
                    "UPDATE events SET next_run=$1, reminded=FALSE WHERE id=$2",
                    next_run, event["id"]
                )
            else:
                await conn.execute("DELETE FROM events WHERE id=$1", event["id"])

    # Odśwież panele dla serwerów które miały aktywność
    if to_remind or to_fire:
        guild_ids = set(
            [e["guild_id"] for e in to_remind] + [e["guild_id"] for e in to_fire]
        )
        for guild_id in guild_ids:
            await update_panel(guild_id)


@tasks.loop(minutes=5)
async def refresh_panels():
    """Co 5 minut odśwież wszystkie panele (aktualizacja countdownów)."""
    async with db.acquire() as conn:
        panels = await conn.fetch("SELECT guild_id FROM panel_messages")
    for row in panels:
        await update_panel(row["guild_id"])


async def send_notification(event, reminder: bool):
    channel = bot.get_channel(event["channel_id"])
    if not channel:
        return

    async with db.acquire() as conn:
        subs = await conn.fetch(
            "SELECT user_id FROM subscriptions WHERE event_id=$1", event["id"]
        )

    mentions = " ".join(f"<@{r['user_id']}>" for r in subs) if subs else ""
    next_run = event["next_run"].astimezone(TZ)

    if reminder:
        embed = discord.Embed(
            title=f"⏰ Przypomnienie: {event['name']}",
            description=event["description"] or "",
            color=discord.Color.orange()
        )
        embed.set_footer(text=f"Wydarzenie startuje za {event['remind_min']} minut! ({next_run.strftime('%H:%M')})")
    else:
        embed = discord.Embed(
            title=f"🎉 Zaczyna się: {event['name']}",
            description=event["description"] or "",
            color=discord.Color.green()
        )
        embed.set_footer(text="Wydarzenie właśnie się zaczyna!")

    await channel.send(content=mentions or None, embed=embed)


# ─── Slash commands ──────────────────────────────────────────────────────────

@bot.tree.command(name="setup_panel", description="Ustaw stały panel wydarzeń na tym kanale (admin)")
@app_commands.checks.has_permissions(administrator=True)
async def setup_panel(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    embed = await build_panel_embed(interaction.guild_id)
    view = PanelView(interaction.guild_id)
    msg = await interaction.channel.send(embed=embed, view=view)

    async with db.acquire() as conn:
        await conn.execute("""
            INSERT INTO panel_messages (guild_id, channel_id, message_id)
            VALUES ($1, $2, $3)
            ON CONFLICT (guild_id) DO UPDATE SET channel_id=$2, message_id=$3
        """, interaction.guild_id, interaction.channel_id, msg.id)

    await interaction.followup.send("✅ Panel ustawiony! Możesz przypiąć tę wiadomość.", ephemeral=True)


# ─── Bot events ──────────────────────────────────────────────────────────────

@bot.event
async def on_ready():
    log.info(f"Bot zalogowany jako {bot.user} (id={bot.user.id})")
    await init_db()

    # Przywróć persistent views
    async with db.acquire() as conn:
        panels = await conn.fetch("SELECT guild_id FROM panel_messages")
    for row in panels:
        bot.add_view(PanelView(row["guild_id"]))

    check_events.start()
    refresh_panels.start()

    guild = discord.Object(id=GUILD_ID)
    bot.tree.copy_global_to(guild=guild)
    synced = await bot.tree.sync(guild=guild)
    log.info(f"Zsynchronizowano {len(synced)} komend slash.")


if __name__ == "__main__":
    bot.run(TOKEN)
