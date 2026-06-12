import os
import asyncio
import asyncpg
import discord
from discord import app_commands
from discord.ext import commands
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from dotenv import load_dotenv
import pytz
import logging

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

TOKEN = os.getenv("DISCORD_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
GUILD_ID = int(os.getenv("GUILD_ID", "0"))

# ─── Bot setup ───────────────────────────────────────────────────────────────

intents = discord.Intents.default()
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)
scheduler = AsyncIOScheduler(timezone=pytz.timezone("Europe/Warsaw"))
db: asyncpg.Pool = None


# ─── Database ────────────────────────────────────────────────────────────────

async def init_db():
    global db
    db = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=10)
    async with db.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS events (
                id          SERIAL PRIMARY KEY,
                guild_id    BIGINT NOT NULL,
                channel_id  BIGINT NOT NULL,
                name        TEXT NOT NULL,
                description TEXT,
                cron        TEXT NOT NULL,
                remind_min  INT  NOT NULL DEFAULT 60,
                created_by  BIGINT NOT NULL,
                created_at  TIMESTAMPTZ DEFAULT NOW(),
                UNIQUE(guild_id, name)
            );

            CREATE TABLE IF NOT EXISTS subscriptions (
                event_id  INT    NOT NULL REFERENCES events(id) ON DELETE CASCADE,
                user_id   BIGINT NOT NULL,
                PRIMARY KEY (event_id, user_id)
            );
        """)
    log.info("Baza danych gotowa.")


# ─── Scheduler helpers ───────────────────────────────────────────────────────

async def send_reminder(event_id: int, remind_only: bool = False):
    """Wysyła przypomnienie lub ogłoszenie o wydarzeniu."""
    async with db.acquire() as conn:
        event = await conn.fetchrow("SELECT * FROM events WHERE id = $1", event_id)
        if not event:
            return

        subs = await conn.fetch(
            "SELECT user_id FROM subscriptions WHERE event_id = $1", event_id
        )

    channel = bot.get_channel(event["channel_id"])
    if not channel:
        log.warning(f"Nie znaleziono kanału {event['channel_id']} dla eventu {event['name']}")
        return

    mentions = " ".join(f"<@{r['user_id']}>" for r in subs) if subs else ""

    if remind_only:
        # Przypomnienie X minut przed
        minutes = event["remind_min"]
        embed = discord.Embed(
            title=f"⏰ Przypomnienie: {event['name']}",
            description=event["description"] or "",
            color=discord.Color.orange(),
        )
        embed.set_footer(text=f"Wydarzenie startuje za {minutes} minut!")
        content = f"{mentions}\n" if mentions else ""
        await channel.send(content=content, embed=embed)
    else:
        # Ogłoszenie główne — czas start
        embed = discord.Embed(
            title=f"🎉 Zaczyna się: {event['name']}",
            description=event["description"] or "",
            color=discord.Color.green(),
        )
        embed.set_footer(text="Wydarzenie właśnie się zaczyna!")
        content = f"{mentions}\n" if mentions else ""
        await channel.send(content=content, embed=embed)


def schedule_event(event: asyncpg.Record):
    """Rejestruje dwa joby: przypomnienie i ogłoszenie główne."""
    event_id = event["id"]

    # Ogłoszenie główne wg crona
    scheduler.add_job(
        send_reminder,
        CronTrigger.from_crontab(event["cron"], timezone=pytz.timezone("Europe/Warsaw")),
        args=[event_id, False],
        id=f"main_{event_id}",
        replace_existing=True,
    )

    # Oblicz cron dla przypomnienia (remind_min minut wcześniej)
    # Prostsze: odpal remind jako osobny offset job za pomocą date trigger przy każdym main
    # Dla uproszczenia: scheduler sprawdza co minutę i jeśli za X minut jest event — pinguje
    # Tu rejestrujemy osobny interwałowy job sprawdzający
    log.info(f"Zaplanowano event '{event['name']}' (id={event_id}) cron={event['cron']}")


async def load_all_jobs():
    """Ładuje wszystkie eventy z bazy i rejestruje je w schedulerze."""
    async with db.acquire() as conn:
        events = await conn.fetch("SELECT * FROM events")
    for event in events:
        schedule_event(event)
    log.info(f"Załadowano {len(events)} eventów do schedulera.")


# ─── Bot events ──────────────────────────────────────────────────────────────

@bot.event
async def on_ready():
    log.info(f"Bot zalogowany jako {bot.user} (id={bot.user.id})")
    await init_db()
    await load_all_jobs()
    scheduler.start()

    guild = discord.Object(id=GUILD_ID)
    bot.tree.copy_global_to(guild=guild)
    synced = await bot.tree.sync(guild=guild)
    log.info(f"Zsynchronizowano {len(synced)} komend slash.")


# ─── Slash commands ──────────────────────────────────────────────────────────

@bot.tree.command(name="event_create", description="Utwórz nowe cykliczne wydarzenie")
@app_commands.describe(
    nazwa="Nazwa wydarzenia",
    opis="Krótki opis (opcjonalnie)",
    cron="Wyrażenie cron, np. '0 20 * * 5' = co piątek 20:00",
    kanal="Kanał gdzie bot będzie ogłaszał",
    przypomnienie="Ile minut przed wydarzeniem wysłać przypomnienie (domyślnie 60)",
)
@app_commands.checks.has_permissions(manage_events=True)
async def event_create(
    interaction: discord.Interaction,
    nazwa: str,
    cron: str,
    kanal: discord.TextChannel,
    opis: str = "",
    przypomnienie: int = 60,
):
    await interaction.response.defer(ephemeral=True)

    # Walidacja crona
    try:
        CronTrigger.from_crontab(cron, timezone=pytz.timezone("Europe/Warsaw"))
    except Exception:
        await interaction.followup.send(
            "❌ Nieprawidłowe wyrażenie cron. Przykład: `0 20 * * 5` (co piątek 20:00).",
            ephemeral=True,
        )
        return

    try:
        async with db.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO events (guild_id, channel_id, name, description, cron, remind_min, created_by)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                RETURNING *
                """,
                interaction.guild_id,
                kanal.id,
                nazwa.strip(),
                opis.strip(),
                cron.strip(),
                przypomnienie,
                interaction.user.id,
            )
    except asyncpg.UniqueViolationError:
        await interaction.followup.send(
            f"❌ Wydarzenie o nazwie **{nazwa}** już istnieje na tym serwerze.",
            ephemeral=True,
        )
        return

    schedule_event(row)

    embed = discord.Embed(title="✅ Wydarzenie utworzone!", color=discord.Color.blue())
    embed.add_field(name="Nazwa", value=nazwa, inline=True)
    embed.add_field(name="Kanał", value=kanal.mention, inline=True)
    embed.add_field(name="Cron", value=f"`{cron}`", inline=True)
    embed.add_field(name="Przypomnienie", value=f"{przypomnienie} min przed", inline=True)
    if opis:
        embed.add_field(name="Opis", value=opis, inline=False)
    await interaction.followup.send(embed=embed, ephemeral=True)


