""" Twitch Presentation Bot """
import asyncio
import concurrent.futures
import logging
import os

from dotenv import load_dotenv
from rich.live import Live
from rich.panel import Panel
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn
from twitchAPI import Twitch
from twitchAPI.chat import Chat, ChatCommand, EventData
from twitchAPI.oauth import UserAuthenticator
from twitchAPI.types import AuthScope, ChatEvent

from slide_twitch.slide_gpt import slide_gen
from slide_twitch.slide_obs import OBS
from slide_twitch.util import OUTPUT, get_output_run, get_random_run

load_dotenv()

logger = logging.getLogger("slide_twitch")

TWITCH_APP_ID = os.environ["TWITCH_APP_ID"]
TWITCH_APP_SECRET = os.environ["TWITCH_APP_SECRET"]
TWITCH_USER_SCOPE = [AuthScope.CHAT_READ, AuthScope.CHAT_EDIT]
TWITCH_TARGET_CHANNEL = os.environ["TWITCH_TARGET_CHANNEL"]


class SlideBot:
    """Twitch Presentation Bot

    This class is used to manage the presentation queue and play the
    presentations.

    Attributes
    ----------
    presentations : asyncio.Queue
        The queue of presentations to play
    play_task : asyncio.Task
        The task for playing the presentation
    output : str
        The output directory to use for the files
    chat : Chat
        The chat client to use
    client : OBS
        The OBS client to use
    presentation_task : asyncio.Task
        The task for managing the presentation queue
    job_progress : Progress
        The progress bar for the current job
    live : Live
        The live panel for the progress bar
    """

    # pylint: disable=too-many-instance-attributes

    def __init__(self, chat: Chat, client: OBS, output: str = OUTPUT):
        """Setup the bot

        Parameters
        ----------
        chat : Chat
            The chat client to use
        client : OBS
            The OBS client to use
        output : str, optional
            The output directory to use for the files, by default OUTPUT
        """
        self.presentations: asyncio.Queue = asyncio.Queue(maxsize=5)
        self.play_task = None

        self.output = output
        self.chat = chat
        self.client = client

        chat.register_event(ChatEvent.READY, self.on_ready)

        chat.register_command("present", self.present_command)
        chat.register_command("skip", self.skip_command)

        self.presentation_task = None

        self.job_progress = Progress(
            "{task.description}",
            SpinnerColumn(),
            BarColumn(),
            TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
        )
        panel = Panel.fit(self.job_progress, title="Slide Twitch")
        self.live = Live(panel, refresh_per_second=10)

    async def on_ready(self, ready_event: EventData):
        """Handle the ready event

        Parameters
        ----------
        ready_event : EventData
            The ready event data
        """
        await ready_event.chat.join_room(TWITCH_TARGET_CHANNEL)
        logger.info("Bot is ready for work, joining channels")

    async def present_command(self, cmd: ChatCommand):
        """Handle the present command

        This command will generate a video for the given prompt and add it to
        the presentation queue. If the queue is full, the command will fail and
        the user will be notified.

        Parameters
        ----------
        cmd : ChatCommand
            The chat command data
        """
        if len(cmd.parameter) == 0:
            await cmd.reply("You need to specify a message to present!")
        elif self.presentations.full():
            await cmd.reply("Presentation queue is full, try again later!")
            logger.warning(
                "Presentation queue is full, %s skipped", cmd.user.name
            )
        else:
            run = get_output_run(self.output)
            output = os.path.join(self.output, str(run))

            await cmd.reply(
                "Presentation queued! "
                f"{cmd.user.name} will present '{cmd.parameter}' next!"
            )
            logger.info(
                "Video queued for %s with run %s...", cmd.user.name, run
            )

            loop = asyncio.get_event_loop()

            with concurrent.futures.ThreadPoolExecutor() as executor:
                await loop.run_in_executor(
                    executor,
                    slide_gen,
                    cmd.parameter,
                    output,
                    self.job_progress,
                )

            await self.presentations.put(run)

    async def skip_command(self, cmd: ChatCommand):
        """Handle the skip command

        This command will skip the current presentation. If no presentation is
        currently playing, the command will fail and the user will be notified.
        This command can only be used by the target channel.

        Parameters
        ----------
        cmd : ChatCommand
            The chat command data
        """
        if cmd.user.name != TWITCH_TARGET_CHANNEL:
            return

        if self.play_task is None or self.play_task.done():
            await cmd.reply("No presentation is currently playing!")
            logger.warning("No presentation is currently playing")
        else:
            self.play_task.cancel()
            await self.client.reset_obs()
            await cmd.reply("Presentation skipped!")
            logger.info("Presentation skipped by %s", cmd.user.name)

    async def _presentation_task(self):
        """The task for managing the presentation queue

        This task will play a random presentation if the queue is empty and no
        presentation is currently playing. If the queue is not empty, the task
        will play the next presentation in the queue.
        """
        while True:
            if (
                self.play_task is None or self.play_task.done()
            ) and self.presentations.empty():
                run = get_random_run()

                if run is None:
                    await asyncio.sleep(1)
                    continue

                logger.info("Queueing random presentation for run %s...", run)
                path = os.path.join(self.output, str(run))
                self.play_task = asyncio.create_task(
                    self.client.play_run(path)
                )
            elif not self.presentations.empty():
                run = await self.presentations.get()
                logger.info("Starting presentation for run %s...", run)

                path = os.path.join(self.output, str(run))
                self.play_task = asyncio.create_task(
                    self.client.play_run(path)
                )
                self.play_task.add_done_callback(
                    lambda _: self.presentations.task_done()
                )
            else:
                await asyncio.sleep(1)

    def start(self):
        """Start the Chat Bot"""
        self.chat.start()

        self.presentation_task = asyncio.create_task(self._presentation_task())

        self.live.start()

    async def stop(self):
        """Stop the Chat Bot"""
        self.chat.stop()

        if self.presentation_task is not None:
            self.presentation_task.cancel()

        if self.play_task is not None and not self.play_task.done():
            self.play_task.cancel()

        await self.client.reset_obs()

        self.live.stop()


async def run_bot():
    """Main Function"""

    # Twitch Authentication
    twitch = await Twitch(TWITCH_APP_ID, TWITCH_APP_SECRET)
    auth = UserAuthenticator(twitch, TWITCH_USER_SCOPE)
    token, refresh_token = await auth.authenticate()
    await twitch.set_user_authentication(
        token, TWITCH_USER_SCOPE, refresh_token
    )

    chat = await Chat(twitch)

    # OBS Connection
    try:
        client = await OBS.create()
    except ConnectionRefusedError:
        logger.error("Failed to connect to OBS. Is it running?")
        return

    # Create Bot
    bot = SlideBot(chat, client)

    # Start Bot
    bot.start()

    # Wait for Ctrl+C
    try:
        while True:
            await asyncio.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        await bot.stop()
        await twitch.close()
