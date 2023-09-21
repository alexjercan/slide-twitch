""" Twitch Presentation Bot """
import asyncio
import concurrent.futures
import glob
import json
import logging
import os
import random
import urllib.request
import wave
from typing import Dict, Optional

import fakeyou
import ffmpeg
import obsws_python as obs
import openai
from dotenv import load_dotenv
from rich.logging import RichHandler
from rich.progress import Progress
from twitchAPI import Twitch
from twitchAPI.chat import Chat, ChatCommand, EventData
from twitchAPI.oauth import UserAuthenticator
from twitchAPI.types import AuthScope, ChatEvent

load_dotenv()

LOG_LEVEL = os.getenv("APPLICATION_LOG_LEVEL", "DEBUG")
LOG_FILE = os.getenv("APPLICATION_LOG_FILE", "app.log")

rich_handler = RichHandler()
file_handler = logging.FileHandler(LOG_FILE)
file_handler.setFormatter(
    logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="[%X]",
    )
)

logger = logging.getLogger("slide_twitch")
logger.setLevel(logging._nameToLevel[LOG_LEVEL])
logger.addHandler(rich_handler)
logger.addHandler(file_handler)

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

TWITCH_APP_ID = os.environ["TWITCH_APP_ID"]
TWITCH_APP_SECRET = os.environ["TWITCH_APP_SECRET"]
TWITCH_USER_SCOPE = [AuthScope.CHAT_READ, AuthScope.CHAT_EDIT]
TWITCH_TARGET_CHANNEL = os.environ["TWITCH_TARGET_CHANNEL"]
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

    Raises
    ------
    OSError
        If could not create the run directory
    AssertionError
        If the run directory already exists
    Exception
        If anything else goes wrong
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


def get_random_run(output: str = OUTPUT) -> Optional[int]:
    """Get a random run from the output directory

    Parameters
    ----------
    output : str, optional
        The output directory to use for the files, by default OUTPUT

    Returns
    -------
    Optional[int]
        The run number or None if no runs exist
    """
    if not os.path.exists(output):
        return None

    runs = [
        int(run)
        for run in os.listdir(output)
        if os.path.isdir(os.path.join(output, run))
        if os.path.exists(os.path.join(output, run, "video.mp4"))
    ]

    if len(runs) == 0:
        return None

    return random.choice(runs)


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

    Raises
    ------
    FileNotFoundError
        If the presentation file does not exist
    ValueError
        If the number of slides and audio files is not the same
    OSError
        If the presentation file cannot be opened
    Exception
        If anything else goes wrong
    """
    logger.debug("Creating srt...")

    audio_files = sorted(glob.glob(os.path.join(output, "slide_*.wav")))

    with open(
        os.path.join(output, "presentation.json"), "r", encoding="utf-8"
    ) as file:
        presentation = json.load(file)

    if len(presentation) != len(audio_files):
        raise ValueError("Number of slides and audio files must be same")

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

    Raises
    ------
    FileNotFoundError
        If the presentation file does not exist
    ValueError
        If the number of slides and audio files is not the same
    OSError
        If the presentation file cannot be opened
    Exception
        If anything else goes wrong
    """
    logger.debug("Creating vtt...")

    audio_files = sorted(glob.glob(os.path.join(output, "slide_*.wav")))

    with open(
        os.path.join(output, "presentation.json"), "r", encoding="utf-8"
    ) as file:
        presentation = json.load(file)

    if len(presentation) != len(audio_files):
        raise ValueError("Number of slides and audio files must be same")

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


def create_script(prompt: str, output: str) -> Dict:
    """Create the script for the presentation

    The script will be saved in the output directory as `presentation.json`.
    The script will be created by using the system prompt (from config) and
    the user prompt.

    Parameters
    ----------
    prompt : str
        The user prompt to use
    output : str
        The output directory to use for the files

    Returns
    -------
    Dict
        The presentation script

    Raises
    ------
    IndexError
        If the response is empty
    Exception
        If anything else goes wrong
    """
    response = openai.ChatCompletion.create(
        api_key=OPENAI_API_KEY,
        model=MODEL,
        messages=[
            {
                "role": "system",
                "content": SYSTEM,
            },
            {"role": "user", "content": prompt},
        ],
    )

    presentation = json.loads(response.choices[0].message.content)
    for slide in presentation:
        slide["speaker"] = SPEAKER

    with open(
        os.path.join(output, "presentation.json"), "w", encoding="utf-8"
    ) as file:
        json.dump(presentation, file, indent=2)

    return presentation