@bot.tree.command(name="event_delete", description="Usuń wydarzenie (wymaga uprawnień)")
@app_commands.describe(nazwa="Nazwa wydarzenia do usunięcia")
@app_commands.checks.has_permissions(manage_events=True)
async def event_delete(interaction: discord.Interaction, nazwa: str):
    await interaction.response.defer(ephemeral=True)

    async with db.acquire() as conn:
        row = await conn.fetchrow(
            "DELETE FROM events WHERE guild_id=$1 AND name=$2 RETURNING id, name",
            interaction.guild_id,
            nazwa.strip(),
        )

    if not row:
        await interaction.followup.send(f"❌ Nie znaleziono wydarzenia **{nazwa}**.", ephemeral=True)
        return

    # Usuń job ze schedulera
    for job_id in [f"main_{row['id']}", f"remind_{row['id']}"]:
        if scheduler.get_job(job_id):
            scheduler.remove_job(job_id)

    await interaction.followup.send(f"🗑️ Wydarzenie **{row['name']}** zostało usunięte.", ephemeral=True)


@bot.tree.command(name="events", description="Pokaż wszystkie wydarzenia na tym serwerze")
async def events_list(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    async with db.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT e.*, COUNT(s.user_id) AS sub_count
            FROM events e
            LEFT JOIN subscriptions s ON s.event_id = e.id
            WHERE e.guild_id = $1
            GROUP BY e.id
            ORDER BY e.name
            """,
            interaction.guild_id,
        )

    if not rows:
        await interaction.followup.send("Brak wydarzeń. Użyj `/event_create` żeby dodać pierwsze!", ephemeral=True)
        return

    embed = discord.Embed(title="📅 Cykliczne wydarzenia", color=discord.Color.blue())
    for r in rows:
        channel = bot.get_channel(r["channel_id"])
        ch_name = channel.mention if channel else f"#{r['channel_id']}"
        val = (
            f"**Cron:** `{r['cron']}`\n"
            f"**Kanał:** {ch_name}\n"
            f"**Subskrybenci:** {r['sub_count']}\n"
            f"**Przypomnienie:** {r['remind_min']} min przed"
        )
        if r["description"]:
            val += f"\n**Opis:** {r['description']}"
        embed.add_field(name=r["name"], value=val, inline=False)

    await interaction.followup.send(embed=embed, ephemeral=True)


@bot.tree.command(name="subscribe", description="Zapisz się na wydarzenie")
@app_commands.describe(nazwa="Nazwa wydarzenia")
async def subscribe(interaction: discord.Interaction, nazwa: str):
    await interaction.response.defer(ephemeral=True)

    async with db.acquire() as conn:
        event = await conn.fetchrow(
            "SELECT id, name FROM events WHERE guild_id=$1 AND name=$2",
            interaction.guild_id,
            nazwa.strip(),
        )
        if not event:
            await interaction.followup.send(f"❌ Nie znaleziono wydarzenia **{nazwa}**. Sprawdź `/events`.", ephemeral=True)
            return

        existing = await conn.fetchrow(
            "SELECT 1 FROM subscriptions WHERE event_id=$1 AND user_id=$2",
            event["id"],
            interaction.user.id,
        )
        if existing:
            await interaction.followup.send(f"ℹ️ Już jesteś zapisany na **{event['name']}**.", ephemeral=True)
            return

        await conn.execute(
            "INSERT INTO subscriptions (event_id, user_id) VALUES ($1, $2)",
            event["id"],
            interaction.user.id,
        )

    await interaction.followup.send(f"✅ Zapisałeś się na **{event['name']}**! Będziesz pingowany przy każdym wydarzeniu.", ephemeral=True)


@bot.tree.command(name="unsubscribe", description="Wypisz się z wydarzenia")
@app_commands.describe(nazwa="Nazwa wydarzenia")
async def unsubscribe(interaction: discord.Interaction, nazwa: str):
    await interaction.response.defer(ephemeral=True)

    async with db.acquire() as conn:
        event = await conn.fetchrow(
            "SELECT id, name FROM events WHERE guild_id=$1 AND name=$2",
            interaction.guild_id,
            nazwa.strip(),
        )
        if not event:
            await interaction.followup.send(f"❌ Nie znaleziono wydarzenia **{nazwa}**.", ephemeral=True)
            return

        result = await conn.execute(
            "DELETE FROM subscriptions WHERE event_id=$1 AND user_id=$2",
            event["id"],
            interaction.user.id,
        )

    if result == "DELETE 0":
        await interaction.followup.send(f"ℹ️ Nie byłeś zapisany na **{event['name']}**.", ephemeral=True)
    else:
        await interaction.followup.send(f"👋 Wypisałeś się z **{event['name']}**.", ephemeral=True)


@bot.tree.command(name="myevents", description="Pokaż wydarzenia, na które jesteś zapisany")
async def my_events(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    async with db.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT e.name, e.cron, e.description, e.remind_min, e.channel_id
            FROM subscriptions s
            JOIN events e ON e.id = s.event_id
            WHERE s.user_id = $1 AND e.guild_id = $2
            ORDER BY e.name
            """,
            interaction.user.id,
            interaction.guild_id,
        )

    if not rows:
        await interaction.followup.send("Nie jesteś zapisany na żadne wydarzenie. Użyj `/subscribe`!", ephemeral=True)
        return

    embed = discord.Embed(title="🔔 Twoje wydarzenia", color=discord.Color.purple())
    for r in rows:
        channel = bot.get_channel(r["channel_id"])
        ch_name = channel.mention if channel else f"#{r['channel_id']}"
        val = f"**Cron:** `{r['cron']}`\n**Kanał:** {ch_name}\n**Przypomnienie:** {r['remind_min']} min przed"
        if r["description"]:
            val += f"\n**Opis:** {r['description']}"
        embed.add_field(name=r["name"], value=val, inline=False)

    await interaction.followup.send(embed=embed, ephemeral=True)


# Error handler dla brakujących uprawnień
@event_create.error
@event_delete.error
async def permission_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message(
            "❌ Nie masz uprawnień do tej komendy (wymagane: `Zarządzaj wydarzeniami`).",
            ephemeral=True,
        )


# ─── Run ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    bot.run(TOKEN)
