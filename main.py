"""im going to make a bot that will let you input a youtube link, audio url, or
send an audio with dcc and it will add it as the next on the queue, each use
can like have 3 audios at once on the queue unless admins.

It will also let you play live with a sonic pi ruby repl. Also will
create passwords for authorized users to use the openmic
"""

# TODO download videos from youtube
# TODO edit icecast password for a source randomly
# TODO add to playlist, limit users queue length
# TODO accept mp3 files from dcc
# TODO accept youtube or audio links
# TODO irc bot commands

# https://github.com/matheusfillipe/ircbot/blob/main/examples/dccbot.py
# https://python-mpd2.readthedocs.io/en/latest/topics/getting-started.html
# https://www.youtube.com/watch?v=3rW7Vpep3II 

import datetime
import logging
import re
import threading
import validators
from pathlib import Path

import trio
from cachetools import TTLCache
from IrcBot.bot import Color, IrcBot, Message, utils
from IrcBot.dcc import DccServer
from IrcBot.utils import debug, log

# import all excetions
from audio_download import (ExtensionNotAllowed, FailedToDownload,
                            FailedToProcess, MaxAudioLength, MaxFilesize,
                            download_audio)
from message_server import listen_loop
from mpd_client import MPDClient, loop_with_handler

LOGFILE = None
LEVEL = logging.DEBUG
HOST = 'irc.dot.org.es'
PORT = 6697
NICK = '_mpdbot'
PASSWORD = ''
CHANNELS = ["#bots"]
DCC_HOST = "127.0.0.1"
MUSIC_DIR = "~/music/mpdbot"
ICECAST_CONFIG = "/etc/icecast.xml"
MESSAGE_RELAY_FIFO_PATH = "/tmp/mpdbot_relay.sock"
MPD_HOST = "localhost"
MPD_PORT = 6600
MPD_FOLDER = "~/music/bot/"
MAX_USER_QUEUE_LENGTH = 3
PREFIX = "!"


utils.setPrefix(PREFIX)


mpd_client = MPDClient(MPD_HOST, MPD_PORT)

nick_cache = {}


@utils.custom_handler("dccsend")
async def on_dcc_send(bot: IrcBot, **m):
    nick = m["nick"]
    if not await is_identified(bot, nick):
        await bot.dcc_reject(DccServer.SEND, nick, m["filename"])
        await bot.send_message(
            "You cannot use this bot before you register your nick", nick
        )
        return

    notify_each_b = progress_curve(m["size"])

    config = Config.get(nick)

    async def progress_handler(p, message):
        if not config.display_progress:
            return
        percentile = int(p * 100)
        if percentile % notify_each_b == 0:
            await bot.send_message(message % percentile, m["nick"])

    folder = Folder(nick)
    if folder.size() + int(m["size"]) > int(Config.get(nick).quota) * 1048576:
        await bot.send_message(
            Message(
                m["nick"],
                message="Your quota has exceeded! Type 'info' to check, 'list' to see your files and 'delete [filename]' to free some space",
                is_private=True,
            )
        )
        return

    path = folder.download_path(m["filename"])
    await bot.dcc_get(
        str(path),
        m,
        progress_callback=lambda _, p: progress_handler(
            p, f"UPLOAD {Path(m['filename']).name} %s%%"
        ),
    )
    await bot.send_message(
        Message(
            m["nick"], message=f"{m['filename']} has been received!", is_private=True
        )
    )


@utils.custom_handler("dccreject")
def on_dcc_reject(**m):
    log(f"Rejected!!! {m=}")

async def is_identified(bot: IrcBot, nick: str) -> bool:
    global nick_cache
    nickserv = "NickServ"
    if nick in nick_cache and "status" in nick_cache[nick]:
        msg = nick_cache[nick]["status"]
    else:
        await bot.send_message(f"status {nick}", nickserv)
        # We need filter because multiple notices from nickserv can come at the same time
        # if multiple requests are being made to this function all together
        msg = await bot.wait_for(
            "notice",
            nickserv,
            timeout=5,
            cache_ttl=15,
            filter_func=lambda m: nick in m["text"],
        )
        nick_cache[nick] = TTLCache(128, 10)
        nick_cache[nick]["status"] = msg
    return msg.get("text").strip() == f"{nick} 3 {nick}" if msg else False

def _reply_str(bot: IrcBot, in_msg: Message, text: str):
    return f"({in_msg.nick}): {text}"

