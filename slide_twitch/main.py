""" Entry point to the bot """
import asyncio
import logging

from rich.logging import RichHandler

from slide_twitch.slide_bot import run_bot

rich_handler = RichHandler(rich_tracebacks=True)
logging.root.addHandler(rich_handler)
logging.root.setLevel(logging.INFO)

logger = logging.getLogger("slide_twitch")
logger.setLevel(logging.DEBUG)


def main():
    """Entry point to the bot"""
    asyncio.run(run_bot())
