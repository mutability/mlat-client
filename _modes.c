/*
 * _modes.c - Python C extension module to speed up Mode S message handling.
 *
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

/* prototypes and definitions */

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
} modesmessage;

/* type support functions */
static long modesmessage_hash(PyObject *self);
static PyObject *modesmessage_richcompare(PyObject *self, PyObject *other, int op);
static PyObject *modesmessage_repr(PyObject *self);
static PyObject *modesmessage_str(PyObject *self);
static PyObject *modesmessage_new(PyTypeObject *type, PyObject *args, PyObject *kwds);
static int modesmessage_init(modesmessage *self, PyObject *args, PyObject *kwds);
static void modesmessage_dealloc(modesmessage *self);
/* sequence support functions */
static Py_ssize_t modesmessage_sq_length(modesmessage *self);
static PyObject *modesmessage_sq_item(modesmessage *self, Py_ssize_t i);
/* buffer support functions */
static int modesmessage_bf_getbuffer(PyObject *self, Py_buffer *view, int flags);
/* internal factory function */
static PyObject *modesmessage_from_buffer(unsigned long long timestamp, unsigned signal, uint8_t *data, int datalen);
/* decoder */
static int modesmessage_decode(modesmessage *self);

static void init_crc_table(void);
static uint32_t calculate_crc(uint8_t *buf, int len);
static PyObject *crc_residual(PyObject *self, PyObject *args);
static PyObject *packetize_beast_input(PyObject *self, PyObject *args);
static PyObject *packetize_radarcape_input(PyObject *self, PyObject *args);
static PyObject *packetize_sbs_input(PyObject *self, PyObject *args);
static PyObject *packetize_beast_or_radarcape_input(PyObject *self, PyObject *args, int radarcape_gps_mode);

static PyObject *PyExc_ClockResetError;
static PyTypeObject modesmessageType;


/********** CRC ****************/

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

static uint32_t calculate_crc(uint8_t *buf, int len)
{
    uint32_t rem;
    int i;
    for (rem = 0, i = len-3; i > 0; --i) {
        rem = ((rem & 0x00ffff) << 8) ^ crc_table[*buf++ ^ ((rem & 0xff0000) >> 16)];
    }

    rem = rem ^ (*buf++ << 16);
    rem = rem ^ (*buf++ << 8);
    rem = rem ^ (*buf++);

    return rem;
}

static PyObject *
crc_residual(PyObject *self, PyObject *args)
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

    if (buffer.len < 3) {
        PyErr_SetString(PyExc_ValueError, "buffer is too small");
        goto out;
    }

    rv = PyLong_FromLong(calculate_crc(buffer.buf, buffer.len));

 out:
    PyBuffer_Release(&buffer);
    return rv;
}

/********** BEAST INPUT **************/

/* given an input bytestring, return a tuple (consumed, [(timestamp, signal, bytes), (timestamp, signal, bytes), ...])
 * where 'consumed' is the number of bytes read from input
 * and the list is the messages + metadata extracted from the input
 */
static PyObject *packetize_beast_input(PyObject *self, PyObject *args)
{
    return packetize_beast_or_radarcape_input(self, args, 0);
}

static PyObject *packetize_radarcape_input(PyObject *self, PyObject *args)
{
    return packetize_beast_or_radarcape_input(self, args, 1);
}

