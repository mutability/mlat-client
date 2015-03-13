/*
 * Copyright 2015, Oliver Jowett <oliver@mutability.co.uk>
 * All rights reserved. Do not redistribute.
 */

#include <Python.h>
#include <structmember.h>

#include <stdint.h>

/* prototypes and definitions */

typedef struct {
    PyObject HEAD;

    unsigned long long timestamp;
    unsigned int signal;

    unsigned int df;
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
static int modesmessage_compare(PyObject *self, PyObject *other);
static PyObject *modesmessage_repr(PyObject *self);
static PyObject *modesmessage_str(PyObject *self);
static PyObject *modesmessage_new(PyTypeObject *type, PyObject *args, PyObject *kwds);
static int modesmessage_init(modesmessage *self, PyObject *args, PyObject *kwds);
static void modesmessage_dealloc(modesmessage *self);
/* buffer support functions */
static Py_ssize_t modesmessage_getreadbuffer(PyObject *self, Py_ssize_t segment, void **ptrptr);
static Py_ssize_t modesmessage_segcount(PyObject *self, Py_ssize_t *lenp);
/* internal factory function */
static PyObject *modesmessage_from_buffer(unsigned long long timestamp, unsigned signal, uint8_t *data, int datalen);
/* decoder */
static int modesmessage_decode(modesmessage *self);

static void init_crc_table(void);
static uint32_t calculate_crc(uint8_t *buf, int len);
static PyObject *crc_residual(PyObject *self, PyObject *args);
static PyObject *packetize_beast_input(PyObject *self, PyObject *args);

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

    rv = PyInt_FromLong(calculate_crc(buffer.buf, buffer.len));

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
    Py_buffer buffer;
    PyObject *rv = NULL;
    uint8_t *p, *eod;
    int message_count = 0, max_messages = 0;
    PyObject *message_tuple = NULL;
    PyObject **messages = NULL;

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
    messages = calloc(max_messages, sizeof(PyObject*));
    if (!messages) {
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
        if (! (messages[message_count] = modesmessage_from_buffer(timestamp, signal, message, message_len)) )
            goto out;

        ++message_count;
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
    { "timestamp", T_ULONGLONG, offsetof(modesmessage, timestamp), READONLY, "12MHz timestamp" },
    { "signal",    T_UINT,      offsetof(modesmessage, signal),    READONLY, "signal level" },
    { "df",        T_UINT,      offsetof(modesmessage, df),        READONLY, "downlink format" },
    { "even_cpr",  T_BOOL,      offsetof(modesmessage, even_cpr),  READONLY, "CPR even-format flag" },
    { "odd_cpr",   T_BOOL,      offsetof(modesmessage, odd_cpr),   READONLY, "CPR odd-format flag" },
    { "valid",     T_BOOL,      offsetof(modesmessage, valid),     READONLY, "Does the message look OK?" },
    { "crc",       T_OBJECT_EX, offsetof(modesmessage, crc),       READONLY, "CRC residual" },
    { "address",   T_OBJECT_EX, offsetof(modesmessage, address),   READONLY, "ICAO address" },
    { "altitude",  T_OBJECT_EX, offsetof(modesmessage, altitude),  READONLY, "altitude" },
    { NULL, 0, 0, 0, NULL }
};

static PyBufferProcs modesmessageBufferProcs = {
    modesmessage_getreadbuffer,   /* bf_getreadbuffer  */
    0,                            /* bf_getwritebyffer */
    modesmessage_segcount,        /* bf_getsegcount    */
    0,                            /* bf_getcharbuffer  */
};

static PyTypeObject modesmessageType = {
    PyObject_HEAD_INIT(NULL)
    0,		                      /* ob_size        */
    "_modes.message",                 /* tp_name        */
    sizeof(modesmessage),             /* tp_basicsize   */
    0,                                /* tp_itemsize    */
    (destructor)modesmessage_dealloc, /* tp_dealloc     */
    0,                                /* tp_print       */
    0,                                /* tp_getattr     */
    0,                                /* tp_setattr     */
    (cmpfunc)modesmessage_compare,    /* tp_compare     */
    (reprfunc)modesmessage_repr,      /* tp_repr        */
    0,                                /* tp_as_number   */
    0,                                /* tp_as_sequence */
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
    0,                                /* tp_richcompare */
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
    self->HEAD.ob_type->tp_free((PyObject*)self);
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
        return PyInt_FromLong(n * 25 - 1000);
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

    return PyInt_FromLong(a);
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
    if (!(self->crc = PyInt_FromLong(crc)))
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
            if (!(self->address = PyInt_FromLong( (self->data[1] << 16) | (self->data[2] << 8) | (self->data[3]) )))
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
            if (!(self->address = PyInt_FromLong( (self->data[1] << 16) | (self->data[2] << 8) | (self->data[3]) )))
                return -1;            
            Py_XDECREF(self->altitude); self->altitude = (Py_INCREF(Py_None), Py_None);

            metype = self->data[4] >> 3;
            if ((metype >= 9 && metype <= 18) || (metype >= 20 && metype <= 22)) {
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
    
    /*
    if (self->address == Py_None)
        fprintf(stderr, " AA : None\n");
    else
        fprintf(stderr, " AA : %06lx\n", PyInt_AsLong(self->address));

    if (self->altitude == Py_None)
        fprintf(stderr, " AC : None\n");
    else
        fprintf(stderr, " AC : %ld ft\n", PyInt_AsLong(self->altitude));

    fprintf(stderr, "  V : %s\n", self->valid ? "valid" : "not valid");
    fprintf(stderr, "Even: %s\n", self->even_cpr ? "yes" : "no");
    fprintf(stderr, " Odd: %s\n", self->odd_cpr ? "yes" : "no");
    */

    return 0;
}

static Py_ssize_t modesmessage_getreadbuffer(PyObject *self, Py_ssize_t segment, void **ptrptr)
{
    if (segment != 0) {
        PyErr_SetString(PyExc_SystemError, "segment out of range");
        return -1;
    }

    *ptrptr = ((modesmessage*)self)->data;
    return ((modesmessage*)self)->datalen;
}

static Py_ssize_t modesmessage_segcount(PyObject *self, Py_ssize_t *lenp)
{
    if (lenp)
        *lenp = ((modesmessage*)self)->datalen;
    return 1;
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

static int modesmessage_compare(PyObject *self, PyObject *other)
{
    modesmessage *message1, *message2;

    if (! PyObject_TypeCheck(other, &modesmessageType))
        return -1;

    message1 = (modesmessage*)self;
    message2 = (modesmessage*)other;

    if (message1->datalen != message2->datalen)
        return message1->datalen - message2->datalen;

    return memcmp(message1->data, message2->data, message1->datalen);
}

static char *hexdigit = "0123456789ABCDEF";
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

    return PyString_FromFormat("_modes.message(b'%s',%llu,%u)", buf, (unsigned long long)message->timestamp, (unsigned)message->signal);
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

    return PyString_FromString(buf);
}

static PyMethodDef methods[] = {
    { "crc_residual", crc_residual, METH_VARARGS, "Calculate a CRC residual over a Mode S message." },
    { "packetize_beast_input", packetize_beast_input, METH_VARARGS, "Turn a Beast-form bytestream into a series of messages." },
    { NULL, NULL, 0, NULL }
};

PyMODINIT_FUNC
init_modes(void)
{
    PyObject *module;

    if (PyType_Ready(&modesmessageType) < 0)
        return;
    
    init_crc_table();

    module = Py_InitModule3("_modes", methods, "C helpers to speed up ModeS message processing");
    if (module == NULL)
        return;
    
    Py_INCREF(&modesmessageType);
    PyModule_AddObject(module, "message", (PyObject *)&modesmessageType);
}
