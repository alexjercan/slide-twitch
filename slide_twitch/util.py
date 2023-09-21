""" Utility functions for the project """
import os
import random
from typing import Optional

OUTPUT = os.path.join(os.getcwd(), "videos")


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

    Raises
    ------
    Exception
        If anything else goes wrong
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
