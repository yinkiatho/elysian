import asyncio
import logging
import re
import queue
import time
import discord
from discord.utils import get
from collections import defaultdict

class DiscordBotManager:
    def __init__(self, token: str, guild_id: int, max_queue_size=1000):
        self.token = token
        self.guild_id = guild_id
        self.bot = None
        self._ready = None
        self._send_queue = queue.Queue(maxsize=max_queue_size)
        self._sender_task = None
        self._loop = None
        self._channel_cache = {}
        self._last_send = defaultdict(float)
        self._rate_limit_seconds = 0.2

    async def start(self):
        """Start the bot and the sender task within the current event loop."""
        self._loop = asyncio.get_running_loop()
        self.bot = discord.Client(intents=discord.Intents.default())
        self._ready = asyncio.Event()

        @self.bot.event
        async def on_ready():
            print(f"Discord bot logged in as {self.bot.user}")
            self._ready.set()

        # Start the bot as a background task
        bot_task = asyncio.create_task(self.bot.start(self.token))
        # Wait for bot to be ready
        await self._ready.wait()
        # Start sender task
        self._sender_task = asyncio.create_task(self._sender())
        # We don't await bot_task here; it runs forever
        return bot_task  # caller may want to keep reference

    async def _sender(self):
        """Background task that pulls from queue and sends messages."""
        while True:
            try:
                # Use run_in_executor to get from queue without blocking the event loop
                logger_name, message = await self._loop.run_in_executor(
                    None, self._send_queue.get, True, 0.5
                )
                await self._send_log(logger_name, message)
            except queue.Empty:
                continue
            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"Error in Discord sender: {e}")

    async def _send_log(self, logger_name: str, message: str):
        """Internal async method to actually send a message."""
        # Get or create channel
        if logger_name not in self._channel_cache:
            channel = await self._get_or_create_channel(logger_name)
            self._channel_cache[logger_name] = channel.id
        else:
            channel = self.bot.get_channel(self._channel_cache[logger_name])
            if channel is None:
                channel = await self._get_or_create_channel(logger_name)
                self._channel_cache[logger_name] = channel.id

        # Rate limiting
        now = time.time()
        last = self._last_send[channel.id]
        if now - last < self._rate_limit_seconds:
            await asyncio.sleep(self._rate_limit_seconds - (now - last))
        
        # Truncate to Discord's 2000 character limit
        if len(message) > 2000:
            message = message[:1997] + "..."
        
        await channel.send(message)
        self._last_send[channel.id] = time.time()

    async def _get_or_create_channel(self, logger_name: str):
        """Async: returns a text channel for the logger name."""
        guild = self.bot.get_guild(self.guild_id)
        if guild is None:
            raise ValueError(f"Guild {self.guild_id} not found")
        sanitized = logger_name.lower().replace(' ', '-')
        sanitized = ''.join(c for c in sanitized if c.isalnum() or c in ('-', '_'))
        sanitized = sanitized[:100]
        channel = get(guild.text_channels, name=sanitized)
        if channel is None:
            channel = await guild.create_text_channel(sanitized)
            await channel.edit(topic=f"Logs for logger: {logger_name}")
        return channel

    def enqueue_log(self, logger_name: str, message: str) -> bool:
        """Thread-safe method to enqueue a log message. Returns True if enqueued, False if queue full."""
        try:
            self._send_queue.put_nowait((logger_name, message))
            return True
        except queue.Full:
            return False
        
    async def flush(self, timeout=5.0):
        """Wait until the send queue is empty and all pending messages are sent."""
        start = time.time()
        # Wait for queue to be empty
        while not self._send_queue.empty() and (time.time() - start) < timeout:
            await asyncio.sleep(0.05)
        # Give the sender a moment to finish the last message
        await asyncio.sleep(0.5)
        # Optionally, check that the sender task is still alive
        if self._sender_task and not self._sender_task.done():
            # Wait a bit more for the current send operation to complete
            try:
                await asyncio.wait_for(asyncio.shield(self._sender_task), timeout=0.5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass

    async def stop(self, timeout=5.0):
        """Flush remaining messages, then stop the bot."""
        await self.flush(timeout)
        if self._sender_task:
            self._sender_task.cancel()
            await asyncio.gather(self._sender_task, return_exceptions=True)
        if self.bot:
            await self.bot.close()


class DiscordHandler(logging.Handler):
    """Logging handler that enqueues messages to the Discord bot manager."""
    def __init__(self, bot_manager: DiscordBotManager, level=logging.NOTSET):
        super().__init__(level)
        self.bot_manager = bot_manager
        self.ansi_escape = re.compile(r'\x1B\[[0-?]*[ -/]*[@-~]')

    def emit(self, record: logging.LogRecord):
        try:
            msg = self.format(record)
            msg = self.ansi_escape.sub('', msg)
            if not self.bot_manager.enqueue_log(record.name, msg):
                self.handleError(record)  # queue full
        except Exception:
            self.handleError(record)