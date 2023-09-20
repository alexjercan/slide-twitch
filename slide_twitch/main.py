import asyncio
import concurrent.futures
import glob
import json
import logging
import os
import urllib.request
import wave

import fakeyou
import ffmpeg
import obsws_python as obs
import openai
from dotenv import load_dotenv
from tqdm import tqdm
from twitchAPI import Twitch
from twitchAPI.chat import Chat, ChatCommand, EventData
from twitchAPI.oauth import UserAuthenticator
from twitchAPI.types import AuthScope, ChatEvent
from rich.logging import RichHandler
from logging.handlers import QueueHandler, QueueListener
import queue
from typing import Any

load_dotenv()

LOG_LEVEL = os.getenv("APPLICATION_LOG_LEVEL", "DEBUG")
LOG_FILE = os.getenv("APPLICATION_LOG_FILE", "app.log")

log_queue: queue.Queue[Any] = queue.Queue(-1)
queue_handler = QueueHandler(log_queue)

logger = logging.getLogger(__name__)
logger.setLevel(logging._nameToLevel[LOG_LEVEL])
logger.addHandler(queue_handler)

rich_handler = RichHandler()
file_handler = logging.FileHandler(LOG_FILE)
file_handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))

queue_listener = QueueListener(log_queue, file_handler, rich_handler)
queue_listener.start()

SYSTEM = """Your job is to create a slide presentation for a video. \
In this presentation you must include a speech for the current slide and a \
description for the background image. You need to make it as story-like as \
possible. The format of the output must be in JSON. You have to output a list \
of objects. Each object will contain a key for the speech called "text" and a \
key for the image description called "image".

For example for a slide presentation about the new iphone you could output \
something like:

```
[
  {
    "text": "Hello. Today we will discuss about the new iphone",
    "image": "Image of a phone on a business desk with a black background"
  },
  {
    "text": "Apple is going to release this new iphone this summer",
    "image": "A group of happy people with phones in their hand"
  },
  {
    "text": "Thank you for watching my presentation",
    "image": "A thank you message on white background"
  }
]
```

Make sure to output only JSON text. Do not output any extra comments.
"""
SPEAKER = "TM:cpwrmn5kwh97"
MODEL = "gpt-4"
OUTPUT = os.path.join(os.getcwd(), "videos")

APP_ID = os.environ["APP_ID"]
APP_SECRET = os.environ["APP_SECRET"]
USER_SCOPE = [AuthScope.CHAT_READ, AuthScope.CHAT_EDIT]
TARGET_CHANNEL = os.environ["TARGET_CHANNEL"]
FAKEYOU_USERNAME = os.environ["FAKEYOU_USERNAME"]
FAKEYOU_PASSWORD = os.environ["FAKEYOU_PASSWORD"]
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]
OBS_WEBSOCKET_IP = os.environ["OBS_WEBSOCKET_IP"]
OBS_WEBSOCKET_PORT = os.environ["OBS_WEBSOCKET_PORT"]
OBS_WEBSOCKET_PASSWORD = os.environ["OBS_WEBSOCKET_PASSWORD"]


def get_output_run(output: str = OUTPUT) -> int:
    """Create a new folder inside the output directory for this run

    Parameters
    ----------
    output : str, optional
        The output directory to use for the files, by default OUTPUT

    Returns
    -------
    int
        The run number
    """
    if not os.path.exists(output):
        os.mkdir(output)

    run = 0
    while os.path.exists(os.path.join(output, str(run))):
        run += 1

    run_path = os.path.join(output, str(run))
    assert not os.path.exists(run_path), "Run path already exists"
    os.mkdir(run_path)

    return run


def create_video(output: str):
    """Create the video from the slides

    The video will be saved in the output directory as `video.mp4`. The video
    will be created by concatenating the images and audio files together.

    Parameters
    ----------
    output : str
        The output directory to use for the files

    Raises
    ------
    ValueError
        If the number of image and audio files is not the same
    """
    logger.debug("Creating video...")

    image_files = sorted(glob.glob(os.path.join(output, "slide_*.png")))
    audio_files = sorted(glob.glob(os.path.join(output, "slide_*.wav")))

    if len(image_files) != len(audio_files):
        raise ValueError("Number of image and audio files must be the same")

    input_streams = []
    for image_file, audio_file in zip(image_files, audio_files):
        input_streams.append(ffmpeg.input(image_file))
        input_streams.append(ffmpeg.input(audio_file))

    ffmpeg.concat(*input_streams, v=1, a=1).output(
        os.path.join(output, "video.mp4"),
        pix_fmt="yuv420p",
        loglevel="quiet",
    ).overwrite_output().run()


def srt_seconds_to_hh_mm_ss_mmm(seconds: float) -> str:
    """Convert seconds to HH:MM:SS,mmm format

    Parameters
    ----------
    seconds : float
        The seconds to convert

    Returns
    -------
    str
        The seconds in HH:MM:SS,mmm format
    """
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    r_seconds = int(seconds % 60)
    milliseconds = int((seconds - int(seconds)) * 1000)

    result = f"{hours:02d}:{minutes:02d}:{r_seconds:02d},{milliseconds:03d}"

    return result


