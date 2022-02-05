import errno
import json
import logging
import os
import sys
import threading
from typing import List

import boto3
import dotenv
import irc.bot
import irc.client
from boto3.session import Session as AWSSession
from gql_py import Gql
from requests import Session
from requests_aws4auth import AWS4Auth

logging.basicConfig()
logging.getLogger().setLevel(logging.DEBUG)

dotenv.load_dotenv()

FIFO = "/tmp/to_irc"
incoming_event = threading.Event()
incoming_messages_json = ""


class ReactorWithEvent(irc.client.Reactor):
    def process_once(self, timeout=0):
        super().process_once(timeout)
        global incoming_messages_json
        if incoming_event.is_set():
            pass
        else:
            logging.debug("reactor: blocking")
            incoming_event.clear()
            messages = incoming_messages_json
            for m in messages:
                if m:
                    message = "[{0}] {1}".format(m["nickname"], m["content"])
                    if m["command"] == "PRIVMSG":
                        self.connections[0].privmsg(m["channel"], message)
                    elif m["command"] == "NOTICE":
                        self.connections[0].notice(m["channel"], message)
            logging.debug("reactor: clearing")
            incoming_event.set()


class API:
    def __init__(self) -> None:
        aws = AWSSession(
            aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID"),
            aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY"),
        )
        credentials = aws.get_credentials().get_frozen_credentials()

        session = Session()
        session.auth = AWS4Auth(
            credentials.access_key,
            credentials.secret_key,
            aws.region_name,
            "appsync",
            session_token=credentials.token,
        )
        session.headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

        self.api = Gql(api=os.getenv("API_URL"), session=session)
        self.user_pool_id = os.getenv("USER_POOL_ID")
        if not self.user_pool_id:
            raise

    def send_post(self, nickname, channel, content, command) -> None:
        cognito = boto3.client("cognito-idp")
        users = cognito.list_users(
            UserPoolId=self.user_pool_id, AttributesToGet=["nickname"]
        )["Users"]
        nickname_userid_map = {u["Attributes"][0]
                               ["Value"]: u["Username"] for u in users}
        owner_id = nickname_userid_map[nickname.replace("_", "")]
        query = """
        mutation ($channel: String!, $content: String!,
                  $nickname: String, $owner: String, $command: Command) {
            createPost(input: {channel: $channel, command: $command,
                                content: $content, from: IRC,
                                nickname: $nickname, owner: $owner}) {
                id
            }
        }

        """

        variables = {"channel": channel, "content": content,
                     "nickname": nickname, "owner": owner_id,
                     "command": command}
        res = self.api.send(query=query, variables=variables)
        logging.debug(res)

    def get_channels(self) -> List[str]:
        query = """
            query ListChannels {
                listChannels {
                    items {
                        name
                    }
                }
            }
        """
        response = self.api.send(query=query)
        items = response.data['listChannels']['items']
        return list(map(lambda c: c['name'], items))


class IRCLogger(irc.bot.SingleServerIRCBot):
    def __init__(self, server, nickname,  port=6667,
                 encoding="iso-2022-jp"):
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
        nickname = irc.client.NickMask(event.source).nick.replace("_", "")
        channel = event.target
        content = event.arguments[0]

        self.api.send_post(nickname=nickname, channel=channel,
                           content=content, command="PRIVMSG")

    def on_pubnotice(self, connection, event):
        nickname = irc.client.NickMask(event.source).nick.replace("_", "")
        channel = event.target
        content = event.arguments[0]

        self.api.send_post(nickname=nickname, channel=channel,
                           content=content, command="NOTICE")


def ircbot():
    global incoming_messages_json
    c = IRCLogger(
        server=os.getenv("IRC_SERVER"),
        nickname=os.getenv("BOT_NICKNAME", "maobot")
    )

    c.start()


def wait_records():
    global incoming_messages_json
    logging.debug("records: clearing")
    incoming_event.set()
    while True:
        messages = os.listdir("/tmp/messages")
        if len(messages) > 0:
            logging.debug("records: blocking")
            incoming_event.clear()
            message = "/tmp/messages/{0}".format(
                sorted(messages, key=lambda x: int(x))[0]
            )
            with open(message, "r") as m:
                incoming_messages_json = json.load(m)
            os.remove(message)
            logging.debug("records: wait")
            incoming_event.wait()
            logging.debug("records: continuing")


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
