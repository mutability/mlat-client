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

/* methods / type behaviour */
static PyObject *modesmessage_new(PyTypeObject *type, PyObject *args, PyObject *kwds);
static void modesmessage_dealloc(modesmessage *self);
static int modesmessage_init(modesmessage *self, PyObject *args, PyObject *kwds);
static int modesmessage_bf_getbuffer(PyObject *self, Py_buffer *view, int flags);
static Py_ssize_t modesmessage_sq_length(modesmessage *self);
static PyObject *modesmessage_sq_item(modesmessage *self, Py_ssize_t i);
static long modesmessage_hash(PyObject *self);
static PyObject *modesmessage_richcompare(PyObject *self, PyObject *other, int op);
static PyObject *modesmessage_repr(PyObject *self);
static PyObject *modesmessage_str(PyObject *self);

/* internal helpers */
static PyObject *decode_ac13(unsigned ac13);
static uint32_t crc_residual(uint8_t *message, int len);
static int decode(modesmessage *self);

/* modesmessage fields */
/* todo: these can probably all be read/write */
static PyMemberDef modesmessageMembers[] = {
    { "timestamp",    T_ULONGLONG, offsetof(modesmessage, timestamp), 0,        "12MHz timestamp" },   /* read/write */
    { "signal",       T_UINT,      offsetof(modesmessage, signal),    READONLY, "signal level" },
    { "df",           T_UINT,      offsetof(modesmessage, df),        READONLY, "downlink format or a special DF_* value" },
    { "nuc",          T_UINT,      offsetof(modesmessage, nuc),       READONLY, "NUCp value" },
    { "even_cpr",     T_BOOL,      offsetof(modesmessage, even_cpr),  READONLY, "CPR even-format flag" },
    { "odd_cpr",      T_BOOL,      offsetof(modesmessage, odd_cpr),   READONLY, "CPR odd-format flag" },
    { "valid",        T_BOOL,      offsetof(modesmessage, valid),     READONLY, "Does the message look OK?" },
    { "crc_residual", T_OBJECT,    offsetof(modesmessage, crc),       READONLY, "CRC residual" },
    { "address",      T_OBJECT,    offsetof(modesmessage, address),   READONLY, "ICAO address" },
    { "altitude",     T_OBJECT,    offsetof(modesmessage, altitude),  READONLY, "altitude" },
    { "eventdata",    T_OBJECT,    offsetof(modesmessage, eventdata), READONLY, "event data dictionary for special event messages" },
    { NULL, 0, 0, 0, NULL }
};

/* modesmessage buffer protocol */
static PyBufferProcs modesmessageBufferProcs = {
    modesmessage_bf_getbuffer,          /* bf_getbuffer  */
    NULL                                /* bf_releasebuffer */
};

/* modesmessage sequence protocol */
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