def create_srt(output: str):
    """Create the SRT file for the presentation

    The SRT file will be saved in the output directory as `video.srt`.
    The timing for each slide will be based on the `.wav` length.

    Parameters
    ----------
    output : str
        The output directory to use for the files
    """
    logger.debug("Creating srt...")

    audio_files = sorted(glob.glob(os.path.join(output, "slide_*.wav")))

    with open(
        os.path.join(output, "presentation.json"), "r", encoding="utf-8"
    ) as file:
        presentation = json.load(file)

    with open(
        os.path.join(output, "video.srt"), "w", encoding="utf-8"
    ) as file:
        current_s = 0

        for index, (slide, audio_file) in enumerate(
            zip(presentation, audio_files)
        ):
            with open(audio_file, "rb") as audio_f:
                audio = wave.open(audio_f)
                duration = audio.getnframes() / audio.getframerate()

            start = current_s
            end = current_s + duration

            start_fmt = srt_seconds_to_hh_mm_ss_mmm(start)
            end_fmt = srt_seconds_to_hh_mm_ss_mmm(end)

            file.write(f"{index + 1}\n")
            file.write(f"{start_fmt} --> {end_fmt}\n")
            file.write(f"{slide['text']}\n")
            file.write("\n")

            current_s = end


def vtt_seconds_to_hh_mm_ss_mmm(seconds: float) -> str:
    """Convert seconds to HH:MM:SS.mmm format

    Parameters
    ----------
    seconds : float
        The seconds to convert

    Returns
    -------
    str
        The seconds in HH:MM:SS.mmm format
    """
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    r_seconds = int(seconds % 60)
    milliseconds = int((seconds - int(seconds)) * 1000)

    result = f"{hours:02d}:{minutes:02d}:{r_seconds:02d}.{milliseconds:03d}"

    return result


def create_vtt(output: str):
    """Create the VTT file for the presentation

    The SRT file will be saved in the output directory as `video.vtt`.
    The timing for each slide will be based on the `.wav` length.

    Parameters
    ----------
    output : str
        The output directory to use for the files
    """
    logger.debug("Creating vtt...")

    audio_files = sorted(glob.glob(os.path.join(output, "slide_*.wav")))

    with open(
        os.path.join(output, "presentation.json"), "r", encoding="utf-8"
    ) as file:
        presentation = json.load(file)

    with open(
        os.path.join(output, "video.vtt"), "w", encoding="utf-8"
    ) as file:
        current_s = 0

        file.write("WEBVTT\n\n")

        for index, (slide, audio_file) in enumerate(
            zip(presentation, audio_files)
        ):
            with open(audio_file, "rb") as audio_f:
                audio = wave.open(audio_f)
                duration = audio.getnframes() / audio.getframerate()

            start = current_s
            end = current_s + duration

            start_fmt = vtt_seconds_to_hh_mm_ss_mmm(start)
            end_fmt = vtt_seconds_to_hh_mm_ss_mmm(end)

            file.write(f"{index + 1}\n")
            file.write(f"{start_fmt} --> {end_fmt}\n")
            file.write(f"{slide['text']}\n")
            file.write("\n")

            current_s = end


def create_slides(
    prompt: str,
    model: str = MODEL,
    speaker: str = SPEAKER,
    output: str = os.path.curdir,
):
    """Create the slides for the presentation

    The slides will be saved in the output directory as `slide_*.png` and
    `slide_*.wav`. The slides will be created by using the system prompt (from
    config) and the user prompt.

    Parameters
    ----------
    prompt : str
        The user prompt to use
    model : str, optional
        The model to use, by default c.MODEL
    speaker : str, optional
        The speaker to use, by default c.SPEAKER
    output : str, optional
        The output directory to use for the files, by default os.path.curdir
    """
    logger.debug("Creating slides...")

    fk_you = fakeyou.FakeYou()

    try:
        fk_you.login(username=FAKEYOU_USERNAME, password=FAKEYOU_PASSWORD)
    except fakeyou.exception.InvalidCredentials:
        logger.warning("Invalid login credentials for FakeYou")
    except fakeyou.exception.TooManyRequests:
        logger.error("Too many requests for FakeYou")

    with open(
        os.path.join(output, "prompt.txt"), "w", encoding="utf-8"
    ) as file:
        file.write(prompt)

    response = openai.ChatCompletion.create(
        api_key=OPENAI_API_KEY,
        model=model,
        messages=[
            {
                "role": "system",
                "content": SYSTEM,
            },
            {"role": "user", "content": prompt},
        ],
    )

    presentation = json.loads(response.choices[0].message.content)

    with open(
        os.path.join(output, "presentation.json"), "w", encoding="utf-8"
    ) as file:
        json.dump(presentation, file, indent=2)

    with tqdm(total=len(presentation)) as progress:
        for index, slide in enumerate(presentation):
            progress.set_description(
                f"Slide {index}: Image '{slide['image']}' ..."
            )

            response = openai.Image.create(
                prompt=slide["image"],
                n=1,
                size="1024x1024",
                api_key=OPENAI_API_KEY,
            )
            image_url = response["data"][0]["url"]

            path = os.path.join(output, f"slide_{index:02d}.png")
            urllib.request.urlretrieve(image_url, path)

            progress.set_description(
                f"Slide {index}: TTS ({speaker}) '{slide['text']}' ..."
            )

            path = os.path.join(output, f"slide_{index:02d}.wav")
            fk_you.say(slide["text"], speaker).save(path)

            progress.update(1)


