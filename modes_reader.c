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
#include <stdlib.h>
#include <sys/time.h>

static unsigned long long monotic_ms(void) {
    struct timespec ts;
    unsigned long long mst;

    clock_gettime(CLOCK_MONOTONIC, &ts);
    mst = ((unsigned long long) ts.tv_sec) * 1000;
    mst += ts.tv_nsec / (1000 * 1000);
    return mst;
}


/* decoder modes */
typedef enum {
    DECODER_NONE,                   /* Not configured */
    DECODER_BEAST,                  /* Beast binary, freerunning 48-bit timestamp @ 12MHz */
    DECODER_RADARCAPE,              /* Beast binary, 1GHz Radarcape timestamp, UTC synchronized from GPS */
    DECODER_RADARCAPE_EMULATED,     /* Beast binary, 1GHz Radarcape timestamp, not synchronized */
    DECODER_AVR,                    /* AVR, no timestamp */
    DECODER_AVRMLAT,                /* AVR, freerunning 48-bit timestamp @ 12MHz */
    DECODER_SBS,                    /* Kinetic SBS, freerunning 20MHz 24-bit timestamp, wraps around all the time but we try to widen it */
} decoder_mode;

/* A timestamp that indicates the data is synthetic, created from a
 * multilateration result. (FF 00 "MLAT")
 */
#define MAGIC_MLAT_TIMESTAMP 0xFF004D4C4154ULL
#define MAGIC_UAT_TIMESTAMP  0xFF004D4C4155ULL
#define OUTLIER_LIMIT 1

/* a modesreader object */
typedef struct {
    PyObject_HEAD

    /* decoder characteristics */
    decoder_mode decoder_mode;
    const char *decoder_mode_string;
    unsigned long long frequency;
    const char *epoch;

    unsigned long long last_timestamp; /* last seen timestamp */
    unsigned long long last_ts_mono; /* system time associated with last timestamp */
    unsigned long long monotonic; /* current monotonic time */
    unsigned int radarcape_utc_bugfix;

    /* count timestamp outliers, first one is ignored / message discarded / last_timestamp not updated */
    /* two consecutive outliers will result in sending a clock_reset message to the mlat-server (all sync dropped) */
    /* a non outlier message will outliers to zero */
    unsigned int outliers;

    /* configurable bits */
    char allow_mode_change;
    char want_zero_timestamps;
    char want_mlat_messages;
    char want_invalid_messages;
    char want_events;

    /* filtering */
    PyObject *seen;
    PyObject *default_filter;
    PyObject *specific_filter;
    PyObject *modeac_filter;

    /* stats */
    unsigned int received_messages;
    unsigned int suppressed_messages;
    unsigned int mlat_messages;
} modesreader;

/* methods for the modesreader type */
static PyObject *modesreader_new(PyTypeObject *type, PyObject *args, PyObject *kwds);
static int modesreader_init(modesreader *self, PyObject *args, PyObject *kwds);
static void modesreader_dealloc(modesreader *self);
static int modesreader_setmode(modesreader *self, PyObject *mode, void *dummy);
static PyObject *modesreader_getmode(modesreader *self, void *dummy);
static PyObject *modesreader_feed(modesreader *self, PyObject *args, PyObject *kwds);

/* modesreader fields */
static PyMemberDef modesreaderMembers[] = {
    /* these two are derived from the current mode always */
    { "frequency",             T_ULONGLONG, offsetof(modesreader, frequency),             READONLY,  "timestamp frequency" },
    { "epoch",                 T_STRING,    offsetof(modesreader, epoch),                 READONLY,  "timestamp epoch" },

    { "last_timestamp",        T_ULONGLONG, offsetof(modesreader, last_timestamp),        0,         "last timestamp seen"  },
    { "allow_mode_change",     T_BOOL,      offsetof(modesreader, allow_mode_change),     0,         "can the decoder change mode based on status messages it receives?" },
    { "want_zero_timestamps",  T_BOOL,      offsetof(modesreader, want_zero_timestamps),  0,         "should the decoder return messages with zero timestamps?" },
    { "want_mlat_messages",    T_BOOL,      offsetof(modesreader, want_mlat_messages),    0,         "should the decoder return synthetic mlat messages?" },
    { "want_invalid_messages", T_BOOL,      offsetof(modesreader, want_invalid_messages), 0,         "should the decoder return invalid messages?" },
    { "want_events",           T_BOOL,      offsetof(modesreader, want_events),           0,         "should the decoder return metadata events?" },
    { "seen",                  T_OBJECT,    offsetof(modesreader, seen),                  0,         "set of addresses seen by the decoder" },
    { "default_filter",        T_OBJECT,    offsetof(modesreader, default_filter),        0,         "DF accept filter for all aircraft"},
    { "specific_filter",       T_OBJECT,    offsetof(modesreader, specific_filter),       0,         "DF accept filter for specific aircraft"},
    { "modeac_filter",         T_OBJECT,    offsetof(modesreader, modeac_filter),         0,         "Mode A/C accept filter"},
    { "received_messages",     T_UINT,      offsetof(modesreader, received_messages),     0,         "total number of messages decoded"},
    { "suppressed_messages",   T_UINT,      offsetof(modesreader, suppressed_messages),   0,         "number of messages suppressed by filtering"},
    { "mlat_messages",         T_UINT,      offsetof(modesreader, mlat_messages),         0,         "number of incoming MLAT messages received (and ignored)"},
    { NULL, 0, 0, 0, NULL }
};

/* .. and the mode field which has a special getter/setter */
static PyGetSetDef modesreaderGetSet[] = {
    { "mode", (getter)modesreader_getmode, (setter)modesreader_setmode, "decoder mode", NULL },
    { NULL, NULL, NULL, NULL, NULL }
};

