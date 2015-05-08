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

    reconnect_interval = 30.0

    def __init__(self, host, port):
        asyncore.dispatcher.__init__(self)
        self.host = host
        self.port = port
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
        asyncore.dispatcher.close(self)

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
            log('Reconnecting in {0} seconds', self.reconnect_interval)
            self.reconnect_at = monotonic_time() + self.reconnect_interval

    def reconnect(self):
        if self.state != 'disconnected':
            self.disconnect('About to reconnect')

        try:
            self.reset_connection()
            self.create_socket(socket.AF_INET, socket.SOCK_STREAM)
            self.connect((self.host, self.port))
        except socket.error as e:
            log('Connection to {host}:{port} failed: {ex!s}', host=self.host, port=self.port, ex=e)
            self.close()

    def handle_connect(self):
        self.state = 'connected'
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