static PyObject *packetize_beast_or_radarcape_input(PyObject *self, PyObject *args, int radarcape_gps_mode)
{
    Py_buffer buffer;
    PyObject *rv = NULL;
    uint8_t *p, *eod;
    int message_count = 0, max_messages = 0;
    PyObject *message_tuple = NULL;
    PyObject **messages = NULL;
    unsigned PY_LONG_LONG starting_timestamp = 0;
    uint64_t last_timestamp = 0;

    if (!PyArg_ParseTuple(args, "y*K", &buffer, &starting_timestamp))
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
    max_messages = buffer.len / 11 + 2;
    messages = calloc(max_messages, sizeof(PyObject*));
    if (!messages) {
        rv = PyErr_NoMemory();
        goto out;
    }

    /* parse messages */
    last_timestamp = (uint64_t) starting_timestamp;
    p = buffer.buf;
    eod = buffer.buf + buffer.len;
    while (p+2 <= eod && message_count+1 < max_messages) {
        int message_len = -1;
        uint64_t timestamp;
        uint8_t signal;
        uint8_t message[14];
        uint8_t *m, *eom;
        int i;
        uint8_t type;

        if (p[0] != 0x1a) {
            PyErr_Format(PyExc_ValueError, "Lost sync with input stream: expected a 0x1A marker at offset %d but found 0x%02x instead", (int) ((void*)p-buffer.buf), (int)p[0]);
            goto out;
        }

        type = p[1];
        switch (type) {
        case '1': message_len = 2; break;
        case '2': message_len = 7; break;
        case '3': message_len = 14; break;
        case '4': message_len = 14; break;
        default:
            PyErr_Format(PyExc_ValueError, "Lost sync with input stream: unexpected message type 0x%02x after 0x1A marker at offset %d", (int)p[1], (int) ((void*)p-buffer.buf));
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

        if (radarcape_gps_mode) {
            /* adjust timestamp so that it is a contiguous nanoseconds-since-
             * midnight value, rather than the raw form which skips values once
             * a second
             */
            uint64_t nanos = timestamp & 0x00003FFFFFFF;
            uint64_t secs = timestamp >> 30;
            timestamp = nanos + secs * 1000000000;

            /* adjust for the timestamp being at the _end_ of the frame;
             * we don't really care about getting a particular starting point
             * (that's just a fixed offset), so long as it is _the same in
             * every frame_.
             */
            timestamp = timestamp - (8000 + message_len * 8000); /* each byte takes 8us to transmit, plus 8us preamble */

            if (timestamp < last_timestamp) {
                /* check for end of day rollover */
                if (last_timestamp >= (86340 * 1000000000ULL) && timestamp <= (60 * 1000000000ULL)) {
                    /* we plug this in as a special "message" in the returned tuple, as
                     * we just want to flag it, not break the parsing.
                     */
                    modesmessage *marker = (modesmessage*)modesmessage_new(&modesmessageType, NULL, NULL);
                    if (!marker)
                        goto out;

                    marker->df = -1; /* special flag for clock reset */
                    marker->timestamp = timestamp;
                    messages[message_count++] = (PyObject*)marker;
                } else if ((last_timestamp - timestamp) > 1000000000ULL) {
                    PyErr_Format(PyExc_ClockResetError,
                                 "Out of range timestamp seen (last %llu, now %llu)",
                                 (unsigned long long)last_timestamp,
                                 (unsigned long long)timestamp);
                    goto out;
                }
            }
        } else {
            /* check for very out of range value
             * (dump1090 can hold messages for up to 60 seconds! so be conservative here)
             */
            if (timestamp < last_timestamp && (last_timestamp - timestamp) > 90*12000000ULL) {
                PyErr_Format(PyExc_ClockResetError,
                             "Out of range timestamp seen (last %llu, now %llu)",
                             (unsigned long long)last_timestamp,
                             (unsigned long long)timestamp);
                goto out;
            }
        }

        last_timestamp = timestamp;

        /* got a complete message */
        if (type == '4') {
            /* This is some sort of periodic stats message generated by Radarcape receivers.
             * Skip it.
             */
            p = m;
            continue;
        }

        if (! (messages[message_count] = modesmessage_from_buffer(timestamp, signal, message, message_len)) )
            goto out;

        if (!((modesmessage*)messages[message_count])->valid) {
            Py_DECREF(messages[message_count]);
        } else {
            ++message_count;
        }

        p = m;
    }

 nomoredata:
    if (! (message_tuple = PyTuple_New(message_count)))
        goto out;
    
    while (--message_count >= 0) {
        PyTuple_SET_ITEM(message_tuple, message_count, messages[message_count]); /* steals ref */
    }
    
    rv = Py_BuildValue("(lN)", (long) ((void*)p - buffer.buf), message_tuple);

 out:
    while (--message_count >= 0) {
        Py_DECREF(messages[message_count]);
    }
    free(messages);
    PyBuffer_Release(&buffer);
    return rv;
}


/********** SBS INPUT **************/

/*
 * Some notes on this format, as it is poorly documented by Kinetic:
 *
 * The stream can start at an arbitrary point, the first byte might be mid-packet.
 * You need to look for a DLE STX to synchronize with the stream.
 * This implementation does that in the Python code to keep this bit simpler; the
 * C code assumes it is always given bytes starting at the start of a packet.
 *
 * You might get arbitrary packet types e.g. AIS interleaved with Mode S messages.
 * This implementation doesn't try to interpret them at all, it just reads all
 * data until DLE ETX regardless of type and skips those types it doesn't
 * understand.
 *
 * The Mode S CRC values are not the raw bytes from the message; they are the
 * residual CRC value after XORing the raw bytes with the calculated CRC over
 * the body of the message. That is, a DF17 message with a correct CRC will have
 * zeros in the CRC bytes; a DF11 with correct CRC will have the IID in the CRC
 * bytes; messages that use Address/Parity will have the address in the CRC bytes.
 * To recover the original message, calculate the CRC and XOR it back into the CRC
 * bytes. Andrew Whewell says this is probably controlled by a Basestation setting.
 *
 * The timestamps are measured at the _end_ of the frame, not at the start.
 * As frames are variable length, if you want a timestamp anchored to the
 * start of the frame (as dump1090 / Beast do), you have to compensate for
 * the frame length.
 */


/* given an input bytestring, return a tuple (consumed, [(timestamp, signal, bytes), (timestamp, signal, bytes), ...])
 * where 'consumed' is the number of bytes read from input
 * and the list is the messages + metadata extracted from the input
 */
static PyObject *packetize_sbs_input(PyObject *self, PyObject *args)
{
    Py_buffer buffer;
    PyObject *rv = NULL;
    uint8_t *p, *eod;
    int message_count = 0, max_messages = 0;
    PyObject *message_tuple = NULL;
    PyObject **messages = NULL;
    unsigned PY_LONG_LONG starting_timestamp = 0;
    uint64_t last_timestamp = 0;

    if (!PyArg_ParseTuple(args, "y*K", &buffer, &starting_timestamp))
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
     *   <DLE> <STX> <0x09> <n/a> <3 bytes timestamp> <2 bytes message> <DLE> <ETX> <2 bytes CRC> = 13 bytes total
     */
    message_count = 0;
    max_messages = buffer.len / 13 + 1;
    messages = calloc(max_messages, sizeof(PyObject*));
    if (!messages) {
        rv = PyErr_NoMemory();
        goto out;
    }

    /* parse messages */
    last_timestamp = (uint64_t) starting_timestamp;
    p = buffer.buf;
    eod = buffer.buf + buffer.len;
    while (p+13 <= eod && message_count < max_messages) {
        int message_len = -1;
        uint64_t timestamp;
        /* largest message we care about is:
         *  type      1 byte   0x05 = ADS-B
         *  spare     1 byte
         *  timestamp 3 bytes
         *  data      14 bytes
         *      total 19 bytes
         */
        uint8_t data[19];
        uint8_t *m;
        int i;
        uint8_t type;

        if (p[0] != 0x10 || p[1] != 0x02) {
            PyErr_Format(PyExc_ValueError, "Lost sync with input stream: expected DLE STX at offset %d but found 0x%02x 0x%02x instead", (int) ((void*)p-buffer.buf), (int)p[0], (int)p[1]);
            goto out;
        }

        /* scan for DLE ETX, copy data */
        m = p + 2;
        i = 0;
        while (m < eod) {
            if (*m == 0x10) {
                if ((m+1) >= eod)
                    goto nomoredata;

                if (m[1] == 0x03) {
                    /* DLE ETX found */
                    break;
                }

                if (m[1] != 0x10) {
                    PyErr_Format(PyExc_ValueError, "Lost sync with input stream: unexpected DLE 0x%02x at offset %d", (int) ((void*)m-buffer.buf), (int)m[1]);
                    goto out;
                }

                ++m;
            }

            if (i < 19)
                data[i++] = *m;

            ++m;
        }

        /* now pointing at DLE of DLE ETX */
        m += 2;

        /* first CRC byte */
        if (m >= eod)
            goto nomoredata;
        if (*m++ == 0x10) {
            if (m >= eod)
                goto nomoredata;
            if (m[0] != 0x10) {
                PyErr_Format(PyExc_ValueError, "Lost sync with input stream: unexpected DLE 0x%02x at offset %d", (int) ((void*)m-buffer.buf), (int)*m);
                goto out;
            }
            ++m;
        }

        /* second CRC byte */
        if (m >= eod)
            goto nomoredata;
        if (*m++ == 0x10) {
            if (m >= eod)
                goto nomoredata;
            if (m[0] != 0x10) {
                PyErr_Format(PyExc_ValueError, "Lost sync with input stream: unexpected DLE 0x%02x at offset %d", (int) ((void*)m-buffer.buf), (int)*m);
                goto out;
            }
            ++m;
        }

        /* try to make sense of the message */
        type = data[0];
        switch (type) {
        case 0x01:
            /* ADS-B or TIS-B */
            message_len = 14;
            break;

        case 0x05:
            /* Mode S, long */
            message_len = 14;
            break;

        case 0x07:
            /* Mode S, short */
            message_len = 7;
            break;

        case 0x09:
            /* Mode A/C */
            message_len = 2;
            break;

        default:
            /* something else, skip it */
            break;
        }

        if (message_len > 0 && (5 + message_len) <= i) {
            /* regenerate message CRC */
            if (message_len > 3) {
                uint32_t crc = calculate_crc(&data[5], message_len);  /* this XORs the resulting CRC with any existing data, which is what we want */
                data[5 + message_len - 3] = crc >> 16;
                data[5 + message_len - 2] = crc >> 8;
                data[5 + message_len - 1] = crc;
            }

            /* little-endian, apparently */
            timestamp = (data[4] << 16) | (data[3] << 8) | (data[2]);

            /* Baseless speculation! Let's assume that it's like the Radarcape
             * and measures at the end of the frame.
             *
             * It's easier to add to the timestamp than subtract from it, so
             * add on enough of an offset so that the timestamps we report are
             * consistently (start of frame + 112us) regardless of the actual
             * frame length.
             */
            timestamp = (timestamp + ((14-message_len) * 160)) & 0xFFFFFF;

            /* merge in top bits */
            timestamp = timestamp | (last_timestamp & 0xFFFFFFFFFF000000ULL);

            /* check for rollover */
            if (timestamp < last_timestamp)
                timestamp += (1 << 24);

            last_timestamp = timestamp;

            if (! (messages[message_count] = modesmessage_from_buffer(timestamp, 0, &data[5], message_len)))
                goto out;

            if (!((modesmessage*)messages[message_count])->valid) {
                Py_DECREF(messages[message_count]);
            } else {
                ++message_count;
            }
        }

        p = m;
    }

 nomoredata:
    if (! (message_tuple = PyTuple_New(message_count)))
        goto out;

    while (--message_count >= 0) {
        PyTuple_SET_ITEM(message_tuple, message_count, messages[message_count]); /* steals ref */
    }

    rv = Py_BuildValue("(lN)", (long) ((void*)p - buffer.buf), message_tuple);

 out:
    while (--message_count >= 0) {
        Py_DECREF(messages[message_count]);
    }
    free(messages);
    PyBuffer_Release(&buffer);
    return rv;
}

/****** MODE S MESSAGES *******/

static PyMemberDef modesmessageMembers[] = {
    { "timestamp", T_ULONGLONG, offsetof(modesmessage, timestamp), 0,        "12MHz timestamp" },   /* read/write */
    { "signal",    T_UINT,      offsetof(modesmessage, signal),    READONLY, "signal level" },
    { "df",        T_UINT,      offsetof(modesmessage, df),        READONLY, "downlink format" },
    { "nuc",       T_UINT,      offsetof(modesmessage, nuc),       READONLY, "NUCp value" },
    { "even_cpr",  T_BOOL,      offsetof(modesmessage, even_cpr),  READONLY, "CPR even-format flag" },
    { "odd_cpr",   T_BOOL,      offsetof(modesmessage, odd_cpr),   READONLY, "CPR odd-format flag" },
    { "valid",     T_BOOL,      offsetof(modesmessage, valid),     READONLY, "Does the message look OK?" },
    { "crc",       T_OBJECT_EX, offsetof(modesmessage, crc),       READONLY, "CRC residual" },
    { "address",   T_OBJECT_EX, offsetof(modesmessage, address),   READONLY, "ICAO address" },
    { "altitude",  T_OBJECT_EX, offsetof(modesmessage, altitude),  READONLY, "altitude" },
    { NULL, 0, 0, 0, NULL }
};

static PyBufferProcs modesmessageBufferProcs = {    
    modesmessage_bf_getbuffer,          /* bf_getbuffer  */
    NULL                                /* bf_releasebuffer */
};

static PySequenceMethods modesmessageSequenceMethods = {
    (lenfunc)modesmessage_sq_length,    /* sq_length */
    0,                                  /* sq_concat */
    0,                                  /* sq_repeat */
    (ssizeargfunc)modesmessage_sq_item, /* sq_item */
    0,                                  /* sq_ass_item */
    0,                                  /* sq_contains */
    0,                                  /* sq_inplace_concat */
    0,                                  /* sq_inplace_repeat */
};

static PyTypeObject modesmessageType = {
    PyVarObject_HEAD_INIT(NULL, 0)
    "_modes.message",                 /* tp_name        */
    sizeof(modesmessage),             /* tp_basicsize   */
    0,                                /* tp_itemsize    */
    (destructor)modesmessage_dealloc, /* tp_dealloc     */
    0,                                /* tp_print       */
    0,                                /* tp_getattr     */
    0,                                /* tp_setattr     */
    0,                                /* tp_reserved    */
    (reprfunc)modesmessage_repr,      /* tp_repr        */
    0,                                /* tp_as_number   */
    &modesmessageSequenceMethods,     /* tp_as_sequence */
    0,                                /* tp_as_mapping  */
    (hashfunc)modesmessage_hash,      /* tp_hash        */
    0,                                /* tp_call        */
    (reprfunc)modesmessage_str,       /* tp_str         */
    0,                                /* tp_getattro    */
    0,                                /* tp_setattro    */
    &modesmessageBufferProcs,         /* tp_as_buffer   */
    Py_TPFLAGS_DEFAULT,               /* tp_flags       */
    "A ModeS message.",               /* tp_doc         */
    0,                                /* tp_traverse    */
    0,                                /* tp_clear       */
    modesmessage_richcompare,         /* tp_richcompare */
    0,                                /* tp_weaklistoffset */
    0,                                /* tp_iter        */
    0,                                /* tp_iternext    */
    0,                                /* tp_methods     */
    modesmessageMembers,              /* tp_members     */
    0,                                /* tp_getset      */
    0,                                /* tp_base        */
    0,                                /* tp_dict        */
    0,                                /* tp_descr_get   */
    0,                                /* tp_descr_set   */
    0,                                /* tp_dictoffset  */
    (initproc)modesmessage_init,      /* tp_init        */
    0,                                /* tp_alloc       */
    modesmessage_new,                 /* tp_new         */
};

static PyObject *modesmessage_new(PyTypeObject *type, PyObject *args, PyObject *kwds)
{
    modesmessage *self;

    /*fprintf(stderr, "modesmessage_new(...)\n");*/

    self = (modesmessage *)type->tp_alloc(type, 0);
    if (!self)
        return NULL;

    /* minimal init */
    self->timestamp = 0;
    self->signal = 0;
    self->df = 0;
    self->nuc = 0;
    self->even_cpr = self->odd_cpr = 0;
    self->valid = 0;
    Py_INCREF(Py_None); self->crc = Py_None;
    Py_INCREF(Py_None); self->address = Py_None;
    Py_INCREF(Py_None); self->altitude = Py_None;
    self->data = NULL;
    self->datalen = 0;

    /*fprintf(stderr, "modesmessage_new(...): returns %p\n", self);*/
    return (PyObject *)self;
}

static void modesmessage_dealloc(modesmessage *self)
{
    /*fprintf(stderr, "modesmessage_dealloc(%p)\n", self); */
    Py_XDECREF(self->crc);
    Py_XDECREF(self->address);
    Py_XDECREF(self->altitude);
    free(self->data);
    Py_TYPE(self)->tp_free((PyObject*)self);
}

/* internal entry point to build a new message from a buffer */
static PyObject *modesmessage_from_buffer(unsigned long long timestamp, unsigned signal, uint8_t *data, int datalen)
{
    modesmessage *message;
    uint8_t *copydata;

    /*fprintf(stderr, "modesmessage_from_buffer(%llu,%u,%p,%d)\n", timestamp, signal, data, datalen); */
    message = (modesmessage*)modesmessage_new(&modesmessageType, NULL, NULL);
    if (!message)
        goto err;

    /*fprintf(stderr, "modesmessage_from_buffer(...): new object is %p\n", message); */

    /* minimal init so deallocation works */
    message->data = NULL;

    copydata = malloc(datalen);
    if (!copydata) {
        PyErr_NoMemory();
        goto err;
    }
    memcpy(copydata, data, datalen);

    message->timestamp = timestamp;
    message->signal = signal;
    message->data = copydata;
    message->datalen = datalen;

    if (modesmessage_decode(message) < 0)
        goto err;

    return (PyObject*)message;

 err:
    Py_DECREF(message);
    return NULL;
}

/* external entry point to build a new message from python (i.e. _modes.message(...)) */
static int modesmessage_init(modesmessage *self, PyObject *args, PyObject *kwds)
{
    static char *kwlist[] = { "data", "timestamp", "signal", NULL };
    Py_buffer data;
    int rv = -1;

    if (!PyArg_ParseTupleAndKeywords(args, kwds, "s*|KI", kwlist, &data, &self->timestamp, &self->signal))
        return -1;

    if (data.itemsize != 1) {
        PyErr_SetString(PyExc_ValueError, "buffer itemsize is not 1");
        goto out;
    }

    if (!PyBuffer_IsContiguous(&data, 'C')) {
        PyErr_SetString(PyExc_ValueError, "buffer is not contiguous");
        goto out;
    }

    self->datalen = 0;
    free(self->data);

    self->data = malloc(data.len);
    if (!self->data) {
        PyErr_NoMemory();
        goto out;
    }

    memcpy(self->data, data.buf, data.len);
    self->datalen = data.len;
    
    rv = modesmessage_decode(self);

 out:
    PyBuffer_Release(&data);
    return rv;
}

static PyObject *decode_ac13(unsigned ac13)
{
    int h, f, a;

    if (ac13 == 0)
        Py_RETURN_NONE;

    if (ac13 & 0x0040) /* M bit */
        Py_RETURN_NONE;
    
    if (ac13 & 0x0010) { /* Q bit */
        int n = ((ac13 & 0x1f80) >> 2) | ((ac13 & 0x0020) >> 1) | (ac13 & 0x000f);
        return PyLong_FromLong(n * 25 - 1000);
    }

    /* convert from Gillham code */
    if (! ((ac13 & 0x1500))) {
        /* illegal gillham code */
        Py_RETURN_NONE;
    }

    h = 0;
    if (ac13 & 0x1000) h ^= 7;  /* C1 */
    if (ac13 & 0x0400) h ^= 3;  /* C2 */
    if (ac13 & 0x0100) h ^= 1;  /* C4 */

    if (h & 5)
        h ^= 5;

    if (h > 5)
        Py_RETURN_NONE; /* illegal */
    
    f = 0;
    if (ac13 & 0x0010) f ^= 0x1ff; /* D1 */
    if (ac13 & 0x0004) f ^= 0x0ff; /* D2 */
    if (ac13 & 0x0001) f ^= 0x07f; /* D4 */
    if (ac13 & 0x0800) f ^= 0x03f; /* A1 */
    if (ac13 & 0x0200) f ^= 0x01f; /* A2 */
    if (ac13 & 0x0080) f ^= 0x00f; /* A4 */
    if (ac13 & 0x0020) f ^= 0x007; /* B1 */
    if (ac13 & 0x0008) f ^= 0x003; /* B2 */
    if (ac13 & 0x0002) f ^= 0x001; /* B4 */
    
    if (f & 1)
        h = (6 - h);

    a = 500 * f + 100 * h - 1300;
    if (a < -1200)
        Py_RETURN_NONE; /* illegal */

    return PyLong_FromLong(a);
}

static PyObject *decode_ac12(unsigned ac12)
{
    return decode_ac13(((ac12 & 0x0fc0) << 1) | (ac12 & 0x003f));
}

static int modesmessage_decode(modesmessage *self)
{
    uint32_t crc;

    /*fprintf(stderr, "modesmessage_decode(%p,len=%d)\n", self, self->datalen);*/

    if (self->datalen < 7) {
        /*fprintf(stderr, "Mode A/C\n");*/
        self->df = 32;
        self->valid = 1;
        Py_XDECREF(self->crc); self->crc = (Py_INCREF(Py_None), Py_None);
        Py_XDECREF(self->address); self->address = (Py_INCREF(Py_None), Py_None);
        Py_XDECREF(self->altitude); self->altitude = (Py_INCREF(Py_None), Py_None);
        return 0;
    }

    crc = calculate_crc(self->data, self->datalen);
    /*fprintf(stderr, " CRC: %06x\n", crc);*/
    Py_XDECREF(self->crc);
    if (!(self->crc = PyLong_FromLong(crc)))
        return -1;

    self->df = (self->data[0] >> 3) & 31;
    /*fprintf(stderr, " DF : %d\n", self->df);*/
    self->valid = ((self->df < 16 && self->datalen == 7) || (self->df >= 16 && self->datalen == 14));
    if (!self->valid) {
        /*fprintf(stderr, "  (no further decoding)\n");*/
        /* don't decode further */
        return 0;
    }
    
    switch (self->df) {
    case 0:
    case 4:
    case 16:
    case 20:
        Py_XDECREF(self->address); self->address = (Py_INCREF(self->crc), self->crc);
        Py_XDECREF(self->altitude);
        if (! (self->altitude = decode_ac13((self->data[2] & 0x1f) << 8 | (self->data[3]))))
            return -1;
        break;

    case 5:
    case 21:
    case 24:
        Py_XDECREF(self->address); self->address = (Py_INCREF(self->crc), self->crc);
        Py_XDECREF(self->altitude); self->altitude = (Py_INCREF(Py_None), Py_None);
        break;

    case 11:
        self->valid = ((crc & ~0x7f) == 0);
        if (self->valid) {
            Py_XDECREF(self->address);
            if (!(self->address = PyLong_FromLong( (self->data[1] << 16) | (self->data[2] << 8) | (self->data[3]) )))
                return -1;
            Py_XDECREF(self->altitude); self->altitude = (Py_INCREF(Py_None), Py_None);
        }
        break;

    case 17:
    case 18:
        self->valid = (crc == 0);
        if (self->valid) {
            unsigned metype;

            Py_XDECREF(self->address);
            if (!(self->address = PyLong_FromLong( (self->data[1] << 16) | (self->data[2] << 8) | (self->data[3]) )))
                return -1;            
            Py_XDECREF(self->altitude); self->altitude = (Py_INCREF(Py_None), Py_None);

            metype = self->data[4] >> 3;
            if ((metype >= 9 && metype <= 18) || (metype >= 20 && metype < 22)) {
                if (metype == 22)
                    self->nuc = 0;
                else if (metype <= 18)
                    self->nuc = 18 - metype;
                else
                    self->nuc = 29 - metype;

                if (self->data[6] & 0x04)
                    self->odd_cpr = 1;
                else
                    self->even_cpr = 1;
                Py_XDECREF(self->altitude);
                if (! (self->altitude = decode_ac12((self->data[5] << 4) | ((self->data[6] & 0xF0) >> 4))))
                    return -1;
            }
        }
        break;

    default:
        break;
    }

    return 0;
}

static int modesmessage_bf_getbuffer(PyObject *self, Py_buffer *view, int flags)
{
    return PyBuffer_FillInfo(view, self, ((modesmessage*)self)->data, ((modesmessage*)self)->datalen, 1, flags);
}

static Py_ssize_t modesmessage_sq_length(modesmessage *self)
{
    return self->datalen;
}

static PyObject *modesmessage_sq_item(modesmessage *self, Py_ssize_t i)
{
    if (i < 0 || i >= self->datalen) {
        PyErr_SetString(PyExc_IndexError, "byte index out of range");
        return NULL;
    }

    return PyLong_FromLong(self->data[i]);
}

static long modesmessage_hash(PyObject *self)
{
    modesmessage *msg = (modesmessage*)self;
    uint32_t hash = 0;
    int i;

    /* Jenkins one-at-a-time hash */
    for (i = 0; i < 4 && i < msg->datalen; ++i) {
        hash += msg->data[i] & 0xff;
        hash += hash << 10;
        hash ^= hash >> 6;
    }
             
    hash += (hash << 3);
    hash ^= (hash >> 11);
    hash += (hash << 15);

    return (long)hash;
}

static PyObject *modesmessage_richcompare(PyObject *self, PyObject *other, int op)
{
    PyObject *result = NULL;

    if (! PyObject_TypeCheck(self, &modesmessageType) ||
        ! PyObject_TypeCheck(other, &modesmessageType)) {
        result = Py_NotImplemented;
    } else { 
        modesmessage *message1 = (modesmessage*)self;
        modesmessage *message2 = (modesmessage*)other;
        int c = 0;

        switch (op) {
        case Py_EQ:
            c = (message1->datalen == message2->datalen) && (memcmp(message1->data, message2->data, message1->datalen) == 0);
            break;
        case Py_NE:
            c = (message1->datalen != message2->datalen) || (memcmp(message1->data, message2->data, message1->datalen) != 0);
            break;
        case Py_LT:
            c = (message1->datalen < message2->datalen) || 
                (message1->datalen == message2->datalen && memcmp(message1->data, message2->data, message1->datalen) < 0);
            break;
        case Py_LE:
            c = (message1->datalen < message2->datalen) ||
                (message1->datalen == message2->datalen && memcmp(message1->data, message2->data, message1->datalen) <= 0);
            break;
        case Py_GT:
            c = (message1->datalen > message2->datalen) ||
                (message1->datalen == message2->datalen && memcmp(message1->data, message2->data, message1->datalen) > 0);
            break;
        case Py_GE:
            c = (message1->datalen > message2->datalen) ||
                (message1->datalen == message2->datalen && memcmp(message1->data, message2->data, message1->datalen) >= 0);
            break;
        default:
            result = Py_NotImplemented;
            break;
        }

        if (!result)
            result = (c ? Py_True : Py_False);
    }

    Py_INCREF(result);
    return result;
}

static char *hexdigit = "0123456789abcdef";
static PyObject *modesmessage_repr(PyObject *self)
{
    modesmessage *message = (modesmessage *)self;
    char buf[256];
    char *p = buf;
    int i;

    for (i = 0; i < message->datalen; ++i) {
        *p++ = '\\';
        *p++ = 'x';
        *p++ = hexdigit[(message->data[i] >> 4) & 15];
        *p++ = hexdigit[message->data[i] & 15];
    }
    *p++ = 0;

    return PyUnicode_FromFormat("_modes.message(b'%s',%llu,%u)", buf, (unsigned long long)message->timestamp, (unsigned)message->signal);
}

static PyObject *modesmessage_str(PyObject *self)
{
    modesmessage *message = (modesmessage *)self;
    char buf[256];
    char *p = buf;
    int i;

    for (i = 0; i < message->datalen; ++i) {
        *p++ = hexdigit[(message->data[i] >> 4) & 15];
        *p++ = hexdigit[message->data[i] & 15];
    }
    *p++ = 0;

    return PyUnicode_FromString(buf);
}

static PyMethodDef methods[] = {
    { "crc_residual", crc_residual, METH_VARARGS, "Calculate a CRC residual over a Mode S message." },
    { "packetize_beast_input", packetize_beast_input, METH_VARARGS, "Turn Beast-format input into a series of messages." },
    { "packetize_radarcape_input", packetize_radarcape_input, METH_VARARGS, "Turn input from a Radarcape with GPS timestamps into a series of messages." },
    { "packetize_sbs_input", packetize_sbs_input, METH_VARARGS, "Turn input from a SBS raw data socket into a series of messages." },
    { NULL, NULL, 0, NULL }
};

PyDoc_STRVAR(docstr, "C helpers to speed up ModeS message processing");
static PyModuleDef module = {
    PyModuleDef_HEAD_INIT,
    "_modes",
    docstr,
    0,
    methods,
    NULL,
    NULL,
    NULL,
    NULL
};
    
PyMODINIT_FUNC
PyInit__modes(void)
{
    PyObject *m = NULL;

    init_crc_table();

    if (PyType_Ready(&modesmessageType) < 0)
        return NULL;
    

    m = PyModule_Create(&module);
    if (m == NULL)
        return NULL;
    
    Py_INCREF(&modesmessageType);
    if (PyModule_AddObject(m, "message", (PyObject *)&modesmessageType) < 0) {
        Py_DECREF(&modesmessageType);
        goto error;
    }

    if (!(PyExc_ClockResetError = PyErr_NewException("_modes.ClockResetError", NULL, NULL)))
        goto error;

    if (PyModule_AddObject(m, "ClockResetError", PyExc_ClockResetError) < 0) {
        Py_DECREF(PyExc_ClockResetError);
        goto error;
    }

    return m;

 error:
    Py_DECREF(m);
    return NULL;
}
