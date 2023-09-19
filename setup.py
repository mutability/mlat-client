#!/usr/bin/env python3

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

from setuptools import setup, Extension

CLIENT_VERSION = "unknown"
exec(open('mlat/client/version.py').read())

setup(name='MlatClient',
      description='Multilateration client package',
      version=CLIENT_VERSION,
      author='Oliver Jowett',
      author_email='oliver.jowett@flightaware.com',
      packages=['mlat', 'mlat.client', 'flightaware', 'flightaware.client'],
      ext_modules=[
          Extension('_modes',
                    include_dirs=["."],
                    sources=['_modes.c', 'modes_reader.c', 'modes_message.c', 'modes_crc.c'])],
      entry_points={
          'console_scripts': [
              'mlat-client = mlat.client.cli:main',
              'fa-mlat-client = flightaware.client.cli:main'
          ]}
      )
