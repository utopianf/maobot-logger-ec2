import asyncio
import errno
import json
import logging
import os
import re
import sys
import threading
import base64
import time
from typing import List
from urllib.parse import urlparse

import boto3
import irc.bot
import irc.client
from boto3.session import Session as AWSSession
from gql import gql, Client
from gql.transport.aiohttp import AIOHTTPTransport
from gql.transport.appsync_auth import AppSyncIAMAuthentication

from dotenv import load_dotenv
load_dotenv()

fmt = "%(asctime)s %(levelname)s %(name)s: %(message)s"
logging.basicConfig(format=fmt)
logging.getLogger().setLevel(logging.INFO)

FIFO = "/tmp/to_irc"
incoming_event = threading.Event()
incoming_messages_json = ""


class ISO2022JPConnection(irc.client.ServerConnection):
    def encode(self, msg):
        return msg.encode(self.transmit_encoding, 'namereplace')


class ReactorWithEvent(irc.client.Reactor):
    connection_class = ISO2022JPConnection

    def process_once(self, timeout=0):
        super().process_once(timeout)
        global incoming_messages_json
        if incoming_event.is_set():
            pass
        else:
            logging.debug("reactor: blocking (event != set)")
            if incoming_messages_json is not None:
                messages = incoming_messages_json
                for m in messages:
                    if m:
                        content = re.sub('\r\n|\n|\r', ' ', m["content"].replace('\0', ''))
                        message = "[{0}] {1}".format(
                            m["nickname"], content)
                        logging.info("From LOG To {0} ({1}): {2}".format(
                            m["channel"], m["command"], message))
                        self.post_IRC(m["command"], m["channel"], message)
            logging.debug("reactor: clearing (event.set)")
            incoming_event.set()

    def post_IRC(self, command, channel, message):
        try:
            if command == "PRIVMSG":
                self.connections[0].privmsg(
                    channel, message)
            elif command == "NOTICE":
                self.connections[0].notice(
                    channel, message)
            time.sleep(1)
        except irc.client.MessageTooLong:
            message1 = message[:len(message)//2]
            message2 = message[len(message)//2:]
            self.post_IRC(command, channel, message1)
            self.post_IRC(command, channel, message2)

class API:
    def __init__(self) -> None:
        print(os.getenv("AWS_ACCESS_KEY_ID"))
        aws = AWSSession(
            aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID"),
            aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY"),
        )
        credentials = aws.get_credentials().get_frozen_credentials()

        auth = AppSyncIAMAuthentication(
            host=str(urlparse(os.getenv("API_URL")).netloc),
            credentials=credentials,
            region_name=aws.region_name
        )
        self.transport = AIOHTTPTransport(
            url=os.getenv("API_URL"),
            auth=auth
        )

        self.user_pool_id = os.getenv("USER_POOL_ID")
        if not self.user_pool_id:
            raise

    async def send_post(self, nickname, channel, content, command) -> None:
        cognito = boto3.client("cognito-idp")
        users = cognito.list_users(
            UserPoolId=self.user_pool_id, AttributesToGet=["nickname"]
        )["Users"]
        nickname_userid_map = {}
        for u in users:
            nicknames = u["Attributes"][0]["Value"].split(",")
            for n in nicknames:
                nickname_userid_map[n] = u["Username"]
        _nickname = re.sub('_+$', '', nickname)
        if _nickname not in nickname_userid_map:
            owner_id = nickname_userid_map["maobot"]
        else:
            owner_id = nickname_userid_map[_nickname]

        async with Client(
            transport=self.transport, fetch_schema_from_transport=False
        ) as session:
            query = gql("""
            mutation ($channel: String!, $content: String!,
                    $nickname: String, $owner: String, $command: Command) {
                createPost(input: {channel: $channel, command: $command,
                                    content: $content, from: IRC,
                                    nickname: $nickname, owner: $owner}) {
                    id
                }
            }

        """)

            variables = {"channel": channel, "content": content,
                         "nickname": _nickname, "owner": owner_id,
                         "command": command}
            res = await session.execute(query, variable_values=variables)
            logging.debug(res)

    def get_channels(self) -> List[str]:
        client = Client(
            transport=self.transport, fetch_schema_from_transport=True
        )

        query = gql("""
            query ListChannels {
                listChannels {
                    items {
                        name
                    }
                }
            }
        """)
        response = client.execute(query)
        items = response['listChannels']['items']
        return list(map(lambda c: c['name'], items))


class IRCLogger(irc.bot.SingleServerIRCBot):
    def __init__(self, server, nickname,  port=6667,
                 encoding="iso-2022-jp-ext"):
        irc.bot.SingleServerIRCBot.__init__(
            self,
            [(server, port)],
            nickname,
            nickname,
        )

        self.reactor = ReactorWithEvent()
        self.connection = self.reactor.server()
        self.reactor.add_global_handler("all_events", self._dispatcher, -10)
        self.reactor.add_global_handler(
            "dcc_disconnect", self._dcc_disconnect, -10)

        self.connection.buffer_class.encoding = encoding
        self.connection.buffer_class.errors = "replace"
        self.connection.transmit_encoding = encoding

        self.api = API()
        self.channels = self.api.get_channels()

    def on_welcome(self, connection, event):
        for channel in self.channels:
            if irc.client.is_channel(channel):
                connection.join(channel)

    def on_disconnect(self, connection, event):
        self.connection.quit("Using irc.client.py")
        sys.exit(0)

    def on_pubmsg(self, connection, event):
        nickname = irc.client.NickMask(event.source).nick
        if not re.search(r"^[a-zA-Z][a-zA-Z0-9_]*$", nickname):
            return
        channel = event.target
        content = event.arguments[0]
        logging.info("From {0} To LOG (PRIVMSG, {1}): {2}".format(
            channel, nickname, content))
        asyncio.run(self.api.send_post(nickname=nickname, channel=channel,
                                       content=content, command="PRIVMSG"))

    def on_pubnotice(self, connection, event):
        nickname = irc.client.NickMask(event.source).nick
        if not re.search(r"^[a-zA-Z][a-zA-Z0-9_]*$", nickname):
            return
        channel = event.target
        content = event.arguments[0]
        logging.info("From {0} To LOG (NOTICE, {1}): {2}".format(
            channel, nickname, content))
        asyncio.run(self.api.send_post(nickname=nickname, channel=channel,
                                       content=content, command="NOTICE"))


def ircbot():
    global incoming_messages_json
    c = IRCLogger(
        server=os.getenv("IRC_SERVER"),
        nickname=os.getenv("BOT_NICKNAME", "maobot")
    )

    c.start()


def wait_records():
    global incoming_messages_json
    logging.debug("records: clearing (event.set)")
    incoming_event.set()
    while True:
        messages = os.listdir("/tmp/messages")
        if len(messages) > 0:
            message = "/tmp/messages/{0}".format(
                sorted(messages, key=lambda x: int(x))[0]
            )
            with open(message, "r") as m:
                try:
                    base64_m = m.read()
                    json_str = base64.b64decode(base64_m).decode()
                    incoming_messages_json = json.loads(json_str)
                except json.JSONDecodeError:
                    logging.debug("record ERROR")
                    incoming_messages_json = None
            os.remove(message)
            logging.debug("records: blocking (event.clear)")
            incoming_event.clear()
            logging.debug("records: wait (event.wait)")
            incoming_event.wait()
            logging.debug("records: continuing (event == set)")


if __name__ == "__main__":
    try:
        os.mkdir("/tmp/messages")

    except OSError as oe:
        if oe.errno != errno.EEXIST:
            raise
    ircbot_thread = threading.Thread(target=ircbot)
    ircbot_thread.start()

    wait_records_thread = threading.Thread(target=wait_records)
    wait_records_thread.start()