def create_image(index: int, slide: Dict, output: str):
    """Create the image for the slide

    The image will be saved in the output directory as `slide_*.png`. The image
    will be created by using the image prompt from the slide.

    Parameters
    ----------
    index : int
        The slide index
    slide : Dict
        The slide to create the image for
    output : str
        The output directory to use for the files

    Raises
    ------
    IndexError
        If the response is empty
    OSError
        If something goes wrong with the image download
    Exception
        If anything else goes wrong
    """
    response = openai.Image.create(
        prompt=slide["image"],
        n=1,
        size="1024x1024",
        api_key=OPENAI_API_KEY,
    )
    image_url = response["data"][0]["url"]

    path = os.path.join(output, f"slide_{index:02d}.png")
    urllib.request.urlretrieve(image_url, path)

    logger.debug(f"Slide {index}: Image '{slide['image']}'")


def create_audio(index: int, slide: Dict, output: str):
    """Create the audio for the slide

    The audio will be saved in the output directory as `slide_*.wav`. The audio
    will be created by using the text prompt from the slide.

    Parameters
    ----------
    index : int
        The slide index
    slide : Dict
        The slide to create the audio for
    output : str
        The output directory to use for the files

    Raises
    ------
    OSError
        If something goes wrong with the audio download
    Exception
        If anything else goes wrong
    """
    fk_you = fakeyou.FakeYou()

    try:
        fk_you.login(username=FAKEYOU_USERNAME, password=FAKEYOU_PASSWORD)
    except fakeyou.exception.InvalidCredentials:
        logger.warning("Invalid login credentials for FakeYou")
    except fakeyou.exception.TooManyRequests:
        logger.error("Too many requests for FakeYou")

    path = os.path.join(output, f"slide_{index:02d}.wav")
    fk_you.say(slide["text"], slide["speaker"]).save(path)

    logger.debug(f"Slide {index}: TTS ({slide['speaker']}) '{slide['text']}'")


