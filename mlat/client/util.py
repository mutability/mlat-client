# -*- python -*-

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

import sys
import time
import traceback


__all__ = ('log', 'log_exc', 'LoggingMixin')


def log(msg, *args, **kwargs):
    print >>sys.stderr, time.ctime(), msg.format(*args, **kwargs)


def log_exc(msg, *args, **kwargs):
    print >>sys.stderr, time.ctime(), msg.format(*args, **kwargs)
    traceback.print_exc(sys.stderr)


class LoggingMixin:
    """A mixin that redirects asyncore's logging to the client's
    global logging."""

    def log(self, message):
        log('{0}', message)

    def log_info(self, message, type='info'):
        log('{0}: {1}', message, type)
