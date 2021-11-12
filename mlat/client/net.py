# -*- mode: python; indent-tabs-mode: nil -*-

# Part of mlat-client - an ADS-B multilateration client.
# Copyright 2015, Oliver Jowett <oliver@mutability.co.uk>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

"""
Common networking bits, based on asyncore
"""

import sys
import socket
import asyncore
from mlat.client.util import log, log_exc, monotonic_time

import random
random.seed()

__all__ = ('LoggingMixin', 'ReconnectingConnection')


class LoggingMixin:
    """A mixin that redirects asyncore's logging to the client's
    global logging."""

    def log(self, message):
        log('{0}', message)

    def log_info(self, message, type='info'):
        log('{0}: {1}', message, type)


class ReconnectingConnection(LoggingMixin, asyncore.dispatcher):
    """
    An asyncore connection that maintains a TCP connection to a particular
    host/port, reconnecting on connection loss.
    """

    reconnect_interval = 10.0

    def __init__(self, host, port):
        asyncore.dispatcher.__init__(self)
        self.host = host
        self.basePort = port
        self.port = port
        self.adsbexchangePorts = [ 31090, 64590 ]
        if self.host == 'feed.adsbexchange.com' and self.basePort == 31090:
            self.port = 64590
        self.addrlist = []
        self.state = 'disconnected'
        self.reconnect_at = None

    def heartbeat(self, now):
        if self.reconnect_at is None or self.reconnect_at > now:
            return
        if self.state == 'ready':
            return
        self.reconnect_at = None
        self.reconnect()

    def close(self, manual_close=False):
        try:
            asyncore.dispatcher.close(self)
        except AttributeError:
            # blarg, try to eat asyncore bugs
            pass

        if self.state != 'disconnected':
            if not manual_close:
                log('Lost connection to {host}:{port}', host=self.host, port=self.port)

            self.state = 'disconnected'
            self.reset_connection()
            self.lost_connection()

        if not manual_close:
            self.schedule_reconnect()

    def disconnect(self, reason):
        if self.state != 'disconnected':
            log('Disconnecting from {host}:{port}: {reason}', host=self.host, port=self.port, reason=reason)
            self.close(True)

    def writable(self):
        return self.connecting

    def schedule_reconnect(self):
        if self.reconnect_at is None:
            if len(self.addrlist) > 0:
                # we still have more addresses to try
                # nb: asyncore breaks in odd ways if you try
                # to reconnect immediately at this point
                # (pending events for the old socket go to
                # the new socket) so do it in 0.5s time
                # so the caller can clean up the old
                # socket and discard the events.
                interval = 0.5
            else:
                interval = self.reconnect_interval + 5 * random.random()

            log('Reconnecting in {seconds:.1f} seconds'.format(seconds=interval))
            self.reconnect_at = monotonic_time() + interval

    def refresh_address_list(self):
        self.address

    def reconnect(self):
        if self.state != 'disconnected':
            self.disconnect('About to reconnect')

        try:
            self.reset_connection()

            if len(self.addrlist) == 0:
                # ran out of addresses to try, resolve it again
                if self.host == 'feed.adsbexchange.com' and self.basePort == 31090:
                    for index, port in enumerate(self.adsbexchangePorts):
                        if self.port == port:
                            self.port = self.adsbexchangePorts[(index + 1) % len(self.adsbexchangePorts)]
                            break

                #if self.host == 'feed.adsbexchange.com' and self.basePort != self.port:
                #    log('Connecting to {host}:{port} (trying hard-coded alternate port for adsbexchange)', host=self.host, port=self.port)
                #else:
                #    log('Connecting to {host}:{port}', host=self.host, port=self.port)

                self.addrlist = socket.getaddrinfo(host=self.host,
                                                   port=self.port,
                                                   family=socket.AF_UNSPEC,
                                                   type=socket.SOCK_STREAM,
                                                   proto=0,
                                                   flags=0)

            # try the next available address
            a_family, a_type, a_proto, a_canonname, a_sockaddr = self.addrlist[0]
            del self.addrlist[0]

            self.create_socket(a_family, a_type)
            self.connect(a_sockaddr)
        except socket.error as e:
            log('Connection to {host}:{port} failed: {ex!s}', host=self.host, port=self.port, ex=e)
            self.close()

    def handle_connect(self):
        self.state = 'connected'
        self.addrlist = []  # connect was OK, re-resolve next time
        self.start_connection()

    def handle_read(self):
        pass

    def handle_write(self):
        pass

    def handle_close(self):
        self.close()

    def handle_error(self):
        t, v, tb = sys.exc_info()
        if isinstance(v, IOError):
            log('Connection to {host}:{port} lost: {ex!s}',
                host=self.host,
                port=self.port,
                ex=v)
        else:
            log_exc('Unexpected exception on connection to {host}:{port}',
                    host=self.host,
                    port=self.port)

        self.handle_close()

    def reset_connection(self):
        pass

    def start_connection(self):
        pass

    def lost_connection(self):
        pass