async def reply(bot: IrcBot, in_msg: Message, text: str):
    """Reply to a message."""
    msg = _reply_str(bot, in_msg, text)
    await bot.send_message(Message(channel=in_msg.channel, message=msg))

def sync_write_fifo(text):
    with open(MESSAGE_RELAY_FIFO_PATH, "w") as f:
        f.write(text)

def download_in_thread(bot: IrcBot, in_msg: Message, url: str):
    """Download a file in a thread."""


    def download_in_thread_target(song_url: str):
        err = None
        try:
            song = download_audio(song_url, MPD_FOLDER)
        except MaxFilesize:
            err = "That file is too big"
        except MaxAudioLength:
            err = "That audio is too long"
        except FailedToProcess:
            err = "That audio could not be processed"
        except FailedToDownload:
            err = "That audio could not be downloaded"
        except ExtensionNotAllowed:
            err = "That audio extension is not allowed"
        # TODO solve this error
        # except Exception as e:
        #     err = f"Error: {e}"
        if err:
            err = _reply_str(bot, in_msg, err)
            sync_write_fifo(f"[[{in_msg.channel}]] {err}")
            return

        onend_text = _reply_str(bot, in_msg, f"{Path(song).stem} has been added to the playlist")
        sync_write_fifo(f"[[{in_msg.channel}]] {onend_text}")
        # TODO add song to mpd queue


    threading.Thread(
        target=download_in_thread_target,
        args=(url,),
        daemon=True,
    ).start()

@utils.arg_command("song", "Info about the current song")
async def song(bot: IrcBot, args: re.Match, msg: Message):
    song = mpd_client.current_song()
    def format_data(k):
        if k == "duration":
            return str(datetime.timedelta(seconds=float(song[k].split('.')[0])))
        if k == "file":
            return Path(song[k]).stem
        return song[k]
    exclude_keys = ["last-modified", "id"]

    await reply(bot, msg, ", ".join(
        f"{k}: {format_data(k)}" for k in song if k not in exclude_keys
    ))

@utils.arg_command("add", "Add a song to the playlist", f"{PREFIX}add <youtube_link|audio_url>. You can also submit audios with dcc. You cannot enqueue more than {MAX_USER_QUEUE_LENGTH} audios.")
async def add(bot: IrcBot, args: re.Match, msg: Message):
    if not await is_identified(bot, msg.nick):
        await reply(bot, msg, "You cannot use this bot before you register your nick")
        return

    args = utils.m2list(args)
    if len(args) == 0:
        await reply(bot, msg, "You need to specify a song to add")
        return

    if len(args) > 1:
        await reply(bot, msg, "You can only add one song at a time")
        return

    song_url = args[0]
    if not song_url.startswith("http"):
        await reply(bot, msg, "That is not a valid url: " + song_url)
        return

    download_in_thread(bot, msg, song_url)
    await reply(bot, msg, "Downloading...")


@utils.arg_command("source", "Shows bot source code url")
async def source(bot: IrcBot, args: re.Match, msg: Message):
    await reply(bot, msg, "https://github.com/matheusfillipe/mpd_irc_bot")

async def onconnect(bot: IrcBot):
    async def message_handler(text):
        match = re.match(r"^\[\[([^\]]+)\]\] (.*)$", text)
        if match:
            channel, text = match.groups()
            logging.debug(f" Message relay server handler regex: {channel=}, {text=}")
            await bot.send_message(text, channel)
            return
        for channel in CHANNELS:
            logging.debug(f" Message relay server handler simple: {channel=}, {text=}")
            await bot.send_message(text, channel)

    async def mpd_player_handler():
        mpd_client.current_song()['file']
        await message_handler(f"Playing: {mpd_client.current_song()['file']}")

    async with trio.open_nursery() as nursery:
        nursery.start_soon(listen_loop, MESSAGE_RELAY_FIFO_PATH, message_handler)
        nursery.start_soon(loop_with_handler, mpd_player_handler)

utils.setHelpHeader("RADIO BOT COMMANDS")
utils.setHelpBottom("You can learn more about sonic pi at: https://sonic-pi.net/tutorial.html")

if __name__ == "__main__":
    utils.setLogging(LEVEL, LOGFILE)
    bot = IrcBot(HOST, PORT, NICK, CHANNELS, PASSWORD, use_ssl=PORT == 6697, dcc_host=DCC_HOST)
    bot.runWithCallback(onconnect)
