/* A standalone extension for testing guest imports and WASI restrictions. */
#define Py_LIMITED_API 0x030b0000
#include <Python.h>
#include <time.h>

static PyObject *answer(PyObject *self, PyObject *unused) {
    return PyLong_FromLong(42);
}

static PyObject *wall_clock(PyObject *self, PyObject *unused) {
    struct timespec now;
    if (clock_gettime(CLOCK_REALTIME, &now) != 0) {
        return PyErr_SetFromErrno(PyExc_OSError);
    }
    return PyLong_FromLong(now.tv_sec);
}

static PyMethodDef methods[] = {
    {"answer", answer, METH_NOARGS, NULL},
    {"wall_clock", wall_clock, METH_NOARGS, NULL},
    {NULL, NULL, 0, NULL},
};

static struct PyModuleDef module = {
    PyModuleDef_HEAD_INIT, "probe", NULL, -1, methods,
};

PyMODINIT_FUNC PyInit_probe(void) {
    return PyModule_Create(&module);
}