def slide_gen(prompt: str, output: str):
    """Generate the slides for the presentation

    Wrapper function for create_slides. This function will create the slides
    using the user prompt and the system prompt. The function returns the run
    number for the presentation.

    Parameters
    ----------
    prompt : str
        The user prompt to use
    output : str
        The output directory to use for the files
    """
    create_slides(prompt, output=output)
    create_srt(output)
    create_vtt(output)
    create_video(output)


async def slide_gen_task(cmd: ChatCommand, output: str):
    """Generate the slides for the presentation

    Async wrapper for slide_gen.

    Parameters
    ----------
    cmd : ChatCommand
        The command to use for the user prompt
    output : str
        The output directory to use for the files
    """
    loop = asyncio.get_event_loop()

    with concurrent.futures.ThreadPoolExecutor() as executor:
        await loop.run_in_executor(executor, slide_gen, cmd.parameter, output)


async def run():
    """Run the bot

    This function will create the bot and start it. The bot will join the
    channel specified in the config and listen for commands. The bot will
    respond to the `present` command. The bot will queue the presentation
    requests and will start the presentation when the current presentation
    finishes.
    """
    presentations = asyncio.Queue(maxsize=5)

    client = obs.ReqClient(
        host=OBS_WEBSOCKET_IP,
        port=OBS_WEBSOCKET_PORT,
        password=OBS_WEBSOCKET_PASSWORD,
    )

    result = client.get_scene_list()
    scenes = result.scenes
    if "Presentation" in [scene["sceneName"] for scene in scenes]:
        client.remove_scene("Presentation")

    # Needed to wait for OBS to update the scene list... UGH!
    await asyncio.sleep(0.1)

    client.create_scene("Presentation")

    client.create_input("Presentation", "VLC Video Source", "vlc_source", {
        "loop": False,
        "playlist": [],
        "subtitle_enable": True,
    }, True)

    client.set_current_program_scene("Presentation")

    client.set_input_mute("Mic/Aux", True)
    client.set_input_mute("Desktop Audio", True)

    async def on_ready(ready_event: EventData):
        await ready_event.chat.join_room(TARGET_CHANNEL)
        logger.info("Bot is ready for work, joining channels")

    async def present_command(cmd: ChatCommand):
        if len(cmd.parameter) == 0:
            await cmd.reply("You need to specify a message to present!")
        elif presentations.full():
            await cmd.reply("Presentation queue is full, try again later!")
            logger.warning(
                f"Presentation queue is full, {cmd.user.name} skipped"
            )
        else:
            run = get_output_run()

            await cmd.reply(
                "Presentation queued!"
                f"{cmd.user.name} will present '{cmd.parameter}' next!"
            )
            logger.info(f"Video queued for {cmd.user.name} with run {run}...")

            output = os.path.join(OUTPUT, str(run))

            await slide_gen_task(cmd, output)
            await presentations.put(run)

    async def presentation_task():
        while True:
            run = await presentations.get()

            logger.info(f"Starting presentation for run {run}...")

            path = os.path.join(OUTPUT, str(run), "video.mp4")

            client.set_input_settings(
                "VLC Video Source",
                settings={
                    "playlist": [
                        {
                            "hidden": False,
                            "selected": False,
                            "value": path,
                        }
                    ],
                },
                overlay=True,
            )

            probe = ffmpeg.probe(path)
            duration = float(probe["format"]["duration"])
            await asyncio.sleep(duration)

            presentations.task_done()

    presentation_task = asyncio.create_task(presentation_task())

    twitch = await Twitch(APP_ID, APP_SECRET)
    auth = UserAuthenticator(twitch, USER_SCOPE)
    token, refresh_token = await auth.authenticate()
    await twitch.set_user_authentication(token, USER_SCOPE, refresh_token)

    chat = await Chat(twitch)

    chat.register_event(ChatEvent.READY, on_ready)

    chat.register_command("present", present_command)

    chat.start()

    try:
        while True:
            await asyncio.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        chat.stop()
        await twitch.close()
        presentation_task.cancel()


def main():
    asyncio.run(run())
