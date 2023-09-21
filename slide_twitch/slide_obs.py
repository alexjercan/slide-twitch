""" Twitch Presentation Bot """
import asyncio
import logging
import os

import ffmpeg
import obsws_python as obs
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("slide_twitch")

OBS_WEBSOCKET_IP = os.environ["OBS_WEBSOCKET_IP"]
OBS_WEBSOCKET_PORT = os.environ["OBS_WEBSOCKET_PORT"]
OBS_WEBSOCKET_PASSWORD = os.environ["OBS_WEBSOCKET_PASSWORD"]


class OBS:
    """The OBS class

    This class is used to interact with OBS. It will setup OBS for the
    presentation and will display messages and videos on the screen.

    Attributes
    ----------
    client : obs.ReqClient
        The OBS client
    """

    def __init__(self):
        """OBS"""
        self.client = None

    @classmethod
    async def create(cls) -> "OBS":
        """Setup OBS

        This function will setup OBS for the presentation. It will create a new
        scene called `Presentation` and will add a VLC source called
        `vlc_source` to the scene. It will also mute the `Mic/Aux` and `Desktop
        Audio` inputs.

        Raises
        ------
        Exception
            If could not setup OBS
        """
        logger.debug("Setting up OBS Scene")

        client = obs.ReqClient(
            host=OBS_WEBSOCKET_IP,
            port=OBS_WEBSOCKET_PORT,
            password=OBS_WEBSOCKET_PASSWORD,
        )

        result = client.get_scene_list()
        scenes = result.scenes
        if "Presentation" in [scene["sceneName"] for scene in scenes]:
            client.remove_scene("Presentation")

        # TODO: Can this be fixed?
        # Needed to wait for OBS to update the scene list... UGH!
        await asyncio.sleep(0.5)

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
        position_x = (1920 - 1024) / 2
        position_y = (1080 - 1024) / 2
        client.set_scene_item_transform(
            "Presentation",
            item_id,
            {"positionX": position_x, "positionY": position_y},
        )

        client.set_input_mute("Mic/Aux", True)
        client.set_input_mute("Desktop Audio", True)

        self = OBS()
        self.client = client

        return self

    async def reset_obs(self):
        """Reset OBS

        This function will reset OBS. It will remove the text of the text
        source and will remove the video from the VLC source.

        Raises
        ------
        Exception
            If could not reset OBS
        """
        logger.debug("Resetting OBS Scene")

        self.client.set_input_settings(
            "Text (FreeType 2)",
            settings={
                "text": "",
            },
            overlay=True,
        )
        self.client.set_input_settings(
            "VLC Video Source",
            settings={
                "playlist": [],
            },
            overlay=True,
        )

    async def display_message(self, message: str, duration: int):
        """Display a message on the screen

        This function will display a message on the screen. It will set the
        source to be text and will set the text to the message. It will wait
        for the duration and then will remove the source.

        Parameters
        ----------
        message : str
            The message to display
        duration : int
            The duration to display the message for in seconds

        Raises
        ------
        Exception
            If could not display the message
        """
        logger.debug("Displaying message: '%s'", message)

        self.client.set_input_settings(
            "Text (FreeType 2)",
            settings={
                "text": message,
            },
            overlay=True,
        )

        await asyncio.sleep(0.1)

        response = self.client.get_scene_item_id(
            "Presentation", "Text (FreeType 2)"
        )
        item_id = response.scene_item_id
        response = self.client.get_scene_item_transform(
            "Presentation", item_id
        )
        transform = response.scene_item_transform
        source_width = transform["sourceWidth"]
        source_height = transform["sourceHeight"]
        position_x = (1920 - source_width) / 2
        position_y = (1080 - source_height) / 2
        self.client.set_scene_item_transform(
            "Presentation",
            item_id,
            {"positionX": position_x, "positionY": position_y},
        )

        await asyncio.sleep(duration)

        self.client.set_input_settings(
            "Text (FreeType 2)",
            settings={
                "text": "",
            },
            overlay=True,
        )

    async def play_run(self, run: str):
        """Play the video for the run

        This function will play the video for the run. It will set the VLC
        source to the video file and will wait for the video to finish.

        Parameters
        ----------
        run : str
            The path to the run output directory

        Raises
        ------
        Exception
            If could not play the video
        """
        path = os.path.join(run, "prompt.txt")

        with open(path, "r", encoding="utf-8") as file:
            prompt = file.read()

        await self.display_message(prompt, 3)

        path = os.path.join(run, "video.mp4")

        assert os.path.exists(path), f"Video file does not exist for run {run}"

        logger.debug("Playing video: '%s'", path)

        self.client.set_input_settings(
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


if __name__ == "__main__":
    from rich.logging import RichHandler

    from slide_twitch.util import OUTPUT

    async def experiment():
        """Experiment"""
        try:
            client = await OBS.create()
        except ConnectionRefusedError:
            logger.error("Make sure that OBS is running!")
            return

        # start to play the video
        task = asyncio.create_task(client.play_run(os.path.join(OUTPUT, "0")))

        # after 4 seconds, cancel the task and reset OBS
        await asyncio.sleep(4)
        task.cancel()
        await client.reset_obs()

        # start to play the next video
        task = asyncio.create_task(client.play_run(os.path.join(OUTPUT, "1")))

        # after 5 seconds, cancel the task and reset OBS
        await asyncio.sleep(5)
        task.cancel()
        await client.reset_obs()

    rich_handler = RichHandler(rich_tracebacks=True)
    logging.root.addHandler(rich_handler)
    logging.root.setLevel(logging.CRITICAL)

    logger = logging.getLogger("slide_twitch")
    logger.setLevel(logging.DEBUG)

    asyncio.run(experiment())
