# mlat-client

This is a client that selectively forwards Mode S messages to a
server that resolves the transmitter position by multilateration of the same
message received by multiple clients.

The server code is not yet released so the client on its own is not hugely
useful. Patience..

## Building

To build a Debian (or Ubuntu, Raspbian, etc) package that includes config
and startup scripts:

    $ dpkg-buildpackage -b

To build/install on other systems using setuptools (client only):

    $ ./setup.py install

## Running

Please [contact Oliver](mailto:oliver@mutability.co.uk) before connecting with
this client as the server is - at best - experimental.

## License

Copyright 2015, [Oliver Jowett](mailto:oliver@mutability.co.uk).

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received [a copy of the GNU General Public License](COPYING)
along with this program.  If not, see <http://www.gnu.org/licenses/>.