/* modesreader methods */
static PyMethodDef modesreaderMethods[] = {
    { "feed", (PyCFunction)modesreader_feed, METH_VARARGS|METH_KEYWORDS, "Process and decode some data." },
    { NULL, NULL, 0, NULL }
};

/* modesreader type definition */
static PyTypeObject modesreaderType = {
    PyVarObject_HEAD_INIT(NULL, 0)
    "_modes.Reader",                  /* tp_name        */
    sizeof(modesreader),              /* tp_basicsize   */
    0,                                /* tp_itemsize    */
    (destructor)modesreader_dealloc,  /* tp_dealloc     */
    0,                                /* tp_print       */
    0,                                /* tp_getattr     */
    0,                                /* tp_setattr     */
    0,                                /* tp_reserved    */
    0,                                /* tp_repr        */
    0,                                /* tp_as_number   */
    0,                                /* tp_as_sequence */
    0,                                /* tp_as_mapping  */
    0,                                /* tp_hash        */
    0,                                /* tp_call        */
    0,                                /* tp_str         */
    PyObject_GenericGetAttr,          /* tp_getattro    */
    PyObject_GenericSetAttr,          /* tp_setattro    */
    0,                                /* tp_as_buffer   */
    Py_TPFLAGS_DEFAULT|Py_TPFLAGS_BASETYPE,  /* tp_flags       */
    "A ModeS stream reader.",         /* tp_doc         */
    0,                                /* tp_traverse    */
    0,                                /* tp_clear       */
    0,                                /* tp_richcompare */
    0,                                /* tp_weaklistoffset */
    0,                                /* tp_iter        */
    0,                                /* tp_iternext    */
    modesreaderMethods,               /* tp_methods     */
    modesreaderMembers,               /* tp_members     */
    modesreaderGetSet,                /* tp_getset      */
    0,                                /* tp_base        */
    0,                                /* tp_dict        */
    0,                                /* tp_descr_get   */
    0,                                /* tp_descr_set   */
    0,                                /* tp_dictoffset  */
    (initproc)modesreader_init,       /* tp_init        */
    0,                                /* tp_alloc       */
    modesreader_new,                  /* tp_new         */
};

/* lookup table for decoder_mode <-> python strings */
static struct {
    decoder_mode mode;
    const char *cstr;
    PyObject *pystr;
} modetable[] = {
    { DECODER_BEAST,                  "BEAST", NULL },
    { DECODER_RADARCAPE,              "RADARCAPE", NULL },
    { DECODER_RADARCAPE_EMULATED,     "RADARCAPE_EMULATED", NULL },
    { DECODER_AVR,                    "AVR", NULL },
    { DECODER_AVRMLAT,                "AVRMLAT", NULL },
    { DECODER_SBS,                    "SBS", NULL },
    { DECODER_NONE,                   NULL, NULL }
};

/* internal helpers */
static PyObject *feed_beast(modesreader *self, Py_buffer *buf, int max_messages);
static PyObject *feed_avr(modesreader *self, Py_buffer *buf, int max_messages);
static PyObject *feed_sbs(modesreader *self, Py_buffer *buf, int max_messages);
static void set_decoder_mode(modesreader *self, decoder_mode newmode);
static PyObject *radarcape_settings_to_list(uint8_t settings);
static PyObject *radarcape_status_to_dict(uint8_t *message);
static int filter_message(modesreader *self, PyObject *message);

/*
 * module setup/teardown
 */
int modesreader_module_init(PyObject *m)
{
    int i;

    if (PyType_Ready(&modesreaderType) < 0)
        goto error;

    for (i = 0; modetable[i].cstr != NULL; ++i) {
        PyObject *pystr = PyUnicode_FromString(modetable[i].cstr);
        if (pystr == NULL) {
            goto error;
        }

        Py_INCREF(pystr);
        modetable[i].pystr = pystr;
        if (PyModule_AddObject(m, modetable[i].cstr, pystr) < 0)
            goto error;
    }

    Py_INCREF(&modesreaderType);
    if (PyModule_AddObject(m, "Reader", (PyObject *)&modesreaderType) < 0) {
        Py_DECREF(&modesreaderType);
        goto error;
    }

    return 0;

 error:
    for (i = 0; modetable[i].cstr != NULL; ++i) {
        Py_CLEAR(modetable[i].pystr);
    }
    return -1;
}

void modesreader_module_free(PyObject *m)
{
    int i;

    for (i = 0; modetable[i].cstr != NULL; ++i) {
        Py_CLEAR(modetable[i].pystr);
    }
}

static PyObject *modesreader_new(PyTypeObject *type, PyObject *args, PyObject *kwds)
{
    modesreader *self;

    self = (modesreader *)type->tp_alloc(type, 0);
    if (self == NULL)
        return NULL;

    /* minimal init */
    set_decoder_mode(self, DECODER_NONE);
    self->last_timestamp = 0;
    self->last_ts_mono = 0;
    self->monotonic = 0;
    self->outliers = 0;
    self->allow_mode_change = 1;
    self->want_zero_timestamps = 0;
    self->want_mlat_messages = 0;
    self->want_invalid_messages = 0;
    self->want_events = 1;

    Py_INCREF(Py_None); self->seen = Py_None;
    Py_INCREF(Py_None); self->default_filter = Py_None;
    Py_INCREF(Py_None); self->specific_filter = Py_None;
    Py_INCREF(Py_None); self->modeac_filter = Py_None;

    self->received_messages = self->suppressed_messages = self->mlat_messages = 0;

    return (PyObject *)self;
}

static void modesreader_dealloc(modesreader *self)
{
    Py_CLEAR(self->seen);
    Py_CLEAR(self->default_filter);
    Py_CLEAR(self->specific_filter);
    Py_CLEAR(self->modeac_filter);

    Py_TYPE(self)->tp_free((PyObject*)self);
}

