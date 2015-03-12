/*
 * Copyright 2015, Oliver Jowett <oliver@mutability.co.uk>
 * All rights reserved. Do not redistribute.
 */

#include <Python.h>

#include <stdint.h>

/* Generator polynomial for the Mode S CRC */
#define MODES_GENERATOR_POLY 0xfff409U

/* CRC values for all single-byte messages; used to speed up CRC calculation. */
static uint32_t crc_table[256];

static void init_crc_table(void)
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
}

static PyObject *
crc_residual(PyObject *self, PyObject *args)
{    
    Py_buffer buffer;
    PyObject *rv = NULL;
    uint32_t rem;
    uint8_t *p;
    int i;

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

    if (buffer.len < 3) {
        PyErr_SetString(PyExc_ValueError, "buffer is too small");
        goto out;
    }

    for (rem = 0, i = buffer.len-3, p = buffer.buf; i > 0; --i) {
        rem = ((rem & 0x00ffff) << 8) ^ crc_table[*p++ ^ ((rem & 0xff0000) >> 16)];
    }

    rem = rem ^ (*p++ << 16);
    rem = rem ^ (*p++ << 8);
    rem = rem ^ (*p++);
    rv = PyInt_FromLong(rem);

 out:
    PyBuffer_Release(&buffer);
    return rv;
}

static PyObject *
build_beast_message_tuple(uint64_t timestamp, uint8_t signal, uint8_t *data, int datalen)
{
    PyObject *t1 = NULL, *t2 = NULL, *t3 = NULL, *rv = NULL;
    
    if (! (t1 = PyLong_FromUnsignedLongLong(timestamp)) ||
        ! (t2 = PyInt_FromLong(signal)) ||
        ! (t3 = PyByteArray_FromStringAndSize((char*)data, datalen)) ||
        ! (rv = PyTuple_Pack(3, t1, t2, t3))) {
        if (t1) Py_DECREF(t1);
        if (t2) Py_DECREF(t1);
        if (t3) Py_DECREF(t1);
    }

    return rv;
}

/* given an input bytestring, return a tuple (consumed, [(timestamp, signal, bytes), (timestamp, signal, bytes), ...])
 * where 'consumed' is the number of bytes read from input
 * and the list is the messages + metadata extracted from the input
 */
static PyObject *
packetize_beast_input(PyObject *self, PyObject *args)
{
    Py_buffer buffer;
    PyObject *rv = NULL;
    uint8_t *p, *eod;
    int message_count = 0, max_messages = 0;
    PyObject *message_tuple = NULL;
    PyObject **message_tuples = NULL;

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

    /* allocate the maximum size we might need, given a minimal encoding of:
     *   <1A> <'1'> <6 bytes timestamp> <1 byte signal> <2 bytes message> = 11 bytes total
     */
    message_count = 0;
    max_messages = buffer.len / 11 + 1;
    message_tuples = calloc(max_messages, sizeof(PyObject*));
    if (!message_tuples) {
        rv = PyErr_NoMemory();
        goto out;
    }

    /* parse messages */
    p = buffer.buf;
    eod = buffer.buf + buffer.len;
    while (p+2 <= eod && message_count < max_messages) {
        int message_len = -1;
        uint64_t timestamp;
        uint8_t signal;
        uint8_t message[14];
        uint8_t *m, *eom;
        int i;

        if (p[0] != 0x1a) {
            PyErr_SetString(PyExc_ValueError, "Lost sync with input stream: expected a 0x1A marker but it was not there");
            goto out;
        }

        switch (p[1]) {
        case '1': message_len = 2; break;
        case '2': message_len = 7; break;
        case '3': message_len = 14; break;
        default:
            PyErr_SetString(PyExc_ValueError, "Lost sync with input stream: unexpected message type after 0x1A marker");
            goto out;
        }

        m = p + 2;
        eom = m + 7 + message_len;
        if (eom > eod)
            break;

#define ADVANCE \
        do {                                                            \
            if (*m++ == 0x1a) {                                         \
                if (m < eod && *m != 0x1a) {                            \
                    PyErr_SetString(PyExc_ValueError, "Lost sync with input stream: expected 0x1A after 0x1A escape"); \
                    goto out;                                           \
                }                                                       \
                ++m, ++eom;                                             \
                if (eom > eod)                                          \
                    goto nomoredata;                                    \
            }                                                           \
        } while(0)

        /* timestamp, 6 bytes */
        timestamp = *m;
        ADVANCE;
        timestamp = (timestamp << 8) | *m;
        ADVANCE;
        timestamp = (timestamp << 8) | *m;
        ADVANCE;
        timestamp = (timestamp << 8) | *m;
        ADVANCE;
        timestamp = (timestamp << 8) | *m;
        ADVANCE;
        timestamp = (timestamp << 8) | *m;
        ADVANCE;

        /* signal, 1 byte */
        signal = *m;
        ADVANCE;

        /* message, N bytes */
        for (i = 0; i < message_len; ++i) {
            message[i] = *m;
            ADVANCE;
        }
        
        /* got a complete message */
        if (! (message_tuples[message_count] = build_beast_message_tuple(timestamp, signal, message, message_len)) )
            goto out;

        ++message_count;
        p = m;
    }

 nomoredata:

    if (! (message_tuple = PyTuple_New(message_count)))
        goto out;
    
    while (--message_count >= 0) {
        PyTuple_SET_ITEM(message_tuple, message_count, message_tuples[message_count]); /* steals ref */
    }
    
    rv = Py_BuildValue("(lO)", (long) ((void*)p - buffer.buf), message_tuple);

 out:
    while (--message_count >= 0) {
        Py_DECREF(message_tuples[message_count]);
    }
    free(message_tuples);
    PyBuffer_Release(&buffer);
    return rv;
}

static PyMethodDef methods[] = {
    { "crc_residual", crc_residual, METH_VARARGS, "Calculate a CRC residual over a Mode S message." },
    { "packetize_beast_input", packetize_beast_input, METH_VARARGS, "Turn a Beast-form bytestream into a series of messages." },
    { NULL, NULL, 0, NULL }
};

PyMODINIT_FUNC
init_modes(void)
{
    init_crc_table();
    (void) Py_InitModule("_modes", methods);
}

