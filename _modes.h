#ifndef _MODES_H
#define _MODES_H

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

#include <Python.h>
#include <structmember.h>

#include <stdint.h>

/* a modesmessage object */
typedef struct {
    PyObject_HEAD

    unsigned long long timestamp;
    unsigned int signal;

    unsigned int df;
    unsigned int nuc;
    char even_cpr;
    char odd_cpr;
    char valid;
    PyObject *crc;
    PyObject *address;
    PyObject *altitude;

    uint8_t *data;
    int datalen;

    PyObject *eventdata;
} modesmessage;

/* special DF types for non-Mode-S messages */
#define DF_MODEAC 32
#define DF_EVENT_TIMESTAMP_JUMP 33
#define DF_EVENT_MODE_CHANGE 34
#define DF_EVENT_EPOCH_ROLLOVER 35
#define DF_EVENT_RADARCAPE_STATUS 36
#define DF_EVENT_RADARCAPE_POSITION 37

/* factory function to build a modesmessage from a provided buffer */
PyObject *modesmessage_from_buffer(unsigned long long timestamp, unsigned signal, uint8_t *data, int datalen);
/* factory function to build an event message */
PyObject *modesmessage_new_eventmessage(int type, unsigned long long timestamp, PyObject *eventdata);
/* python entry point */
PyObject *modesmessage_eventmessage(PyObject *self, PyObject *args, PyObject *kwds);

/* crc helpers */
uint32_t modescrc_buffer_crc(uint8_t *buf, Py_ssize_t len); /* internal interface */
PyObject *modescrc_crc(PyObject *self, PyObject *args);   /* external interface */

/* submodule init/cleanup */
int modescrc_module_init(PyObject *m);
void modescrc_module_free(PyObject *m);
int modesreader_module_init(PyObject *m);
void modesreader_module_free(PyObject *m);
int modesmessage_module_init(PyObject *m);
void modesmessage_module_free(PyObject *m);

#endif
