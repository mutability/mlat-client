#!/usr/bin/python3

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

from distutils.core import setup, Extension

modes_ext = Extension('_modes', sources=['_modes.c'])

setup(name='MlatClient',
      version='0.1.12~dev',
      description='Multilateration client package',
      author='Oliver Jowett',
      author_email='oliver@mutability.co.uk',
      packages=['mlat', 'mlat.client'],
      ext_modules=[modes_ext],
      scripts=['mlat-client'])
