#!/usr/bin/env python3

#   build/install the _modes module:
#    $ python3 ./setup.py install --user
#
#   run the script, passing hostnames for 2 or more receivers:
#    $ ./compare-message-timing.py host1 host2
#
#   output should start after around 20 seconds;
#   the output format is TSV with a pair of columns for each
#   receiver: time (seconds) and offset from the first receiver time
#   (nanoseconds)

import sys
import traceback
import asyncio
import concurrent.futures

import _modes

class Receiver(object):
    def __init__(self, *, loop, id, host, port, correlator):
        self.loop = loop
        self.id = id
        self.host = host
        self.port = port

        self.parser = _modes.Reader(_modes.BEAST)
        self.parser.default_filter = [False] * 32
        self.parser.default_filter[17] = True

        self.frequency = None
        self.task = asyncio.async(self.handle_connection())
        self.correlator = correlator

    def __str__(self):
        return 'client #{0} ({1}:{2})'.format(self.id, self.host, self.port)

    @asyncio.coroutine
    def handle_connection(self):
        try:
            reader, writer = yield from asyncio.open_connection(self.host, self.port)
            print(self, 'connected', file=sys.stderr)

            # Binary format, no filters, CRC checks enabled, mode A/C disabled
            writer.write(b'\x1a1C\x1a1d\x1a1f\x1a1j')

            self.loop.call_later(5.0, self.set_default_freq)

            data = b''
            while True:
                moredata = yield from reader.read(4096)
                if len(moredata) == 0:
                    break

                data += moredata
                consumed, messages, pending_error = self.parser.feed(data)
                data = data[consumed:]
                self.handle_messages(messages)

                if pending_error:
                    self.parser.feed(self.data)

            print(self, 'connection closed', file=sys.stderr)
            writer.close()
        except concurrent.futures.CancelledError:
            pass
        except Exception:
            print(self, 'unexpected exception', file=sys.stderr)
            traceback.print_exc(file=sys.stderr)

    def set_default_freq(self):
        if self.frequency is None:
            print(self, 'assuming 12MHz clock frequency', file=sys.stderr)
            self.frequency = 12e6

    def handle_messages(self, messages):
        for message in messages:
            if message.df == _modes.DF_EVENT_MODE_CHANGE:
                self.frequency = message.eventdata['frequency']
                print(self, 'clock frequency changed to {0:.0f}MHz'.format(self.frequency/1e6), file=sys.stderr)
            elif message.df == 17 and (message.even_cpr or message.odd_cpr) and self.frequency is not None:
                self.correlator(self.id, self.frequency, message)


class Correlator(object):
    def __init__(self, loop):
        self.loop = loop
        self.pending = {}
        self.clients = set()
        self.sorted_clients = []
        self.base_times = None

    def add_client(self, client_id):
        self.clients.add(client_id)
        self.sorted_clients = sorted(self.clients)

    def correlate(self, client_id, frequency, message):
        if message in self.pending:
            self.pending[message].append((client_id, frequency, message))
        else:
            self.pending[message] = [ (client_id, frequency, message) ]
            self.loop.call_later(10.0, self.resolve, message)

    def resolve(self, message):
        copies = self.pending.pop(message)
        client_times = {}
        for client_id, frequency, message_copy in copies:
            if client_id in client_times:
                # occurs multiple times, ambiguous, skip it
                return
            client_times[client_id] = float(message_copy.timestamp) / frequency

        if set(client_times.keys()) == self.clients:
            # all clients saw this message

            if self.base_times is None:
                # first matching message, record baseline timestamps
                self.base_times = client_times

            line = ''
            ref_time = client_times[self.sorted_clients[0]] - self.base_times[self.sorted_clients[0]]
            for client_id in self.sorted_clients:
                this_time = client_times[client_id] - self.base_times[client_id]
                offset = this_time - ref_time
                line += '{t:.9f}\t{o:.0f}\t'.format(t = this_time, o = offset * 1e9)
            print(line[:-1])
            sys.stdout.flush()


if __name__ == '__main__':
    if len(sys.argv) < 3:
        print('usage: {0} host1 host2 ...'.format(sys.argv[0]))
        sys.exit(1)

    loop = asyncio.get_event_loop()
    correlator = Correlator(loop)

    clients = []
    for i in range(1, len(sys.argv)):
        client_id = str(i)
        clients.append(Receiver(loop = loop, id = client_id, host = sys.argv[i], port = 30005, correlator = correlator.correlate))
        correlator.add_client(client_id)

    tasks = [client.task for client in clients]
    try:
        done, pending = loop.run_until_complete(asyncio.wait(tasks, return_when = asyncio.FIRST_COMPLETED))
    except KeyboardInterrupt:
        pending = tasks

    for task in pending:
        task.cancel()
    loop.run_until_complete(asyncio.gather(*pending))

    loop.close()