static int modesreader_init(modesreader *self, PyObject *args, PyObject *kwds)
{
    static char *kwlist[] = { "mode", NULL };
    PyObject *mode = Py_None;

    if (!PyArg_ParseTupleAndKeywords(args, kwds, "|O", kwlist, &mode))
        return -1;

    if (modesreader_setmode(self, mode, NULL)  < 0)
        return -1;

    return 0;
}

static int modesreader_setmode(modesreader *self, PyObject *mode, void *dummy)
{
    int i;

    if (mode == Py_None) {
        set_decoder_mode(self, DECODER_NONE);
        return 0;
    }

    for (i = 0; modetable[i].cstr != NULL; ++i) {
        int res = PyObject_RichCompareBool(modetable[i].pystr, mode, Py_EQ);
        if (res < 0)
            return -1;

        if (res == 1) {
            set_decoder_mode(self, modetable[i].mode);
            break;
        }
    }

    if (modetable[i].cstr == NULL) {
        PyErr_SetString(PyExc_ValueError, "unrecognized decoder mode");
        return -1;
    }

    return 0;
}

static PyObject *modesreader_getmode(modesreader *self, void *dummy)
{
    int i;

    for (i = 0; modetable[i].cstr; ++i) {
        if (self->decoder_mode == modetable[i].mode) {
            Py_INCREF(modetable[i].pystr);
            return modetable[i].pystr;
        }
    }

    Py_INCREF(Py_None);
    return Py_None;
}

/* feed some data to the reader and does one of:
 *  1) returns a tuple (bytes_consumed, messages, error_pending), or
 *  2) throws an exception
 *
 * If a stream error is seen, but some messages were parsed OK,
 * then an exception is not immediately thrown and the parsed
 * messages are returned with error_pending = True. The caller
 * should call feed again (after consuming the given number of
 * bytes) to get the exception.
 *
 * Internal errors (e.g. out of memory) are thrown immediately.
 */
static PyObject *modesreader_feed(modesreader *self, PyObject *args, PyObject *kwds)
{
    Py_buffer buffer;
    PyObject *rv = NULL;
    int max_messages = 0;
    static char *kwlist[] = { "buffer", "max_messages", NULL };

    if (!PyArg_ParseTupleAndKeywords(args, kwds, "y*|i", kwlist, &buffer, &max_messages))
        return NULL;

    if (buffer.itemsize != 1) {
        PyErr_SetString(PyExc_ValueError, "buffer itemsize is not 1");
        goto out;
    }

    if (!PyBuffer_IsContiguous(&buffer, 'C')) {
        PyErr_SetString(PyExc_ValueError, "buffer is not contiguous");
        goto out;
    }

    switch (self->decoder_mode) {
    case DECODER_NONE:
        PyErr_SetString(PyExc_NotImplementedError, "decoder mode is None, no decoder type selected");
        break;

    case DECODER_BEAST:
    case DECODER_RADARCAPE:
    case DECODER_RADARCAPE_EMULATED:
        rv = feed_beast(self, &buffer, max_messages);
        break;

    case DECODER_AVR:
    case DECODER_AVRMLAT:
        rv = feed_avr(self, &buffer, max_messages);
        break;

    case DECODER_SBS:
        rv = feed_sbs(self, &buffer, max_messages);
        break;

    default:
        PyErr_Format(PyExc_AssertionError, "decoder somehow got into illegal mode %d", (int)self->decoder_mode);
        break;
    }

 out:
    PyBuffer_Release(&buffer);
    return rv;
}

static void set_decoder_mode(modesreader *self, decoder_mode newmode)
{
    self->decoder_mode = newmode;
    switch (newmode) {
    case DECODER_BEAST:
        self->frequency = 12000000ULL; /* assumed */
        self->epoch = NULL;
        break;

    case DECODER_RADARCAPE:
        self->frequency = 1000000000ULL;
        self->epoch = "utc_midnight";
        break;

    case DECODER_RADARCAPE_EMULATED:
        self->frequency = 1000000000ULL;
        self->epoch = NULL;
        break;

    case DECODER_AVRMLAT:
        self->frequency = 12000000ULL; /* assumed */
        self->epoch = NULL;
        break;

    case DECODER_SBS:
        self->frequency = 20000000ULL;
        self->epoch = NULL;
        break;

    case DECODER_AVR:
    default:
        self->frequency = 0;
        self->epoch = NULL;
        break;
    }
}

/* turn a radarcape DIP switch setting byte into a Python list of settings strings */
static PyObject *radarcape_settings_to_list(uint8_t settings)
{
    return Py_BuildValue("[s,s,s,s,s,s,s]",
                         settings & 0x01 ? "beast" : (settings & 0x04 ? "avrmlat" : "avr"),
                         settings & 0x02 ? "filtered_frames" : "all_frames",
                         settings & 0x08 ? "no_crc" : "check_crc",
                         settings & 0x10 ? "gps_timestamps" : "legacy_timestamps",
                         settings & 0x20 ? "rtscts" : "no_rtscts",
                         settings & 0x40 ? "no_fec" : "fec",
                         settings & 0x80 ? "modeac" : "no_modeac");
}

/* turn a radarcape GPS status byte into a Python dict */
static PyObject *radarcape_gpsstatus_to_dict(uint8_t status)
{
    if (!(status & 0x80)) {
        return Py_BuildValue("{s:O,s:O}",
                             "utc_bugfix", Py_False,
                             "timestamp_ok", Py_True
                             );
    }

    return Py_BuildValue("{s:O,s:O,s:O,s:O,s:O,s:O,s:O}",
                         "utc_bugfix",    Py_True,
                         "timestamp_ok",  (status & 0x20 ? Py_False : Py_True),
                         "sync_ok",       (status & 0x10 ? Py_True : Py_False),
                         "utc_offset_ok", (status & 0x08 ? Py_True : Py_False),
                         "sats_ok",       (status & 0x04 ? Py_True : Py_False),
                         "tracking_ok",   (status & 0x02 ? Py_True : Py_False),
                         "antenna_ok",    (status & 0x01 ? Py_True : Py_False));
}

