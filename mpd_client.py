################################################################################
#      ____  ___    ____  ________     ____  ____  ______
#     / __ \/   |  / __ \/  _/ __ \   / __ )/ __ \/_  __/
#    / /_/ / /| | / / / // // / / /  / __  / / / / / /
#   / _, _/ ___ |/ /_/ // // /_/ /  / /_/ / /_/ / / /
#  /_/ |_/_/  |_/_____/___/\____/  /_____/\____/ /_/
#
#
# Matheus Fillipe 18/05/2022
# MIT License
################################################################################


import asyncio
import datetime
import logging
import time
from pathlib import Path
from typing import Callable

import mpd
import trio
from mpd import MPDClient as Client

NEXT_LIST_LENGTH = 5
ADD_RETRY_DELAY = 5

logger = logging.getLogger()


def dropin(func):
    """Decorator that connects the client, executes the function and
    disconnects the client."""
    def wrapper(*args, **kwargs):
        MPDClient.connect()
        result = func(*args, **kwargs)
        MPDClient.disconnect()
        return result
    return wrapper


def int_args(func):
    """Decorator that converts all arguments to int."""

    def wrapper(self, *args, **kwargs):
        return func(self, *[int(a) for a in args], **{k: int(v) for k, v in kwargs.items()})
    return wrapper


def format_data(data, k):
    if k in ["elapsed", "duration"]:
        return str(datetime.timedelta(seconds=float(data[k].split('.')[0])))
    if k == "file":
        return Path(data[k]).stem
    return data[k]


def format_dict(d: dict):
    return ", ".join(f"{k}: {format_data(d, k)}" for k in d)


class MPDClient:
    client: Client = None
    _host: str = None
    _port: int = None

    def __init__(self, host, port):
        MPDClient._host = host
        MPDClient._port = port

    @classmethod
    def connect(cls):
        cls.client = Client()
        cls.client.connect(cls._host, cls._port)

    @classmethod
    def disconnect(cls):
        cls.client.close()
        cls.client.disconnect()

    @dropin
    def cmd(self, cmd: str):
        return getattr(MPDClient.client, cmd)()

    @dropin
    def current_song(self):
        data = {}
        filter_keys = ["state", "duration", "elapsed"]
        data.update(
            {k: v for k, v in MPDClient.client.status().items() if k in filter_keys})
        include_keys = ["duration", "file", "pos"]
        data.update(
            {k: v for k, v in MPDClient.client.currentsong().items() if k in include_keys})
        return ", ".join(f"{k}: {format_data(data, k)}" for k in data)

    @dropin
    def current_song_name(self):
        return format_data(MPDClient.client.currentsong(), "file")

    @dropin
    def next_songs(self):
        """Next songs in queue."""
        status = MPDClient.client.currentsong()
        pos = int(status["pos"])
        include_keys = ["duration", "file", "pos"]
        return [format_dict({k: v for k, v in song.items() if k in include_keys})
                for song in MPDClient.client.playlistinfo((pos, )) +
                MPDClient.client.playlistinfo((0, NEXT_LIST_LENGTH))
                ][:NEXT_LIST_LENGTH]

    @dropin
    def playlist(self):
        include_keys = ["duration", "file", "pos"]
        playlist = MPDClient.client.playlistinfo()
        info = [format_dict({k: v for k, v in song.items() if k in include_keys})
                for song in playlist]
        duration = int(sum(float(song['duration']) for song in playlist))
        status = MPDClient.client.status()
        length = int(status["playlistlength"])
        info.append(
            f"                      Total duration: {datetime.timedelta(seconds=duration)}")
        info.append(f"                      Total songs: {length}")
        return info

    @dropin
    def surrounding_ids(self):
        status = MPDClient.client.status()
        playlist = MPDClient.client.playlistinfo()
        return (playlist[int(status['song']) - 1]['id'], status["songid"], status["nextsongid"])

    @dropin
    def song_from_id(self, id):
        return MPDClient.client.playlistid(id)[0]

    @dropin
    def remove_id(self, id):
        MPDClient.client.deleteid(id)

    @dropin
    def add_next(self, song: str):
        MPDClient.client.add(song)
        status = MPDClient.client.currentsong()
        pos = int(status["pos"])
        status = MPDClient.client.status()
        length = int(status["playlistlength"])
        MPDClient.client.move(length - 1, pos + 1)

    @dropin
    def pos(self):
        return int(MPDClient.client.status()["song"])

    @dropin
    def length(self):
        return int(MPDClient.client.status()["playlistlength"])

    @dropin
    def get_id_at_pos(self, pos: int):
        return MPDClient.client.playlistinfo((pos, ))[0]["id"]

    @dropin
    def add_at_pos(self, song: str, pos: int, is_retry: bool = False):
        try:
            MPDClient.client.update(song)
            MPDClient.client.add(song)
        except mpd.base.CommandError:
            if is_retry:
                raise AssertionError("Could not add song to playlist")
            logger.info(f"New song... Retrying in {ADD_RETRY_DELAY} seconds")
            time.sleep(ADD_RETRY_DELAY)
            return self.add_at_pos(song, pos, True)
        status = MPDClient.client.status()
        length = int(status["playlistlength"])
        MPDClient.client.move(length - 1, pos)

    @dropin
    def next(self):
        MPDClient.client.next()

    @dropin
    def previous(self):
        MPDClient.client.previous()

    @dropin
    @int_args
    def play(self, pos: int):
        MPDClient.client.play(pos)

    @dropin
    @int_args
    def delete(self, pos: int):
        MPDClient.client.delete(pos)

    @dropin
    @int_args
    def move(self, pos: int, new_pos: int):
        MPDClient.client.move(pos, new_pos)

    async def wait_for_event(self, event="player"):
        stream = await trio.open_tcp_stream(MPDClient._host, MPDClient._port)
        async with stream:
            while True:
                response = await stream.receive_some(4096)
                response = response.decode().strip()
                if "OK MPD" in response:
                    await stream.send_all(f"idle {event}\n".encode())
                elif response.startswith("changed: "):
                    return response.split(" ")[1]


async def mpd_loop_with_handler(handler: Callable, event: str = "player"):
    c = MPDClient('localhost', 6600)
    while True:
        if await c.wait_for_event(event):
            if asyncio.iscoroutinefunction(handler):
                await handler()
            else:
                handler()
        await trio.sleep(0)


async def main():
    c = MPDClient('localhost', 6600)
    prev_id, id, next_id = c.surrounding_ids()
    print(c.song_from_id(prev_id))
    print(c.current_song())
    print(c.next_songs())
    print(c.playlist())
    await mpd_loop_with_handler(lambda: print(c.current_song_name()))

if __name__ == "__main__":
    trio.run(main)