/* modesmessage type object */
static PyTypeObject modesmessageType = {
    PyVarObject_HEAD_INIT(NULL, 0)
    "_modes.Message",                 /* tp_name        */
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
    PyObject_GenericGetAttr,          /* tp_getattro    */
    PyObject_GenericSetAttr,          /* tp_setattro    */
    &modesmessageBufferProcs,         /* tp_as_buffer   */
    Py_TPFLAGS_DEFAULT||Py_TPFLAGS_BASETYPE, /* tp_flags       */
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

/*
 * module setup
 */
int modesmessage_module_init(PyObject *m)
{
    if (PyType_Ready(&modesmessageType) < 0)
        return -1;

    Py_INCREF(&modesmessageType);
    if (PyModule_AddObject(m, "Message", (PyObject *)&modesmessageType) < 0) {
        Py_DECREF(&modesmessageType);
        return -1;
    }

    /* Add DF_* constants */
    if (PyModule_AddIntMacro(m, DF_MODEAC) < 0)
        return -1;

    if (PyModule_AddIntMacro(m, DF_EVENT_TIMESTAMP_JUMP) < 0)
        return -1;

    if (PyModule_AddIntMacro(m, DF_EVENT_MODE_CHANGE) < 0)
        return -1;

    if (PyModule_AddIntMacro(m, DF_EVENT_EPOCH_ROLLOVER) < 0)
        return -1;

    if (PyModule_AddIntMacro(m, DF_EVENT_RADARCAPE_STATUS) < 0)
        return -1;

    if (PyModule_AddIntMacro(m, DF_EVENT_RADARCAPE_POSITION) < 0)
        return -1;

    return 0;
}

void modesmessage_module_free(PyObject *m)
{
}

static PyObject *modesmessage_new(PyTypeObject *type, PyObject *args, PyObject *kwds)
{
    modesmessage *self;

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
    self->crc = NULL;
    self->address = NULL;
    self->altitude = NULL;
    self->data = NULL;
    self->datalen = 0;
    self->eventdata = NULL;

    return (PyObject *)self;
}

static void modesmessage_dealloc(modesmessage *self)
{
    Py_XDECREF(self->crc);
    Py_XDECREF(self->address);
    Py_XDECREF(self->altitude);
    Py_XDECREF(self->eventdata);
    free(self->data);
    Py_TYPE(self)->tp_free((PyObject*)self);
}

/* internal entry point to build a new message from a buffer */
PyObject *modesmessage_from_buffer(unsigned long long timestamp, unsigned signal, uint8_t *data, int datalen)
{
    modesmessage *message;
    uint8_t *copydata;

    if (! (message = (modesmessage*)modesmessage_new(&modesmessageType, NULL, NULL)))
        goto err;

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

    if (decode(message) < 0)
        goto err;

    return (PyObject*)message;

 err:
    Py_XDECREF(message);
    return NULL;
}

/* internal entry point to build a new event message
 * steals a reference from eventdata
 */
PyObject *modesmessage_new_eventmessage(int type, unsigned long long timestamp, PyObject *eventdata)
{
    modesmessage *message;

    if (! (message = (modesmessage*)modesmessage_new(&modesmessageType, NULL, NULL)))
        return NULL;

    message->df = type;
    message->timestamp = timestamp;
    message->eventdata = eventdata;
    return (PyObject *)message;
}

/* external entry point to build a new event message from python i.e. _modes.EventMessage(...) */
PyObject *modesmessage_eventmessage(PyObject *self, PyObject *args, PyObject *kwds)
{
    static char *kwlist[] = { "type", "timestamp", "eventdata", NULL };
    int type;
    unsigned long long timestamp;
    PyObject *eventdata = NULL;
    PyObject *rv = NULL;

    if (!PyArg_ParseTupleAndKeywords(args, kwds, "iKO", kwlist, &type, &timestamp, &eventdata))
        return NULL;

    Py_INCREF(eventdata);
    if (! (rv = modesmessage_new_eventmessage(type, timestamp, eventdata))) {
        Py_DECREF(eventdata);
        return NULL;
    }

    return rv;
}

/* external entry point to build a new message from python (i.e. _modes.Message(...)) */
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

    rv = decode(self);

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

static uint32_t crc_residual(uint8_t *message, int len)
{
    uint32_t crc;

    if (len < 3)
        return 0;

    crc = modescrc_buffer_crc(message, len - 3);
    crc = crc ^ (message[len-3] << 16);
    crc = crc ^ (message[len-2] << 8);
    crc = crc ^ (message[len-1]);
    return crc;
}

static int decode(modesmessage *self)
{
    uint32_t crc;

    /* clear state */
    self->valid = 0;
    self->nuc = 0;
    self->odd_cpr = self->even_cpr = 0;
    Py_CLEAR(self->crc);
    Py_CLEAR(self->address);
    Py_CLEAR(self->altitude);

    if (self->datalen == 2) {
        self->df = DF_MODEAC;
        self->address = PyLong_FromLong((self->data[0] << 8) | self->data[1]);
        self->valid = 1;
        return 0;
    }

    self->df = (self->data[0] >> 3) & 31;

    if ((self->df < 16 && self->datalen != 7) || (self->df >= 16 && self->datalen != 14)) {
        /* wrong length, no further processing */
        return 0;
    }

    if (self->df != 0 && self->df != 4 && self->df != 5 && self->df != 11 &&
        self->df != 16 && self->df != 17 && self->df != 20 && self->df != 21) {
        /* we do not know how to handle this message type, no further processing */
        return 0;
    }

    crc = crc_residual(self->data, self->datalen);
    if (!(self->crc = PyLong_FromLong(crc)))
        return -1;

    switch (self->df) {
    case 0:
    case 4:
    case 16:
    case 20:
        self->address = (Py_INCREF(self->crc), self->crc);
        if (! (self->altitude = decode_ac13((self->data[2] & 0x1f) << 8 | (self->data[3]))))
            return -1;
        self->valid = 1;
        break;

    case 5:
    case 21:
    case 24:
        self->address = (Py_INCREF(self->crc), self->crc);
        self->valid = 1;
        break;

    case 11:
        self->valid = ((crc & ~0x7f) == 0);
        if (self->valid) {
            if (! (self->address = PyLong_FromLong( (self->data[1] << 16) | (self->data[2] << 8) | (self->data[3]) )))
                return -1;
        }
        break;

    case 17:
        self->valid = (crc == 0);
        if (self->valid) {
            unsigned metype;

            unsigned address = (self->data[1] << 16) | (self->data[2] << 8) | (self->data[3]);
            self->address = PyLong_FromLong(address);
            if (!self->address)
                return -1;

            metype = self->data[4] >> 3;
            if ((metype >= 9 && metype <= 18) || (metype >= 20 && metype < 22)) {
                if (metype == 22)
                    self->nuc = 0;
                else if (metype <= 18)
                    self->nuc = 18 - metype;
                else
                    self->nuc = 29 - metype;

                if (0 && self->nuc <= 5) {
                    fprintf(stderr, "%06x nuc: %d\n", address, self->nuc);
                }

                if (self->data[6] & 0x04)
                    self->odd_cpr = 1;
                else
                    self->even_cpr = 1;

                if (! (self->altitude = decode_ac12((self->data[5] << 4) | ((self->data[6] & 0xF0) >> 4))))
                    return -1;

                // crude check if there is any CPR data, if either cpr_lat or cpr_lon is mostly zeros, set invalid
                if ((self->data[7] == 0 && (self->data[8] & 0x7F) == 0) || (self->data[9] == 0 && self->data[10] == 0)) {
                    self->valid = 0;
                    if (0) {
                        fprintf(stderr, "%06x %02x %02x %02x %02x\n",
                                address, self->data[7], self->data[8], self->data[9], self->data[10]);
                    }
                }
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

static const char *df_event_name(int df)
{
    switch (df) {
    case DF_EVENT_TIMESTAMP_JUMP:
        return "DF_EVENT_TIMESTAMP_JUMP";
    case DF_EVENT_MODE_CHANGE:
        return "DF_EVENT_MODE_CHANGE";
    case DF_EVENT_EPOCH_ROLLOVER:
        return "DF_EVENT_EPOCH_ROLLOVER";
    case DF_EVENT_RADARCAPE_STATUS:
        return "DF_EVENT_RADARCAPE_STATUS";
    default:
        return NULL;
    }
}

static char hexdigit[16] = "0123456789abcdef";
static PyObject *modesmessage_repr(PyObject *self)
{
    modesmessage *message = (modesmessage *)self;
    if (message->data) {
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

        return PyUnicode_FromFormat("_modes.Message(b'%s',%llu,%u)", buf, (unsigned long long)message->timestamp, (unsigned)message->signal);
    } else {
        const char *eventname = df_event_name(message->df);
        if (eventname) {
            return PyUnicode_FromFormat("_modes.EventMessage(_modes.%s,%llu,%R)",
                                        df_event_name(message->df),
                                        (unsigned long long)message->timestamp,
                                        message->eventdata);
        } else {
            return PyUnicode_FromFormat("_modes.EventMessage(%d,%llu,%R)",
                                        message->df,
                                        (unsigned long long)message->timestamp,
                                        message->eventdata);
        }
    }
}

static PyObject *modesmessage_str(PyObject *self)
{
    modesmessage *message = (modesmessage *)self;
    if (message->data) {
        char buf[256];
        char *p = buf;
        int i;

        for (i = 0; i < message->datalen; ++i) {
            *p++ = hexdigit[(message->data[i] >> 4) & 15];
            *p++ = hexdigit[message->data[i] & 15];
        }
        *p++ = 0;

        return PyUnicode_FromString(buf);
    } else {
        const char *eventname = df_event_name(message->df);
        if (eventname) {
            return PyUnicode_FromFormat("%s@%llu:%R",
                                        eventname,
                                        (unsigned long long)message->timestamp,
                                        message->eventdata);
        } else {
            return PyUnicode_FromFormat("DF%d@%llu:%R",
                                        message->df,
                                        (unsigned long long)message->timestamp,
                                        message->eventdata);
        }
    }
}

