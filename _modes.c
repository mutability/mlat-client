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

/* would be nice to move this into the submodule init, but that needs python 3.5 for PyModule_AddFunctions */ 
static PyMethodDef methods[] = {
    { "crc", modescrc_crc, METH_VARARGS, "Calculate the Mode S CRC over a buffer. Don't include the message's trailing CRC bytes in the provided buffer." },
    { "EventMessage", (PyCFunction)modesmessage_eventmessage, METH_VARARGS|METH_KEYWORDS,  "Constructs a new event message with a given type, timestamp, and event data." },
    { NULL, NULL, 0, NULL }
};

static void free_modes(PyObject *);

PyDoc_STRVAR(docstr, "C helpers to speed up ModeS message processing");
static PyModuleDef module = {
    PyModuleDef_HEAD_INIT,
    "_modes",            /* m_name */
    docstr,              /* m_doc */
    -1,                  /* m_size */
    methods,             /* m_methods */
    NULL,                /* m_slots / m_reload */
    NULL,                /* m_traverse */
    NULL,                /* m_clear */
    (freefunc)free_modes /* m_free */
};
    
PyMODINIT_FUNC
PyInit__modes(void)
{
    PyObject *m = NULL;

    m = PyModule_Create(&module);
    if (m == NULL)
        return NULL;
    
    if (modescrc_module_init(m) < 0) {
        goto error;
    }

    if (modesmessage_module_init(m) < 0) {
        goto error;
    }

    if (modesreader_module_init(m) < 0) {
        goto error;
    }

    return m;

 error:
    Py_DECREF(m);
    return NULL;
}

void free_modes(PyObject *m)
{
    modesreader_module_free(m);
    modesmessage_module_free(m);
    modescrc_module_free(m);
}
