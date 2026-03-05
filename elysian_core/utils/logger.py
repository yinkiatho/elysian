import logging
import datetime
import pathlib
from pathlib import Path

loggers = {}

# ANSI escape codes for coloring text in terminal
class ColoredFormatter(logging.Formatter):
    COLORS = {
        'WARNING': '\033[33m',  # Yellow
        'ERROR': '\033[31m',    # Red
        'CRITICAL': '\033[41m', # White on red background
        'DEBUG': '\033[34m',    # Blue
        'INFO': '\033[32m',     # Green
        'RESET': '\033[0m'      # Reset to default color
    }

    def format(self, record):
        log_message = super().format(record)
        color = self.COLORS.get(record.levelname, self.COLORS['RESET'])
        return f'{color}{log_message}{self.COLORS["RESET"]}'

def setup_custom_logger(name, log_level=logging.INFO):
    if loggers.get(name):
        return loggers[name]

    logger = logging.getLogger(name)
    loggers[name] = logger

    path = pathlib.Path(__file__).parent.resolve()
    
    # Include filename and line number in the formatter
    formatter = logging.Formatter(fmt='%(asctime)s - %(levelname)s - %(module)s - %(filename)s:%(lineno)d - %(message)s')

    # Stream handler with colored output
    color_formatter = ColoredFormatter(fmt='%(asctime)s - %(levelname)s - %(module)s - %(filename)s:%(lineno)d - %(message)s')
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(color_formatter)

    logger.setLevel(log_level)
    utc_plus_8 = datetime.timezone(datetime.timedelta(hours=8))
    # Log file setup
    timestamp = datetime.datetime.now(utc_plus_8).strftime('%Y-%m-%d_%H-%M-%S')
    log = Path(f'{path}/logs/{timestamp}.log')
    log.parent.mkdir(parents=True, exist_ok=True)
    log.touch(exist_ok=True)

    file_handler = logging.FileHandler(log)
    file_handler.setFormatter(formatter)
    file_handler.setLevel(log_level)

    # Add both handlers
    logger.addHandler(stream_handler)
    logger.addHandler(file_handler)

    return logger