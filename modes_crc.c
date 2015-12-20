/*
 * Part of mlat-client - an ADS-B multilateration client.
 * Copyright 2015, Oliver Jowett <oliver@mutability.co.uk>
 *
 * This program is free software: you can redistribute it and/or modify
 * it under the terms of the GNU General Public License as published by
 *  the Free Software Foundation, either version 3 of the License, or
 * (at your option) any later version.
 *
 * This program is distributed in the hope that it will be useful,
 * but WITHOUT ANY WARRANTY; without even the implied warranty of
 * MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
 * GNU General Public License for more details.
 *
 * You should have received a copy of the GNU General Public License
 * along with this program.  If not, see <http://www.gnu.org/licenses/>.
 */

#include "_modes.h"


/********** CRC ****************/

/* Generator polynomial for the Mode S CRC */
#define MODES_GENERATOR_POLY 0xfff409U

/* CRC values for all single-byte messages; used to speed up CRC calculation. */
static uint32_t crc_table[256];

int modescrc_module_init(PyObject *m)
{
    int i;

    for (i = 0; i < 256; ++i) {
        uint32_t c = i << 16;
        int j;
        for (j = 0; j < 8; ++j) {
            if (c & 0x800000)
                c = (c<<1) ^ MODES_GENERATOR_POLY;
            else
                c = (c<<1);
        }

        crc_table[i] = c & 0x00ffffff;
    }

    return 0;
}

void modescrc_module_free(PyObject *m)
{
}

uint32_t modescrc_buffer_crc(uint8_t *buf, Py_ssize_t len)
{
    uint32_t rem;
    Py_ssize_t i;
    for (rem = 0, i = len; i > 0; --i) {
        rem = ((rem & 0x00ffff) << 8) ^ crc_table[*buf++ ^ ((rem & 0xff0000) >> 16)];
    }

    return rem;
}

PyObject *modescrc_crc(PyObject *self, PyObject *args)
{
    Py_buffer buffer;
    PyObject *rv = NULL;

    if (!PyArg_ParseTuple(args, "s*", &buffer))
        return NULL;

    if (buffer.itemsize != 1) {
        PyErr_SetString(PyExc_ValueError, "buffer itemsize is not 1");
        goto out;
    }

    if (!PyBuffer_IsContiguous(&buffer, 'C')) {
        PyErr_SetString(PyExc_ValueError, "buffer is not contiguous");
        goto out;
    }

    rv = PyLong_FromLong(modescrc_buffer_crc(buffer.buf, buffer.len));

 out:
    PyBuffer_Release(&buffer);
    return rv;
}
