# mlat-client

This is a client that selectively forwards Mode S messages to a
server that resolves the transmitter position by multilateration of the same
message received by multiple clients.

The corresponding server code is available at
https://github.com/mutability/mlat-server.

There is also support for running in a mode used to feed multilateration
information to FlightAware via piaware. In this mode, the client is started
automatically by piaware.

## Building

To build a Debian (or Ubuntu, Raspbian, etc) package that includes config
and startup scripts:

    $ sudo apt-get install build-essential debhelper python3-dev
    $ dpkg-buildpackage -b -uc

This will build a .deb package in the parent directory. Install it with dpkg:

    $ sudo dpkg -i ../mlat-client_(version)_(architecture).deb

To build/install on other systems using setuptools (client only):

    $ ./setup.py install

## Running

If you are using this with piaware, you don't need to do anything special
other than to make sure that fa-mlat-client is available on your $PATH.
piaware will detect the presence of the client and start it when needed.

If you are connecting to a third party multilateration server, contact the
server's administrator for configuration instructions.

## Supported receivers

* Anything that produces Beast-format output with a 12MHz clock:
 * dump1090_mr, dump1090-mutability, FlightAware's dump1090
 * modesdeco (probably?)
 * an actual Mode-S Beast
 * airspy_adsb in Beast output mode
* SBS receivers
* Radarcape in 12MHz mode
* Radarcape in GPS mode

## Unsupported receivers

* The FlightRadar24 radarcape-based receiver. This produces a deliberately
crippled timestamp in its output, making it useless for multilateration.
If you have one of these, you should ask FR24 to fix this.

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
