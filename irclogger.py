import errno
import json
import logging
import os
import sys
import threading

import irc.bot
import irc.client
from jaraco.stream import buffer
from more_itertools import consume, repeatfunc

logging.basicConfig()
logging.getLogger().setLevel(logging.DEBUG)

FIFO = '/tmp/to_irc'
event = threading.Event()

incoming_messages_json = ''


class ReactorWithEvent(irc.client.Reactor):
    def process_once(self, timeout=0):
        super().process_once(timeout)
        global incoming_messages_json
        if event.is_set():
            pass
        else:
            logging.debug("reactor: blocking")
            event.clear()
            messages = incoming_messages_json
            for m in messages:
                message = '[{0}] {1}'.format(
                    m['nickname'], m['content'])
                if m['command'] == 'PRIVMSG':
                    self.connections[0].privmsg(m['channel'], message)
                elif m['command'] == 'NOTICE':
                    self.connections[0].notice(m['channel'], message)
            logging.debug("reactor: clearing")
            event.set()


class IRCLogger(irc.bot.SingleServerIRCBot):

    def __init__(self, target):
        irc.bot.SingleServerIRCBot.__init__(
            self, [("irc.atw-inter.net", 6667)], "maobot_test", "maobot_test",)
        self.reactor = ReactorWithEvent()
        self.connection = self.reactor.server()
        self.reactor.add_global_handler("all_events", self._dispatcher, -10)
        self.reactor.add_global_handler(
            "dcc_disconnect", self._dcc_disconnect, -10)
        self.channel = "#maobot_test"
        self.connection.buffer_class.encoding = "iso-2022-jp"
        self.connection.transmit_encoding = 'iso-2022-jp'

    def on_welcome(self, connection, event):
        if irc.client.is_channel(self.channel):
            connection.join(self.channel)

    def on_disconnect(self, connection, event):
        self.connection.quit("Using irc.client.py")
        sys.exit(0)


def ircbot():
    global incoming_messages_json
    c = IRCLogger("#maobot_test")

    c.start()


def wait_records():
    global incoming_messages_json
    logging.debug("records: clearing")
    event.set()
    while True:
        messages = os.listdir('/tmp/messages')
        if len(messages) > 0:
            logging.debug("records: blocking")
            event.clear()
            message = '/tmp/messages/{0}'.format(
                sorted(messages, key=lambda x: int(x))[0])
            with open(message, 'r') as m:
                incoming_messages_json = json.load(m)
            os.remove(message)
            logging.debug("records: wait")
            event.wait()
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
