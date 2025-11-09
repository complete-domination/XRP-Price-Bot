import os
import asyncio
import logging
import contextlib
from typing import Tuple, Optional, List

import aiohttp
import discord

# ---------- Config ----------
TOKEN = os.environ.get("TOKEN")
GUILD_ID_RAW = os.environ.get("GUILD_ID")  # optional; if unset, update all guilds

COIN_IDS: List[str] = [
    s.strip() for s in os.environ.get("COINGECKO_IDS", "ripple,xrp").split(",") if s.strip()
]

INTERVAL_SECONDS = int(os.environ.get("INTERVAL_SECONDS", "60"))

if not TOKEN:
    raise SystemExit("Missing env var TOKEN")

GUILD_ID: Optional[int] = None
if GUILD_ID_RAW:
    try:
        GUILD_ID = int(GUILD_ID_RAW)
    except ValueError:
        raise SystemExit("GUILD_ID must be an integer if provided")

# ---------- Logging ----------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("xrp-bot")

# ---------- Discord client ----------
intents = discord.Intents.default()
intents.guilds = True
intents.members = True  # enable Server Members Intent in Dev Portal
client = discord.Client(intents=intents)

# Global session + task
_http_session: Optional[aiohttp.ClientSession] = None
update_task: Optional[asyncio.Task] = None


# ---------- HTTP helpers ----------
async def _fetch_one_id(session: aiohttp.ClientSession, coin_id: str) -> Tuple[float, float]:
    """Fetch price and 24h change for a single CoinGecko id."""
    url = f"https://api.coingecko.com/api/v3/coins/markets?vs_currency=usd&ids={coin_id}"
    timeout = aiohttp.ClientTimeout(total=10)
    headers = {"User-Agent": "xrp-discord-bot/1.0"}
    async with session.get(url, timeout=timeout, headers=headers) as resp:
        if resp.status != 200:
            raise RuntimeError(f"HTTP {resp.status}")
        data = await resp.json()
        if not isinstance(data, list) or not data:
            raise RuntimeError("empty or invalid payload")
        row = data[0]
        price = float(row["current_price"])
        change = float(row["price_change_percentage_24h"])
        return price, change


async def get_price_data(session: aiohttp.ClientSession) -> Tuple[float, float]:
    """Try each candidate CoinGecko id with retries."""
    backoffs = [0, 1.5, 3.0, 5.0]
    last_err: Optional[Exception] = None

    for coin_id in COIN_IDS:
        for attempt, delay in enumerate(backoffs, start=1):
            if delay:
                await asyncio.sleep(delay)
            try:
                price, change = await _fetch_one_id(session, coin_id)
                if coin_id != COIN_IDS[0]:
                    log.info(f"Using fallback CoinGecko id '{coin_id}'.")
                return price, change
            except (aiohttp.ClientError, asyncio.TimeoutError, RuntimeError) as e:
                last_err = e
                log.warning(f"CoinGecko error for id '{coin_id}' (attempt {attempt}): {e}")

    raise RuntimeError(f"Failed to fetch XRP price after retries: {last_err}")


# ---------- Per-guild update ----------
async def update_guild(guild: discord.Guild):
    try:
        me = guild.me or await guild.fetch_member(client.user.id)
    except discord.HTTPException as e:
        log.warning(f"[{guild.name}] Could not fetch bot member: {e}")
        return

    perms = me.guild_permissions
    if not (perms.change_nickname or perms.manage_nicknames):
        log.info(f"[{guild.name}] Missing permission: Change Nickname (or Manage Nicknames).")
        return

    try:
        assert _http_session is not None, "HTTP session not initialized"
        price, change_24h = await get_price_data(_http_session)
    except Exception as e:
        log.warning(f"[{guild.name}] Price fetch failed: {e}")
        try:
            await client.change_presence(activity=discord.Game(name="API error"))
        except Exception:
            pass
        return

    emoji = "🟢" if change_24h >= 0 else "🔴"
    nickname = f"${price:.3f} {emoji}"  # Only price, no 'XRP' label
    if len(nickname) > 32:
        nickname = nickname[:32]

    try:
        await me.edit(nick=nickname, reason="Auto price update")
    except discord.Forbidden:
        log.info(f"[{guild.name}] Forbidden: role hierarchy/permissions block nickname change.")
    except discord.HTTPException as e:
        log.warning(f"[{guild.name}] HTTP error updating nick: {e}")

    try:
        await client.change_presence(activity=discord.Game(name=f"24h {change_24h:+.2f}%"))
    except Exception as e:
        log.debug(f"[{guild.name}] Could not set presence: {e}")

    log.info(f"[{guild.name}] Nick → {nickname} | 24h → {change_24h:+.2f}%")


# ---------- Updater loop ----------
async def updater_loop():
    await client.wait_until_ready()
    log.info("Updater loop started.")
    while not client.is_closed():
        try:
            if GUILD_ID:
                g = client.get_guild(GUILD_ID)
                targets = [g] if g else []
                if not g:
                    log.info("Configured GUILD_ID not found yet. Is the bot in that server?")
            else:
                targets = list(client.guilds)

            if not targets:
                log.info("No guilds to update yet.")
            else:
                await asyncio.gather(*(update_guild(g) for g in targets))
        except Exception as e:
            log.error(f"Updater loop error: {e}")

        await asyncio.sleep(INTERVAL_SECONDS)


# ---------- Discord events ----------
@client.event
async def on_ready():
    global update_task, _http_session
    log.info(f"Logged in as {client.user} in {len(client.guilds)} guild(s).")

    if _http_session is None or _http_session.closed:
        _http_session = aiohttp.ClientSession()

    if update_task is None or update_task.done():
        update_task = asyncio.create_task(updater_loop())


@client.event
async def on_disconnect():
    log.warning("Discord disconnected.")


@client.event
async def on_resumed():
    log.info("Discord session resumed.")


# ---------- Shutdown ----------
async def _shutdown():
    global _http_session, update_task
    if update_task and not update_task.done():
        update_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await update_task
    if _http_session and not _http_session.closed:
        await _http_session.close()


# ---------- Entrypoint ----------
if __name__ == "__main__":
    try:
        client.run(TOKEN)
    except KeyboardInterrupt:
        pass