def create_slides(
    prompt: str,
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
    output : str, optional
        The output directory to use for the files, by default os.path.curdir

    Raises
    ------
    Exception
        If could not create the slides
    """
    with open(
        os.path.join(output, "prompt.txt"), "w", encoding="utf-8"
    ) as file:
        file.write(prompt)

    with Progress() as progress:
        _ = progress.add_task("[yellow]Generating script", total=None)

        presentation = create_script(prompt, output)

    with Progress() as progress:
        task = progress.add_task(
            "Creating slides...", total=2 * len(presentation)
        )

        with concurrent.futures.ThreadPoolExecutor() as executor:
            futures = []
            for index, slide in enumerate(presentation):
                futures.append(
                    executor.submit(create_image, index, slide, output)
                )

            for future in concurrent.futures.as_completed(futures):
                _ = future.result()
                progress.advance(task)

        # TODO: Will have to make this concurrent too for speed
        # FakeYou is kind of trash and needs to use serial requests
        for index, slide in enumerate(presentation):
            create_audio(index, slide, output)
            progress.advance(task)


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

    Raises
    ------
    Exception
        If could not create the slides
    """
    create_slides(prompt, output=output)

    try:
        create_srt(output)
    except Exception as exc:
        logger.warning("SRT creation failed: %s", exc)

    try:
        create_vtt(output)
    except Exception as exc:
        logger.warning("VTT creation failed: %s", exc)

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

    Raises
    ------
    Exception
        If could not create the slides
    """
    loop = asyncio.get_event_loop()

    with concurrent.futures.ThreadPoolExecutor() as executor:
        await loop.run_in_executor(executor, slide_gen, cmd.parameter, output)


async def setup_obs(client: obs.ReqClient):
    """Setup OBS

    This function will setup OBS for the presentation. It will create a new
    scene called `Presentation` and will add a VLC source called `vlc_source`
    to the scene. It will also mute the `Mic/Aux` and `Desktop Audio` inputs.

    Parameters
    ----------
    client : obs.ReqClient
        The OBS client to use

    Raises
    ------
    Exception
        If could not setup OBS
    """
    result = client.get_scene_list()
    scenes = result.scenes
    if "Presentation" in [scene["sceneName"] for scene in scenes]:
        client.remove_scene("Presentation")

    # TODO: Can this be fixed?
    # Needed to wait for OBS to update the scene list... UGH!
    await asyncio.sleep(0.1)

    client.create_scene("Presentation")

    client.create_input(
        "Presentation",
        "VLC Video Source",
        "vlc_source",
        {
            "loop": False,
            "playlist": [],
            "subtitle_enable": True,
        },
        True,
    )

    client.create_input(
        "Presentation",
        "Text (FreeType 2)",
        "text_ft2_source_v2",
        {
            "font": {
                "face": "Sans Serif",
                "flags": 0,
                "size": 72,
                "style": "",
            },
            "text": "",
            "custom_width": 1920 * 2 / 3,
            "word_wrap": True,
        },
        True,
    )

    client.set_current_program_scene("Presentation")

    response = client.get_scene_item_id("Presentation", "VLC Video Source")
    item_id = response.scene_item_id
    positionX = (1920 - 1024) / 2
    positionY = (1080 - 1024) / 2
    client.set_scene_item_transform(
        "Presentation",
        item_id,
        {"positionX": positionX, "positionY": positionY},
    )

    client.set_input_mute("Mic/Aux", True)
    client.set_input_mute("Desktop Audio", True)


async def display_message(client: obs.ReqClient, message: str, duration: int):
    """Display a message on the screen

    This function will display a message on the screen. It will set the source
    to be text and will set the text to the message. It will wait for the
    duration and then will remove the source.

    Parameters
    ----------
    client : obs.ReqClient
        The OBS client to use
    message : str
        The message to display
    duration : int
        The duration to display the message for in seconds

    Raises
    ------
    Exception
        If could not display the message
    """
    client.set_input_settings(
        "Text (FreeType 2)",
        settings={
            "text": message,
        },
        overlay=True,
    )

    await asyncio.sleep(0.1)

    response = client.get_scene_item_id("Presentation", "Text (FreeType 2)")
    item_id = response.scene_item_id
    response = client.get_scene_item_transform("Presentation", item_id)
    transform = response.scene_item_transform
    source_width = transform["sourceWidth"]
    source_height = transform["sourceHeight"]
    positionX = (1920 - source_width) / 2
    positionY = (1080 - source_height) / 2
    client.set_scene_item_transform(
        "Presentation",
        item_id,
        {"positionX": positionX, "positionY": positionY},
    )

    await asyncio.sleep(duration)

    client.set_input_settings(
        "Text (FreeType 2)",
        settings={
            "text": "",
        },
        overlay=True,
    )


async def play_run(client: obs.ReqClient, run: int):
    """Play the video for the run

    This function will play the video for the run. It will set the VLC source
    to the video file and will wait for the video to finish.

    Parameters
    ----------
    client : obs.ReqClient
        The OBS client to use
    run : int
        The run number to play

    Raises
    ------
    Exception
        If could not play the video
    """
    path = os.path.join(OUTPUT, str(run), "prompt.txt")

    with open(path, "r", encoding="utf-8") as file:
        prompt = file.read()

    try:
        await display_message(client, prompt, 3)
    except Exception as exc:
        logger.warning("Failed to display message: %s", exc)

    path = os.path.join(OUTPUT, str(run), "video.mp4")

    assert os.path.exists(path), "Video file does not exist for run %s" % run

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


async def run_bot():
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

    await setup_obs(client)

    async def on_ready(ready_event: EventData):
        await ready_event.chat.join_room(TWITCH_TARGET_CHANNEL)
        logger.info("Bot is ready for work, joining channels")

    async def present_command(cmd: ChatCommand):
        if len(cmd.parameter) == 0:
            await cmd.reply("You need to specify a message to present!")
        elif presentations.full():
            await cmd.reply("Presentation queue is full, try again later!")
            logger.warning(
                "Presentation queue is full, %s skipped", cmd.user.name
            )
        else:
            try:
                run = get_output_run()
            except Exception as exc:
                logger.error("Failed to get run: %s", exc)
                return

            await cmd.reply(
                "Presentation queued!"
                f"{cmd.user.name} will present '{cmd.parameter}' next!"
            )
            logger.info(
                "Video queued for %s with run %s...", cmd.user.name, run
            )

            output = os.path.join(OUTPUT, str(run))

            try:
                await slide_gen_task(cmd, output)
            except Exception as exc:
                logger.error("Failed to create slides: %s", exc)
                return

            try:
                await presentations.put(run)
            except Exception as exc:
                logger.error("Failed to add run to queue: %s", exc)

    async def presentation_task():
        while True:
            try:
                run = await presentations.get()
            except Exception as exc:
                logger.error("Failed to get run from queue: %s", exc)
                continue

            logger.info("Starting presentation for run %s...", run)

            try:
                await play_run(client, run)
            except Exception as exc:
                logger.error("Failed to play run: %s", exc)
                continue

            try:
                presentations.task_done()
            except Exception as exc:
                logger.error("Failed to mark run as done: %s", exc)

    async def random_presentation():
        while True:
            if presentations.empty():
                try:
                    run = get_random_run()
                except Exception as exc:
                    logger.error("Failed to get random run: %s", exc)
                    continue

                if run is not None:
                    logger.info(
                        "Starting random presentation for run %s...", run
                    )

                    try:
                        await play_run(client, run)
                    except Exception as exc:
                        logger.error("Failed to play run: %s", exc)
                        continue

    presentation_task = asyncio.create_task(presentation_task())
    random_presentation_task = asyncio.create_task(random_presentation())

    twitch = await Twitch(TWITCH_APP_ID, TWITCH_APP_SECRET)
    auth = UserAuthenticator(twitch, TWITCH_USER_SCOPE)
    token, refresh_token = await auth.authenticate()
    await twitch.set_user_authentication(token, TWITCH_USER_SCOPE, refresh_token)

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
        random_presentation_task.cancel()


def main():
    """Entry point to the bot"""
    asyncio.run(run_bot())
