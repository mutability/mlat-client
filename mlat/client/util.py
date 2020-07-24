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


__all__ = ('log', 'log_exc', 'monotonic_time')


suppress_log_timestamps = True

def log(msg, *args, **kwargs):
    if suppress_log_timestamps:
        print(msg.format(*args, **kwargs), file=sys.stderr)
    else:
        print(time.ctime(), msg.format(*args, **kwargs), file=sys.stderr)
    sys.stderr.flush()


def log_exc(msg, *args, **kwargs):
    if suppress_log_timestamps:
        print(msg.format(*args, **kwargs), file=sys.stderr)
    else:
        print(time.ctime(), msg.format(*args, **kwargs), file=sys.stderr)
    traceback.print_exc(file=sys.stderr)
    sys.stderr.flush()


_adjust = 0
_last = 0


def monotonic_time():
    """Emulates time.monotonic() if not available."""
    global _adjust, _last

    now = time.time()
    if now < _last:
        # system clock went backwards, add in a
        # fudge factor so our monotonic clock
        # does not.
        _adjust = _adjust + (_last - now)

    _last = now
    return now + _adjust


try:
    # try to use the 3.3+ version when available
    from time import monotonic as monotonic_time  # noqa
except ImportError:
    pass
