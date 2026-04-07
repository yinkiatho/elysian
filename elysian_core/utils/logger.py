import logging
import datetime
import pathlib
from pathlib import Path
import sys
import asyncio
from elysian_core.utils.discord_risk_bot import DiscordBotManager, DiscordHandler
from dotenv import load_dotenv
import os

load_dotenv()  # Load environment variables from .env file

loggers = {}
_discord_bot_manager = None
_discord_handlers = []  # track handlers for shutdown
_test_loggers_sent = set()   # track which loggers already sent test message

SUCCESS_LEVEL = 21
logging.addLevelName(SUCCESS_LEVEL, "SUCCESS")

_utc_plus_8 = datetime.timezone(datetime.timedelta(hours=8))
_run_timestamp: str = datetime.datetime.now(_utc_plus_8).strftime('%Y-%m-%d_%H-%M-%S')


def success(self, message, *args, **kwargs):
    if self.isEnabledFor(SUCCESS_LEVEL):
        self._log(SUCCESS_LEVEL, message, args, **kwargs)
        

logging.Logger.success = success  # ← this line was missing
# ANSI escape codes for coloring text in terminal
class ColoredFormatter(logging.Formatter):
    COLORS = {
        'DEBUG':    '\033[34m',   # Blue
        'INFO':     '\033[97m',   # Bright white
        'SUCCESS':  '\033[92m',   # Bright green
        'WARNING':  '\033[33m',   # Yellow
        'ERROR':    '\033[31m',   # Red
        'CRITICAL': '\033[41m',   # White on red background
        'RESET':    '\033[0m'     # Reset
    }

    def format(self, record):
        log_message = super().format(record)
        color = self.COLORS.get(record.levelname, self.COLORS['RESET'])
        return f'{color}{log_message}{self.COLORS["RESET"]}'


def _add_discord_handler_to_logger(logger):
    if _discord_bot_manager is None:
        return
    discord_handler = DiscordHandler(_discord_bot_manager, level=logger.level)
    discord_handler.setFormatter(logging.Formatter(
        fmt='%(asctime)s - %(levelname)s - %(module)s - %(filename)s:%(lineno)d - %(message)s'
    ))
    logger.addHandler(discord_handler)
    _discord_handlers.append(discord_handler)
    
    if logger.name not in _test_loggers_sent:
        logger.success(f"Discord logging test logger '{logger.name}' initialized at {_run_timestamp}")
        _test_loggers_sent.add(logger.name)


async def init_discord_logging(token: str, guild_id: int):
    global _discord_bot_manager
    _discord_bot_manager = DiscordBotManager(token, guild_id)
    await _discord_bot_manager.start()
    
    # Optionally, add Discord handler to any already‑created loggers
    for name, logger in loggers.items():
        if not any(isinstance(h, DiscordHandler) for h in logger.handlers):
            _add_discord_handler_to_logger(logger)

    
async def shutdown_discord():
    global _discord_bot_manager
    if _discord_bot_manager:
        # Get all logger names that have been used (any logger that had Discord handler added)
        # We can use _test_loggers_sent which contains names of loggers that got test message
        # Or iterate over loggers dict and check for DiscordHandler presence.
        active_logger_names = []
        for name, logger in loggers.items():
            if any(isinstance(h, DiscordHandler) for h in logger.handlers):
                active_logger_names.append(name)
        
        # Send shutdown message to each channel
        print(f'Total loggers with Discord handler: {len(active_logger_names)}')
        shutdown_msg = "ENDING --- Discord bot shutting down"
        for logger_name in active_logger_names:
            _discord_bot_manager.enqueue_log(logger_name, shutdown_msg)

        # Force flush the queue before stopping
        await _discord_bot_manager.flush()
        await _discord_bot_manager.stop()
        _discord_bot_manager = None
         
    
def setup_custom_logger(name, log_level=logging.INFO, enable_discord=True):
    if loggers.get(name):
        return loggers[name]

    logger = logging.getLogger(name)
    loggers[name] = logger

    path = pathlib.Path(__file__).parent.resolve()
    
    # Include filename and line number in the formatter
    formatter = logging.Formatter(fmt='%(asctime)s - %(levelname)s - %(module)s - %(filename)s:%(lineno)d - %(message)s')

    # Stream handler with colored output
    color_formatter = ColoredFormatter(fmt='%(asctime)s - %(levelname)s - %(module)s - %(filename)s:%(lineno)d - %(message)s')
    stream_handler = logging.StreamHandler(stream=open(sys.stdout.fileno(), mode='w', encoding='utf-8', buffering=1, closefd=False))
    stream_handler.setFormatter(color_formatter)

    logger.setLevel(log_level)
    logger.propagate = False

    # Log file: logs/<run_timestamp>/<name>.log
    log = Path(path) / 'logs' / _run_timestamp / f'{name}.log'
    log.parent.mkdir(parents=True, exist_ok=True)
    log.touch(exist_ok=True)

    file_handler = logging.FileHandler(log, encoding='utf-8')
    file_handler.setFormatter(formatter)
    file_handler.setLevel(log_level)

    # Add both handlers
    logger.addHandler(stream_handler)
    logger.addHandler(file_handler)
    
    # Discord handler
    if enable_discord and _discord_bot_manager is not None:
        _add_discord_handler_to_logger(logger)
        
    return logger