/* turn a radarcape 0x34 status message into a Python dict */
static PyObject *radarcape_status_to_dict(uint8_t *message)
{
    return Py_BuildValue("{s:N,s:i,s:N}",
                         "settings", radarcape_settings_to_list(message[0]),
                         "timestamp_pps_delta", (int)(int8_t)message[1],
                         "gps_status", radarcape_gpsstatus_to_dict(message[2]));
}

/* create an event message for a timestamp jump */
static PyObject *make_timestamp_jump_event(modesreader *self, unsigned long long timestamp)
{
    PyObject *eventdata = Py_BuildValue("{s:K}",
                                        "last-timestamp", self->last_timestamp);
    if (eventdata == NULL)
        return NULL;

    return modesmessage_new_eventmessage(DF_EVENT_TIMESTAMP_JUMP, timestamp, eventdata);
}

/* create an event message for a decoder mode change. the new mode should already be set. */
static PyObject *make_mode_change_event(modesreader *self)
{
    PyObject *eventdata = Py_BuildValue("{s:N,s:K,s:s}",
                                        "mode", modesreader_getmode(self, NULL),
                                        "frequency", self->frequency,
                                        "epoch", self->epoch);
    if (eventdata == NULL)
        return NULL;

    return modesmessage_new_eventmessage(DF_EVENT_MODE_CHANGE, 0, eventdata);
}

/* create an event message for an epoch rollover (e.g. GPS end of day) */
static PyObject *make_epoch_rollover_event(modesreader *self, unsigned long long timestamp)
{
    PyObject *eventdata = PyDict_New();
    if (eventdata == NULL)
        return NULL;

    return modesmessage_new_eventmessage(DF_EVENT_EPOCH_ROLLOVER, timestamp, eventdata);
}

/* create an event message for a radarcape status report */
static PyObject *make_radarcape_status_event(modesreader *self, unsigned long long timestamp, uint8_t *data)
{
    PyObject *eventdata = radarcape_status_to_dict(data);
    if (eventdata == NULL)
        return NULL;

    return modesmessage_new_eventmessage(DF_EVENT_RADARCAPE_STATUS, timestamp, eventdata);
}

/* create an event message for a radarcape position report */
static PyObject *radarcape_position_to_dict(uint8_t *data)
{
    float lat, lon, alt;

    lat = _PyFloat_Unpack4(data + 4, 1);
    if (lat == -1.0 && PyErr_Occurred())
        return NULL;

    lon = _PyFloat_Unpack4(data + 8, 1);
    if (lon == -1.0 && PyErr_Occurred())
        return NULL;

    alt = _PyFloat_Unpack4(data + 12, 1);
    if (alt == -1.0 && PyErr_Occurred())
        return NULL;

    return Py_BuildValue("{s:f,s:f,s:f}",
                         "lat", lat,
                         "lon", lon,
                         "alt", alt);
}

static PyObject *make_radarcape_position_event(modesreader *self, uint8_t *data)
{
    PyObject *eventdata = radarcape_position_to_dict(data);
    if (eventdata == NULL)
        return NULL;

    return modesmessage_new_eventmessage(DF_EVENT_RADARCAPE_POSITION, 0, eventdata);
}

static int is_synthetic_timestamp(unsigned long long timestamp)
{
    return (timestamp == 0 || (timestamp >= MAGIC_MLAT_TIMESTAMP && timestamp <= MAGIC_MLAT_TIMESTAMP + 10));
}

/* check if the given timestamp is in range (not a jump), return 1 if it is */
static int timestamp_check(modesreader *self, unsigned long long timestamp)
{
    if (is_synthetic_timestamp(timestamp))
        return 1;

    if (self->frequency == 0)
        return 1;

    self->monotonic = monotic_ms(); // update system time

    if (self->last_timestamp == 0)
        return 1;


    long long ts_elapsed = (long long) timestamp - (long long) self->last_timestamp;
    long long sys_elapsed = (self->monotonic - self->last_ts_mono) * (self->frequency / 1000);
    long long max_offset = 1.25 * self->frequency; // 1.25 seconds

    if (ts_elapsed > sys_elapsed + max_offset || ts_elapsed < sys_elapsed - max_offset) {
        if (self->outliers == 0) {
            double tosec = 1.0 / self->frequency;
            fprintf(stderr, "outlier detected with ts: %.3f, last_ts: %.3f, ts_elapsed: %.3f, sys_elapsed: %.3f (values in seconds)\n", timestamp * tosec, self->last_timestamp * tosec, ts_elapsed * tosec, sys_elapsed * tosec);
        }
        self->outliers++;
        return 0;
    }
    self->outliers = 0;

    return 1;
}

/* update self->last_timestamp given that we just saw this timestamp */
static void timestamp_update(modesreader *self, unsigned long long timestamp)
{
    if (is_synthetic_timestamp(timestamp)) {
        /* special timestamps, don't use them */
        return;
    }

    if (self->last_timestamp == 0 || self->frequency == 0) {
        /* startup cases, just accept whatever */
        self->last_ts_mono = self->monotonic;
        self->last_timestamp = timestamp;
        return;
    }

    if (self->last_timestamp > timestamp && (self->last_timestamp - timestamp) < 90 * self->frequency) {
        /* ignore small moves backwards */
        return;
    }

    if ((self->decoder_mode == DECODER_RADARCAPE || self->decoder_mode == DECODER_RADARCAPE_EMULATED) &&
        timestamp >= (86340 * 1000000000ULL) && self->last_timestamp <= (60 * 1000000000ULL)) {
        /* in radarcape mode, don't allow last_timestamp to roll back to the previous day
         * as we will have already issued an epoch reset
         */
        return;
    }

    // don't update the timestamp for outliers until we exceed OUTLIER_LIMIT
    if (self->outliers && self->outliers <= OUTLIER_LIMIT)
        return;

    self->last_timestamp = timestamp;
    self->last_ts_mono = self->monotonic;
}

