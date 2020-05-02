import requests
import time
from datetime import datetime#
import os
import csv
import json
import environ
import discord
from discord.ext import tasks, commands
import asyncio

env = environ.Env()
env.read_env()

CLIENT_ID=env.ENVIRON['CLIENT_ID']
CLIENT_SECRET=env.ENVIRON['CLIENT_SECRET']
REFRESH_TOKEN=env.ENVIRON['REFRESH_TOKEN']
oauth_token=None
oauth_token_expires_at=0
DISCORD_TOKEN=env.ENVIRON['DISCORD_TOKEN']
DISCORD_CHANNEL=env.ENVIRON['DISCORD_CHANNEL']
DISCORD_GUILD=env.ENVIRON['DISCORD_GUILD']
tformat="%Y-%m-%dT%H:%M:%S"

# whitelisted tags that should appear in discord
LOG_TAGS=set(('Speedrun','Randomizer','Racing'))

cached_tags={}

def get_oauth_token():
    global oauth_token
    global oauth_token_expires_at
    if oauth_token_expires_at > time.time():
        return oauth_token
    r = requests.post('https://id.twitch.tv/oauth2/token',
        json={
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "grant_type": "refresh_token",
            "refresh_token": REFRESH_TOKEN,
        })
    if not r.status_code == 200:
        raise Exception("grabbing oauth token failed!")
    oauth_token = r.json()['access_token']
    oauth_token_expires_at = time.time() + 3500 # I think they are valid for an hour so 3500 is safe
    return oauth_token
    

def translate_tags(tag_ids):
    unknown = tag_ids - cached_tags.keys()
    if len(unknown) > 0:
        tags_str='&'.join(map(lambda x: 'tag_id={}'.format(x), unknown))
        tags=requests.get('https://api.twitch.tv/helix/tags/streams?'+tags_str,
                    headers={'Client-ID': CLIENT_ID, 'Authorization': get_oauth_token()}).json()
        for tag in tags['data']:
            cached_tags[tag['tag_id']]=tag['localization_names']['en-us']
    return list(map(lambda x: cached_tags[x], tag_ids))

stream_infos=["_id", "game", "broadcast_platform", "viewers", "video_height",
    "average_fps", "delay", "created_at", "is_playlist", "stream_type"]
channel_infos=["mature", "status", "broadcaster_language", "broadcaster_software", "display_name", "game",
        "language", "_id", "name", "created_at", "updated_at", "partner", "views", "followers", "broadcaster_type",
        "description", "private_video", "privacy_options_enabled"]

class Stream:
    def __init__(self, raw_data):
        for s in stream_infos:
            self.__setattr__(s, raw_data[s])
        for s in channel_infos:
            self.__setattr__('channel_'+s, raw_data['channel'][s])
        self.tags=[]
    
    def to_row(self):
        row=[]
        for s in stream_infos:
            row.append(self.__getattribute__(s))
        for s in channel_infos:
            row.append(self.__getattribute__('channel_'+s))
        row.append(";;;;".join(self.tags))
        return row
    
    def to_embed(self):
        embed=discord.Embed(title="new stream Pog", url='https://twitch.tv/'+self.channel_name)
        embed.add_field(name='channel',value=self.channel_name)
        embed.add_field(name='title',value=self.channel_status)
        if self.channel_game:
            embed.add_field(name='game',value=self.channel_game)
        return embed

def get_bingo_streams(already_seen_streams):
    streams = requests.get("https://api.twitch.tv/kraken/search/streams?query=bingo&limit=100", headers={"Accept": 'application/vnd.twitchtv.v5+json', 'Client-ID': CLIENT_ID}).json()
    allstreams=(Stream(s) for s in streams['streams'] if 'bingo' in s['channel']['status'].lower())
    allstreams=[x for x in allstreams if not (x.channel__id, x.channel_status) in already_seen_streams]
    # print(f"streams: {len(allstreams)}")
    if len(allstreams) > 0:
        helixstreams = requests.get("https://api.twitch.tv/helix/streams?"+'&'.join(map(lambda x: 'user_id={}'.format(x.channel__id), allstreams)),
                        headers={'Client-ID': CLIENT_ID, 'Authorization': get_oauth_token()}).json()
        tagdict=dict((int(x['user_id']),x['tag_ids']) for x in helixstreams['data'])
        for stream in allstreams:
            already_seen_streams.add((stream.channel__id, stream.channel_status))
            stream.tags = translate_tags(tagdict.get(stream.channel__id, []))
        # print(f"cached tags: {len(tagdict)}")
    return allstreams

def log_streams(allstreams):
    if not os.path.isfile("bingolog.txt"):
        with open("bingolog.txt",'w', encoding="UTF-8") as f:
            csv.writer(f).writerow(stream_infos+list(map(lambda x: 'channel_'+x, channel_infos))+["tags"])
    with open("bingolog.txt",'a',encoding="UTF-8") as f:
        writer=csv.writer(f)
        for stream in allstreams:
            if not '!bingo' in stream.channel_status.lower():
                writer.writerow(stream.to_row())
    # print("logfile written")

class BingoStreams(discord.Client):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # create the background task and run it in the background
        self.bg_task = self.loop.create_task(self.log_bingo_streams())

    async def log_bingo_streams(self):
        await self.wait_until_ready()
        already_seen_streams=set()
        channel = self.get_channel(int(DISCORD_CHANNEL)) # channel ID goes here
        if not channel:
            # print('channel not found!')
            return
        while not self.is_closed():
            try:
                # print('logging streams...')
                new_streams=get_bingo_streams(already_seen_streams)
                log_streams(new_streams)
                for stream in new_streams:
                    # low effort non bingo filter
                    if not '!bingo' in stream.channel_status.lower():
                        await channel.send(embed=stream.to_embed())
                        # print(stream.to_row())
                # print('logged streams')
                await asyncio.sleep(5 * 60) # task runs every 5 minutes
            # print("exit cause closed")
            except asyncio.CancelledError:
                return
            except Exception as e:
                with open('error.log','a') as f:
                   f.write("error sending to discord:\n")
                   f.write(repr(e)+"\n")
            # print("error sending to discord:")
            # print(e)

def startup_discord():
    client = BingoStreams()
    # @client.event
    # async def on_ready():
    #     print('We have logged in as {0.user}'.format(client))
    client.run(DISCORD_TOKEN)

async def file_log_only():
    already_seen_streams=set()
    while True:
        try:
            print('logging streams...')
            new_streams=get_bingo_streams(already_seen_streams)
            log_streams(new_streams)
            await asyncio.sleep(5 * 60) # task runs every 5 minutes
            # print("exit cause closed")
        except asyncio.CancelledError:
            return
        except Exception as e:
            with open('error.log','a') as f:
                f.write(repr(e)+"\n")
            # print("error sending to discord:")
            print(e)

# startup_discord()
asyncio.run(file_log_only())