/* feed implementation for Beast-format data (including Radarcape) */
static PyObject *feed_beast(modesreader *self, Py_buffer *buffer, int max_messages)
{
    PyObject *rv = NULL;
    uint8_t *buffer_start, *p, *eod;
    int message_count = 0;
    PyObject *message_tuple = NULL;
    PyObject **messages = NULL;
    int error_pending = 0;

    buffer_start = buffer->buf;

    if (max_messages <= 0) {
        /* allocate the maximum size we might need, given a minimal encoding of:
         *   <1A> <'1'> <6 bytes timestamp> <1 byte signal> <2 bytes message> = 11 bytes total
         */
        max_messages = buffer->len / 11 + 2;
    }

    messages = calloc(max_messages, sizeof(PyObject*));
    if (!messages) {
        PyErr_NoMemory();
        goto out;
    }

    /* parse messages */
    p = buffer_start;
    eod = buffer_start + buffer->len;
    while (p+2 <= eod && message_count+2 < max_messages) {
        int message_len = -1;
        uint64_t timestamp;
        uint8_t signal;
        uint8_t data[14];
        uint8_t *m, *eom;
        int i;
        uint8_t type;
        PyObject *message;
        int wanted;
        int has_timestamp_signal;

        if (p[0] != 0x1a) {
            error_pending = 1;
            if (message_count > 0)
                goto nomoredata;
            PyErr_Format(PyExc_ValueError, "Lost sync with input stream: expected a 0x1A marker at offset %d but found 0x%02x instead", (int) (p - buffer_start), (int)p[0]);
            goto out;
        }

        has_timestamp_signal = 1;
        type = p[1];
        switch (type) {
        case '1': message_len = 2; break; /* mode A/C */
        case '2': message_len = 7; break; /* mode S short */
        case '3': message_len = 14; break; /* mode S long */
        case '4': message_len = 14; break; /* radarcape status message */
        case '5':
            /* radarcape position message, no timestamp/signal bytes */
            message_len = 21;
            has_timestamp_signal = 0;
            break;
        default:
            error_pending = 1;
            if (message_count > 0)
                goto nomoredata;
            PyErr_Format(PyExc_ValueError, "Lost sync with input stream: unexpected message type 0x%02x after 0x1A marker at offset %d", (int)p[1], (int) (p - buffer_start));
            goto out;
        }

        m = p + 2;
        eom = m + message_len + (has_timestamp_signal ? 7 : 0);
        if (eom > eod)
            break;

#define ADVANCE \
        do {                                                            \
            if (*m++ == 0x1a) {                                         \
                if (m < eod && *m != 0x1a) {                            \
                    error_pending = 1;                                  \
                    if (message_count > 0)                              \
                        goto nomoredata;                                \
                    PyErr_SetString(PyExc_ValueError, "Lost sync with input stream: expected 0x1A after 0x1A escape"); \
                    goto out;                                           \
                }                                                       \
                ++m, ++eom;                                             \
                if (eom > eod)                                          \
                    goto nomoredata;                                    \
            }                                                           \
        } while(0)

        if (has_timestamp_signal) {
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
        } else {
            timestamp = 0;
            signal = 0;
        }

        /* message, N bytes */
        for (i = 0; i < message_len; ++i) {
            data[i] = *m;
            ADVANCE;
        }

        /* do some filtering */

        if (type == '4') {
            /* radarcape-style status message, use this to switch our decoder type */

            self->radarcape_utc_bugfix = (data[2] & 0x80) == 0x80;

            if (self->allow_mode_change) {
                decoder_mode newmode;
                if (data[0] & 0x10) {
                    /* radarcape in GPS timestamp mode */
                    if ((data[2] & 0x20) == 0x20) {
                        newmode = DECODER_RADARCAPE_EMULATED;
                    } else {
                        newmode = DECODER_RADARCAPE;
                    }
                } else {
                    /* radarcape in 12MHz timestamp mode */
                    newmode = DECODER_BEAST;
                }

                /* handle mode changes by inserting an event message */
                if (newmode != self->decoder_mode) {
                    set_decoder_mode(self, newmode);
                    if (self->want_events) {
                        if (! (messages[message_count++] = make_mode_change_event(self)))
                        goto out;
                    }
                }
            }
        }

        if (has_timestamp_signal && !is_synthetic_timestamp(timestamp)) {
            if (self->decoder_mode == DECODER_BEAST) {
                /* 12MHz mode */

                /* check for very out of range value
                 * (dump1090 can hold messages for up to 60 seconds! so be conservative here)
                 * also work around dump1090-mutability issue #47 which can send very stale Mode A/C messages
                 */
                if (self->want_events && type != '1' && !timestamp_check(self, timestamp)) {
                    if (self->outliers > OUTLIER_LIMIT &&
                            ! (messages[message_count++] = make_timestamp_jump_event(self, timestamp)))
                        goto out;
                }

                /* adjust the timestamps so they always reflect the start of the frame */
                uint64_t adjust;
                if (type == '1') {
                    // Mode A/C, timestamp reported at F2 which is 20.3us after F1
                    // this is 243.6 cycles at 12MHz
                    adjust = 244;
                } else if (type == '2') {
                    // Mode S short, timestamp reported at end of frame, frame is 8us preamble plus 56us data
                    // this is 768 cycles at 12MHz
                    adjust = 768;
                } else if (type == '3') {
                    // Mode S long, timestamp reported halfway through the frame (at bit 56), same offset as Mode S short
                    adjust = 768;
                } else {
                    // anything else we assume is already correct
                    adjust = 0;
                }

                if (timestamp < adjust) {
                    timestamp = 0;
                } else {
                    timestamp = timestamp - adjust;
                }
            } else {
                /* gps mode */

                /* adjust timestamp so that it is a contiguous nanoseconds-since-
                 * midnight value, rather than the raw form which skips values once
                 * a second
                 */
                uint64_t nanos = timestamp & 0x00003FFFFFFF;
                uint64_t secs = timestamp >> 30;

                if (!self->radarcape_utc_bugfix) {
                    /* fix up the timestamp so it is UTC, not 1 second ahead */
                    if (secs == 0) {
                        secs = 86399;
                    } else {
                        --secs;
                    }
                }

                timestamp = nanos + secs * 1000000000;

                /* adjust the timestamps so they always reflect the start of the frame */
                uint64_t adjust;
                if (type == '1') {
                    // Mode A/C, timestamp reported at F2 which is 20.3us after F1
                    adjust = 20300;
                } else if (type == '2') {
                    // Mode S short, timestamp reported at end of frame, frame is 8us preamble plus 56us data
                    adjust = 64000;
                } else if (type == '3') {
                    // Mode S long, timestamp reported at end of frame, frame is 8us preamble plus 112us data
                    adjust = 120000;
                } else {
                    // anything else we assume is already correct
                    adjust = 0;
                }

                if (adjust <= timestamp) {
                    timestamp = timestamp - adjust;
                } else {
                    /* wrap it to the previous day */
                    timestamp = timestamp + 86400 * 1000000000ULL - adjust;
                }

                /* check for end of day rollover */
                if (self->want_events && self->last_timestamp >= (86340 * 1000000000ULL) && timestamp <= (60 * 1000000000ULL)) {
                    if (! (messages[message_count++] = make_epoch_rollover_event(self, timestamp)))
                        goto out;
                } else if (self->want_events && type != '1' && !timestamp_check(self, timestamp)) {
                    if (! (messages[message_count++] = make_timestamp_jump_event(self, timestamp)))
                        goto out;
                }
            }

            if (type != '1') {
                timestamp_update(self, timestamp);
            }
        }

        if (type == '4') {
            /* radarcape-style status message, emit the status event if wanted */
            if (self->want_events) {
                if (! (messages[message_count++] = make_radarcape_status_event(self, timestamp, data)))
                    goto out;
            }

            /* don't try to process this as a Mode S message */
            p = m;
            continue;
        }

        if (type == '5') {
            /* radarcape-style position message, emit the position event if wanted */

            if (self->want_events) {
                if (! (messages[message_count++] = make_radarcape_position_event(self, data)))
                    goto out;
            }

            /* don't try to process this as a Mode S message */
            p = m;
            continue;
        }

        /* it's a Mode A/C or Mode S message, parse it */
        if (! (message = modesmessage_from_buffer(timestamp, signal, data, message_len)))
            goto out;

        /* apply filters, update seen-set */
        ++self->received_messages;
        wanted = filter_message(self, message);
        if (wanted < 0)
            goto out;
        else if (wanted)
            messages[message_count++] = message;
        else {
            ++self->suppressed_messages;
            Py_DECREF(message);
        }

        p = m;
    }

 nomoredata:
    if (! (message_tuple = PyTuple_New(message_count)))
        goto out;

    while (--message_count >= 0) {
        PyTuple_SET_ITEM(message_tuple, message_count, messages[message_count]); /* steals ref */
    }

    rv = Py_BuildValue("(l,N,N)", (long) (p - buffer_start), message_tuple, PyBool_FromLong(error_pending));

 out:
    while (--message_count >= 0) {
        Py_XDECREF(messages[message_count]);
    }
    free(messages);
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
static PyObject *feed_sbs(modesreader *self, Py_buffer *buffer, int max_messages)
{
    PyObject *rv = NULL;
    uint8_t *buffer_start, *p, *eod;
    int message_count = 0;
    PyObject *message_tuple = NULL;
    PyObject **messages = NULL;
    int error_pending = 0;

    buffer_start = buffer->buf;

    if (max_messages <= 0) {
        /* allocate the maximum size we might need, given a minimal encoding of:
         *   <DLE> <STX> <0x09> <n/a> <3 bytes timestamp> <2 bytes message> <DLE> <ETX> <2 bytes CRC> = 13 bytes total
         */
        max_messages = buffer->len / 13 + 1;
    }

    messages = calloc(max_messages, sizeof(PyObject*));
    if (!messages) {
        PyErr_NoMemory();
        goto out;
    }

    /* parse messages */
    p = buffer_start;
    eod = buffer_start + buffer->len;
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
        uint32_t crc;
        PyObject *message;

        if (p[0] != 0x10 || p[1] != 0x02) {
            error_pending = 1;
            if (message_count > 0)
                goto nomoredata;
            PyErr_Format(PyExc_ValueError, "Lost sync with input stream: expected DLE STX at offset %d but found 0x%02x 0x%02x instead", (int) (p - buffer_start), (int)p[0], (int)p[1]);
            goto out;
        }

        /* scan for DLE ETX, copy data */
        m = p + 2;
        i = 0;
        while (m < eod) {
            if (*m == 0x10) {
                /* DLE <something> */

                if ((m+1) >= eod)
                    goto nomoredata;

                if (m[1] == 0x03) {
                    /* DLE ETX */
                    break;
                }

                if (m[1] != 0x10) {
                    /* DLE <something we don't understand> */
                    error_pending = 1;
                    if (message_count > 0)
                        goto nomoredata;
                    PyErr_Format(PyExc_ValueError, "Lost sync with input stream: unexpected DLE 0x%02x at offset %d", (int) (m - buffer_start), (int)m[1]);
                    goto out;
                }

                /* DLE DLE */
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
                error_pending = 1;
                if (message_count > 0)
                    goto nomoredata;
                PyErr_Format(PyExc_ValueError, "Lost sync with input stream: unexpected DLE 0x%02x at offset %d", (int) (m - buffer_start), (int)*m);
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
                error_pending = 1;
                if (message_count > 0)
                    goto nomoredata;
                PyErr_Format(PyExc_ValueError, "Lost sync with input stream: unexpected DLE 0x%02x at offset %d", (int) (m - buffer_start), (int)*m);
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
            p = m;
            continue;
        }

        if ((5 + message_len) > i) {
            /* not enough data */
            p = m;
            continue;
        }

        /* regenerate message CRC */
        crc = modescrc_buffer_crc(&data[5], message_len - 3);
        data[5 + message_len - 3] ^= crc >> 16;
        data[5 + message_len - 2] ^= crc >> 8;
        data[5 + message_len - 1] ^= crc;

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

        /* we don't use timestamp_update or timestamp_check here because SBS is "special" */

        /* The SBS timestamp is only 24 bits wide; at 20MHz this overflows more than once
         * a second (about every 839ms). To get a useful timestamp for mlat synchronization,
         * we have to widen the timestamp.
         *
         * It wasn't reliable to do this based on the system clock, there are enough
         * unpredictable delays between the SBS and mlat-client that it didn't work well.
         * Instead, we assume that we will be receiving at least one message per 839ms.
         * so if we ever see a timestamp that has gone backwards, it must be due to
         * exactly one overflow of the timestamp counter.
         *
         * This is usually true in cases where we see enough traffic for mlat/sync. When it
         * isn't true, you will get synchronization jumps that are a multiple of 839ms.
         */

        /* merge in top bits of the current widened counter */
        timestamp = timestamp | (self->last_timestamp & 0xFFFFFFFFFF000000ULL);

        /* check for rollover, if it happened then increase the widened part */
        if (timestamp < self->last_timestamp)
            timestamp += (1 << 24);

        self->last_timestamp = timestamp;

        /* decode it */
        if (! (message = modesmessage_from_buffer(timestamp, 0, &data[5], message_len)))
            goto out;

        /* apply filters, update seen-set */
        ++self->received_messages;
        int wanted = filter_message(self, message);
        if (wanted < 0)
            goto out;
        else if (wanted)
            messages[message_count++] = message;
        else {
            ++self->suppressed_messages;
            Py_DECREF(message);
        }

        p = m;
    }

 nomoredata:
    if (! (message_tuple = PyTuple_New(message_count)))
        goto out;

    while (--message_count >= 0) {
        PyTuple_SET_ITEM(message_tuple, message_count, messages[message_count]); /* steals ref */
    }

    rv = Py_BuildValue("(l,N,N)", (long) (p - buffer_start), message_tuple, PyBool_FromLong(error_pending));

 out:
    while (--message_count >= 0) {
        Py_XDECREF(messages[message_count]);
    }
    free(messages);
    return rv;
}

/********** AVR INPUT **************/

static int hexvalue(char c)
{
    if (c >= '0' && c <= '9')
        return c - '0';
    else if (c >= 'a' && c <= 'f')
        return c - 'a' + 10;
    else if (c >= 'A' && c <= 'F')
        return c - 'A' + 10;
    else
        return -1;
}

static PyObject *feed_avr(modesreader *self, Py_buffer *buffer, int max_messages)
{
    PyObject *rv = NULL;
    uint8_t *buffer_start, *p, *eod;
    int message_count = 0;
    PyObject *message_tuple = NULL;
    PyObject **messages = NULL;
    int error_pending = 0;

    buffer_start = buffer->buf;

    if (max_messages <= 0) {
        /* allocate the maximum size we might need, given a minimal encoding of:
         *   '*' <2 bytes message> ';' LF
         */
        max_messages = buffer->len / 5 + 1;
    }

    messages = calloc(max_messages, sizeof(PyObject*));
    if (!messages) {
        PyErr_NoMemory();
        goto out;
    }

    p = buffer_start;
    eod = buffer_start + buffer->len;
    while (p+17 <= eod && message_count+1 < max_messages) {
        int message_len = -1;
        uint64_t timestamp;
        uint8_t data[14];
        uint8_t message_format;
        int i;
        uint8_t *m;
        PyObject *message;

        message_format = p[0];
        if (message_format != '@' &&
            message_format != '%' &&
            message_format != '<' &&
            message_format != '*' &&
            message_format != ':') {
            error_pending = 1;
            if (message_count > 0)
                goto nomoredata;
            PyErr_Format(PyExc_ValueError, "Lost sync with input stream: expected '@'/'%%'/'<'/'*'/':' at offset %d but found 0x%02x instead",
                         (int) (p - buffer_start), (int)p[0]);
            goto out;
        }

        m = p + 1;
        if (message_format == '@' ||
            message_format == '%' ||
            message_format == '<') {
            /* read 6 bytes of timestamp */
            timestamp = 0;
            for (i = 0; i < 12; ++i, ++m) {
                int c;

                if (m >= eod) {
                    goto nomoredata;
                }

                timestamp <<= 4;
                c = hexvalue(*m);
                if (c >= 0) {
                    timestamp |= c;
                } else {
                    error_pending = 1;
                    if (message_count > 0)
                        goto nomoredata;
                    PyErr_Format(PyExc_ValueError, "Lost sync with input stream: expected a hex digit at offset %d but found 0x%02x instead",
                                 (int) (m - buffer_start), (int)*m);
                    goto out;
                }
            }
        } else {
            /* AVR format with no timestamp */
            timestamp = 0;
        }

        if (message_format == '<') {
            /* in format '<', skip 1 byte of signal */
            m += 2;
            if (m >= eod)
                goto nomoredata;
        }

        /* read 2-14 bytes of data */
        message_len = 0;
        while (message_len < 14) {
            int c0, c1;

            if (m+1 >= eod) {
                goto nomoredata;
            }

            if (m[0] == ';') {
                break; /* end of message marker */
            } else {
                c0 = hexvalue(m[0]);
                if (c0 < 0) {
                    error_pending = 1;
                    if (message_count > 0)
                        goto nomoredata;

                    PyErr_Format(PyExc_ValueError, "Lost sync with input stream: expected a hex digit at offset %d but found 0x%02x instead",
                                 (int) (m - buffer_start), (int)m[0]);
                    goto out;
                }
            }

            c1 = hexvalue(m[1]);
            if (c1 < 0) {
                error_pending = 1;
                if (message_count > 0)
                    goto nomoredata;

                PyErr_Format(PyExc_ValueError, "Lost sync with input stream: expected a hex digit at offset %d but found 0x%02x instead",
                             (int) (m - buffer_start), (int)m[1]);
                goto out;
            }

            if (message_len < 14) {
                data[message_len] = (c0 << 4) | c1;
            }
            ++message_len;
            m += 2;
        }

        /* consume ';' */
        if (m >= eod)
            goto nomoredata;
        if (*m != ';') {
            error_pending = 1;
            if (message_count > 0)
                goto nomoredata;

            PyErr_Format(PyExc_ValueError, "Lost sync with input stream: expected ';' at offset %d but found 0x%02x instead",
                         (int) (m - buffer_start), (int)*m);
            goto out;
        }

        /* CR LF, LF CR, LF all seen! ugh. */

        /* skip until CR or LF */
        while (m < eod && *m != '\r' && *m != '\n')
            ++m;

        /* consume however many CRs and LFs */
        while (m < eod && (*m == '\r' || *m == '\n'))
            ++m;

        /* check length */
        if (message_len != 2 && message_len != 7 && message_len != 14) {
            error_pending = 1;
            if (message_count > 0)
                goto nomoredata;

            PyErr_Format(PyExc_ValueError, "Lost sync with input stream: unexpected %d-byte message starting at offset %d",
                         message_len, (int) (p - buffer_start));
            goto out;
        }

        /* check for very out of range value
         * (dump1090 can hold messages for up to 60 seconds! so be conservative here)
         * also work around dump1090-mutability issue #47 which can send very stale Mode A/C messages
         */
        if (self->want_events && message_len != 2 && !timestamp_check(self, timestamp)) {
            if (! (messages[message_count++] = make_timestamp_jump_event(self, timestamp)))
                goto out;
        }

        timestamp_update(self, timestamp);

        /* decode it */
        if (! (message = modesmessage_from_buffer(timestamp, 0, data, message_len)))
            goto out;

        /* apply filters, update seen-set */
        ++self->received_messages;
        int wanted = filter_message(self, message);
        if (wanted < 0)
            goto out;
        else if (wanted)
            messages[message_count++] = message;
        else {
            ++self->suppressed_messages;
            Py_DECREF(message);
        }

        /* next message */
        p = m;
    }

 nomoredata:
    if (! (message_tuple = PyTuple_New(message_count)))
        goto out;

    while (--message_count >= 0) {
        PyTuple_SET_ITEM(message_tuple, message_count, messages[message_count]); /* steals ref */
    }

    rv = Py_BuildValue("(l,N,N)", (long) (p - buffer_start), message_tuple, PyBool_FromLong(error_pending));

 out:
    while (--message_count >= 0) {
        Py_XDECREF(messages[message_count]);
    }
    free(messages);
    return rv;
}

/* inspect a message, update the seen set
 * return 1 if we should pass this message on to the caller
 * return 0 if we should drop it
 * return -1 on internal error (exception has been raised)
 */
static int filter_message(modesreader *self, PyObject *o)
{
    modesmessage *message = (modesmessage *)o;

    // Check this, first.  We don't really want to use MLAT msgs...
    if (message->timestamp == MAGIC_MLAT_TIMESTAMP && !self->want_mlat_messages) {
        ++self->mlat_messages;
        return 0;
    }

    // Drop messages as long as timestamps are jumping.
    if (self->outliers > 0)
        return 0;

    // Ignore messages that jump backwards
    if (self->last_timestamp > message->timestamp)
        return 0;

    if (message->df == DF_MODEAC) {
        if (self->modeac_filter != NULL && self->modeac_filter != Py_None) {
            return PySequence_Contains(self->modeac_filter, message->address);
        }

        return 1;
    }

    if (!message->valid) {
        return self->want_invalid_messages; /* don't process further, contents are dubious */
    }

    if (self->seen != NULL && self->seen != Py_None) {
        if (message->df == 11 || message->df == 17 || message->df == 18) {
            /* note that we saw this aircraft, even if the message is filtered.
             * only do this for CRC-checked messages as we get a lot of noise
             * otherwise.
             */
            if (PySet_Add(self->seen, message->address) < 0) {
                return -1;
            }
        }
    }

    if (message->timestamp == 0 && !self->want_zero_timestamps) {
        return 0;
    }

    if ((self->default_filter == NULL || self->default_filter == Py_None) &&
        (self->specific_filter == NULL || self->specific_filter == Py_None)) {
        /* no filters installed, match everything */
        return 1;
    }

    /* check per-type filters */
    if (self->default_filter != NULL && self->default_filter != Py_None) {
        int rv;
        PyObject *entry = PySequence_GetItem(self->default_filter, message->df);
        if (entry == NULL)
            return -1;

        rv = PyObject_IsTrue(entry);
        Py_DECREF(entry);
        if (rv != 0)
            return rv;
    }

    if (self->specific_filter != NULL && self->specific_filter != Py_None) {
        int rv;
        PyObject *entry = PySequence_GetItem(self->specific_filter, message->df);
        if (entry == NULL)
            return -1;

        if (entry == Py_None) {
            rv = 0;
        } else {
            rv = PySequence_Contains(entry, message->address);
        }

        Py_DECREF(entry);
        if (rv != 0)
            return rv;
    }

    return 0;
}
