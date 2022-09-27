from __future__ import annotations

import asyncio
import dataclasses
import importlib
import importlib.abc
import importlib.machinery
import importlib.util
import logging
import re
import sys
import types
from copy import copy
from dataclasses import dataclass
from typing import ClassVar, Dict, List, Mapping, Optional, Sequence, Type, Union

from typing_extensions import TypeAlias

import temporalio.bridge.proto.workflow_activation
import temporalio.bridge.proto.workflow_completion

from .workflow_instance import (
    UnsandboxedWorkflowRunner,
    WorkflowInstance,
    WorkflowInstanceDetails,
    WorkflowRunner,
)

# Approaches to isolating global state:
#
# * Using exec() with existing modules copied and use importlib.reload
#   * Problem: Reload shares globals
# * Using exec() without existing modules copied and use importlib.__import__
#   * Problem: Importing a module by default executes the module with existing
#     globals instead of isolated ones
# * Using Py_NewInterpreter from
#   https://docs.python.org/3/c-api/init.html#sub-interpreter-support
#   * Not yet tried
#
# Approaches to import/call restrictions:
#
# * Using sys.addaudithook
#   * Problem: No callback for every function
# * Using sys.settrace
#   * Problem: Too expensive
# * Using sys.setprofile
#   * Problem: Only affects calls, not variable access
# * Using custom importer to proxy out bad things - Investigating
#
# Discussion of extension module reloading:
#
# * Extensions can't really be reloaded
#   * See https://peps.python.org/pep-0489/#module-reloading for more context on
#     why
#   * See https://bugs.python.org/issue34309
# * This is often not a problem because modules don't rely on non-module or
#   C-static state much
# * For the Google protobuf extension library this is a problem
#   * During first module create, it stores a reference to its "Message" Python
#     class statically (in literal C static state). Then it checks that each
#     message is of that exact class as opposed to the newly imported one in our
#     sandbox
#   * While we can mark all Google protobuf as passthrough, how many other
#     extensions will suffer from this?
#   * This can't even be fixed with subinterpreters or any other in-process
#     approach. C-static is C-static.
#   * While we could switch the pure Python version, how much slower will that
#     be?
#   * I have opened https://github.com/protocolbuffers/protobuf/issues/10143
#     * This is solved in the 4.x series, but they have other problems w/ C
#       extension state reuse: https://github.com/protocolbuffers/upb/pull/804
#
# Final approach implemented:
#
# * Set a custom importer on __import__ that supports "passthrough" modules for
#   reusing modules from the host system
#   * We have to manually set parent attributes for children that are being
#     passed through
#   * We have to manually reset sys.modules and sys.meta_path after we have
#     imported the sys module
# * Set sys.meta_path to a custom finder
#   * We have to copy-and-replace the loader on the module spec to our own so
#     that we can use our own builtins (namely so we get transitive imports)
# * For import/call restrictions, we TODO(cretz): describe

# String patterns are fixed values.  Regex is from the beginning of the string,
# not anywhere in the string (i.e. using match, see
# https://docs.python.org/3/library/re.html#search-vs-match).
Patterns: TypeAlias = List[Union[str, re.Pattern]]


@dataclass(frozen=True)
class SandboxRestrictions:
    # Modules which pass through because we know they are side-effect free.
    # These modules will not be reloaded and will share with the host. Compares
    # against the fully qualified module name.
    passthrough_modules: Patterns

    # Modules which cannot even be imported. If possible, use
    # invalid_module_members instead so modules that are unused by running code
    # can still be imported for other non-running code. Compares against the
    # fully qualified module name.
    invalid_modules: Patterns

    # Module members which cannot be accessed. This includes variables,
    # functions, class methods (including __init__, etc). Compares the key
    # against the fully qualified module name then the value against the
    # qualified member name not including the module itself.
    invalid_module_members: Dict[str, Patterns]

    default: ClassVar[SandboxRestrictions]

    # These are set at the end of the file
    passthrough_modules_minimum: ClassVar[Patterns]
    passthrough_modules_with_temporal: ClassVar[Patterns]
    passthrough_modules_maximum: ClassVar[Patterns]
    passthrough_modules_default: ClassVar[Patterns]


def self_and_children_pattern(parent: str) -> re.Pattern:
    return re.compile(re.escape(parent) + r"(?:$|\..*)")


SandboxRestrictions.passthrough_modules_minimum = [
    # Required due to https://github.com/protocolbuffers/protobuf/issues/10143
    self_and_children_pattern("google.protobuf"),
    self_and_children_pattern("grpc"),
]

SandboxRestrictions.passthrough_modules_with_temporal = (
    SandboxRestrictions.passthrough_modules_minimum
    # Due to Python checks on ABC class extension, we have to include all
    # modules of classes we might extend
    + [
        self_and_children_pattern("asyncio"),
        self_and_children_pattern("abc"),
        self_and_children_pattern("temporalio"),
    ]
)

# sys.stdlib_module_names is only available on 3.10+, so we hardcode here. A
# test will fail if this list doesn't match the 3.10+ Python version it was
# generated against, spitting out the expected list. This is a string instead
# of a list of strings due to black wanting to format this to one item each
# line in a list.
_stdlib_module_names = (
    "__future__,_abc,_aix_support,_ast,_asyncio,_bisect,_blake2,_bootsubprocess,_bz2,_codecs,"
    "_codecs_cn,_codecs_hk,_codecs_iso2022,_codecs_jp,_codecs_kr,_codecs_tw,_collections,"
    "_collections_abc,_compat_pickle,_compression,_contextvars,_crypt,_csv,_ctypes,_curses,"
    "_curses_panel,_datetime,_dbm,_decimal,_elementtree,_frozen_importlib,_frozen_importlib_external,"
    "_functools,_gdbm,_hashlib,_heapq,_imp,_io,_json,_locale,_lsprof,_lzma,_markupbase,"
    "_md5,_msi,_multibytecodec,_multiprocessing,_opcode,_operator,_osx_support,_overlapped,"
    "_pickle,_posixshmem,_posixsubprocess,_py_abc,_pydecimal,_pyio,_queue,_random,_scproxy,"
    "_sha1,_sha256,_sha3,_sha512,_signal,_sitebuiltins,_socket,_sqlite3,_sre,_ssl,_stat,"
    "_statistics,_string,_strptime,_struct,_symtable,_thread,_threading_local,_tkinter,"
    "_tracemalloc,_uuid,_warnings,_weakref,_weakrefset,_winapi,_zoneinfo,abc,aifc,antigravity,"
    "argparse,array,ast,asynchat,asyncio,asyncore,atexit,audioop,base64,bdb,binascii,binhex,"
    "bisect,builtins,bz2,cProfile,calendar,cgi,cgitb,chunk,cmath,cmd,code,codecs,codeop,"
    "collections,colorsys,compileall,concurrent,configparser,contextlib,contextvars,copy,"
    "copyreg,crypt,csv,ctypes,curses,dataclasses,datetime,dbm,decimal,difflib,dis,distutils,"
    "doctest,email,encodings,ensurepip,enum,errno,faulthandler,fcntl,filecmp,fileinput,"
    "fnmatch,fractions,ftplib,functools,gc,genericpath,getopt,getpass,gettext,glob,graphlib,"
    "grp,gzip,hashlib,heapq,hmac,html,http,idlelib,imaplib,imghdr,imp,importlib,inspect,"
    "io,ipaddress,itertools,json,keyword,lib2to3,linecache,locale,logging,lzma,mailbox,"
    "mailcap,marshal,math,mimetypes,mmap,modulefinder,msilib,msvcrt,multiprocessing,netrc,"
    "nis,nntplib,nt,ntpath,nturl2path,numbers,opcode,operator,optparse,os,ossaudiodev,"
    "pathlib,pdb,pickle,pickletools,pipes,pkgutil,platform,plistlib,poplib,posix,posixpath,"
    "pprint,profile,pstats,pty,pwd,py_compile,pyclbr,pydoc,pydoc_data,pyexpat,queue,quopri,"
    "random,re,readline,reprlib,resource,rlcompleter,runpy,sched,secrets,select,selectors,"
    "shelve,shlex,shutil,signal,site,smtpd,smtplib,sndhdr,socket,socketserver,spwd,sqlite3,"
    "sre_compile,sre_constants,sre_parse,ssl,stat,statistics,string,stringprep,struct,"
    "subprocess,sunau,symtable,sys,sysconfig,syslog,tabnanny,tarfile,telnetlib,tempfile,"
    "termios,textwrap,this,threading,time,timeit,tkinter,token,tokenize,trace,traceback,"
    "tracemalloc,tty,turtle,turtledemo,types,typing,unicodedata,unittest,urllib,uu,uuid,"
    "venv,warnings,wave,weakref,webbrowser,winreg,winsound,wsgiref,xdrlib,xml,xmlrpc,zipapp,"
    "zipfile,zipimport,zlib,zoneinfo"
)

SandboxRestrictions.passthrough_modules_maximum = (
    SandboxRestrictions.passthrough_modules_with_temporal
    # All stdlib modules except "sys" and their children. Children are not
    # listed in stdlib names but we need them because in some cases (e.g. os
    # manually setting sys.modules["os.path"]) they have certain child
    # expectations.
    + [
        self_and_children_pattern(m)
        for m in _stdlib_module_names.split(",")
        if m != "sys"
    ]
)

SandboxRestrictions.passthrough_modules_default = (
    SandboxRestrictions.passthrough_modules_maximum
)

SandboxRestrictions.default = SandboxRestrictions(
    passthrough_modules=SandboxRestrictions.passthrough_modules_default,
    invalid_modules=[],
    invalid_module_members={},
)

logger = logging.getLogger(__name__)

# Set to true to log lots of sandbox details
LOG_TRACE = False


def _trace(message: object, *args: object) -> None:
    if LOG_TRACE:
        logger.debug(message, *args)


class SandboxedWorkflowRunner(WorkflowRunner):
    def __init__(
        self,
        # TODO(cretz): Document that this is re-imported and instantiated for
        # _each_ workflow run.
        runner_class: Type[WorkflowRunner] = UnsandboxedWorkflowRunner,
        restrictions: SandboxRestrictions = SandboxRestrictions.default,
    ) -> None:
        super().__init__()
        self._runner_class = runner_class
        self._restrictions = restrictions
        self._cached_module_passthrough: Dict[str, bool] = {}

    async def create_instance(self, det: WorkflowInstanceDetails) -> WorkflowInstance:
        return await _WorkflowInstanceImpl.create(self, det)

    def _check_module_passthrough(self, name: str) -> bool:
        res = self._cached_module_passthrough.get(name)
        if res is None:
            res = self._check_patterns(name, self._restrictions.passthrough_modules)
            self._cached_module_passthrough[name] = res
        return res

    def _check_patterns(self, str: str, patterns: Patterns) -> bool:
        for p in patterns:
            if isinstance(p, re.Pattern):
                if p.match(str):
                    return True
            elif p == str:
                return True
        return False


class _WorkflowInstanceImpl(WorkflowInstance):
    @staticmethod
    async def create(
        runner: SandboxedWorkflowRunner, det: WorkflowInstanceDetails
    ) -> _WorkflowInstanceImpl:
        _trace("Creating sandboxed instance for %s", det.defn.cls)
        instance = _WorkflowInstanceImpl(runner, det)
        # We're gonna add a task-holder to the globals that we will populate
        # with the task
        instance._globals_and_locals["__temporal_loop"] = asyncio.get_running_loop()
        instance._run_code(
            f"from {det.defn.cls.__module__} import {det.defn.cls.__name__} as __temporal_workflow_class\n"
            f"from {runner._runner_class.__module__} import {runner._runner_class.__name__} as __temporal_runner_class\n"
            "import asyncio\n"
            # We are importing the top-level temporalio.bridge.worker here
            # because there are technically things we need from the bridge that
            # are not in scope (e.g. the PollShutdownError) but we want them
            # loaded
            "import temporalio.bridge.worker\n"
            "__temporal_task = asyncio.create_task(__temporal_instance._sandboxed_create_instance(__temporal_runner_class, __temporal_workflow_class))\n"
        )
        # Wait on creation to complete
        del instance._globals_and_locals["__temporal_loop"]
        await instance._globals_and_locals.pop("__temporal_task")  # type: ignore
        return instance

    def __init__(
        self, runner: SandboxedWorkflowRunner, det: WorkflowInstanceDetails
    ) -> None:
        super().__init__()
        self._runner = runner
        self._det = det
        self._sandboxed_instance: Optional[WorkflowInstance] = None
        # Retain the original import for use inside the sandbox
        self._real_import = __import__
        # Builtins not properly typed in typeshed
        assert isinstance(__builtins__, dict)
        # Replace import in builtins
        new_builtins = __builtins__.copy()
        new_builtins["__import__"] = self._sandboxed_import
        self._globals_and_locals = {
            "__builtins__": new_builtins,
            "__temporal_instance": self,
            # TODO(cretz): Any harm in just hardcoding this?
            "__file__": "workflow_sandbox.py",
        }

        # Make a new set of sys.modules cache and new meta path
        self._new_modules: Dict[str, types.ModuleType] = {}
        self._new_meta_path: List = [_SandboxedImporter(self, sys.meta_path)]  # type: ignore

    def _run_code(self, code: str) -> None:
        _trace("Running sandboxed code:\n%s", code)
        # TODO(cretz): Is it ok/necessary to remove from sys.modules temporarily here?
        # TODO(cretz): Try to copy all of sys instead
        self._old_modules = sys.modules
        old_meta_path = sys.meta_path
        sys.modules = self._new_modules
        sys.meta_path = self._new_meta_path
        try:
            exec(code, self._globals_and_locals, self._globals_and_locals)
        finally:
            sys.modules = self._old_modules
            sys.meta_path = old_meta_path

    async def _sandboxed_create_instance(
        self, runner_class, workflow_class: Type
    ) -> None:
        runner: WorkflowRunner = runner_class()
        # In Python, functions capture their globals at definition time.
        # Therefore, we must make a new workflow definition replacing all
        # existing functions (run, signals, queries) with the ones on this new
        # class. We also replace the class itself.
        # TODO(cretz): Should we rework the definition to only store fn names
        # instead of callables?
        old_defn = self._det.defn
        new_defn = dataclasses.replace(
            old_defn,
            cls=workflow_class,
            run_fn=getattr(workflow_class, old_defn.run_fn.__name__),
            signals={
                k: dataclasses.replace(v, fn=getattr(workflow_class, v.fn.__name__))
                for k, v in old_defn.signals.items()
            },
            queries={
                k: dataclasses.replace(v, fn=getattr(workflow_class, v.fn.__name__))
                for k, v in old_defn.queries.items()
            },
        )
        new_det = dataclasses.replace(self._det, defn=new_defn)
        self._sandboxed_instance = await runner.create_instance(new_det)

    def activate(
        self, act: temporalio.bridge.proto.workflow_activation.WorkflowActivation
    ) -> temporalio.bridge.proto.workflow_completion.WorkflowActivationCompletion:
        self._globals_and_locals["__temporal_activation"] = act
        self._run_code(
            "__temporal_completion = __temporal_instance._sandboxed_instance.activate(__temporal_activation)"
        )
        del self._globals_and_locals["__temporal_activation"]
        return self._globals_and_locals.pop("__temporal_completion")  # type: ignore

    def _sandboxed_import(
        self,
        name: str,
        globals: Optional[Mapping[str, object]] = None,
        locals: Optional[Mapping[str, object]] = None,
        fromlist: Sequence[str] = (),
        level: int = 0,
    ) -> types.ModuleType:
        new_sys = False
        if name not in sys.modules:
            new_sys = name == "sys"
            # If it's a passthrough module, just put it in sys.modules from old
            # sys.modules
            if (
                self._runner._check_module_passthrough(name)
                and name in self._old_modules
            ):
                # Internally, Python loads the parents before the children, but
                # we are skipping that when we set the module explicitly here.
                # So we must manually load the parent if there is one.
                parent, _, child = name.rpartition(".")
                if parent and parent not in sys.modules:
                    _trace(
                        "Importing parent module %s before passing through %s",
                        parent,
                        name,
                    )
                    self._sandboxed_import(parent, globals, locals)

                # Just set the module
                _trace("Passing module %s through from host", name)
                sys.modules[name] = self._old_modules[name]

                # Put it on the parent
                if parent:
                    setattr(sys.modules[parent], child, sys.modules[name])
        mod = importlib.__import__(name, globals, locals, fromlist, level)
        if new_sys:
            _trace("Replacing modules and meta path in sys")
            # We have to change the modules and meta path back to the known
            # ones
            self._new_modules["sys"] = mod
            setattr(mod, "modules", self._new_modules)
            setattr(mod, "meta_path", self._new_meta_path)
        return mod


class _SandboxedImporter(importlib.abc.MetaPathFinder):
    def __init__(
        self,
        instance: _WorkflowInstanceImpl,
        old_meta_path: List[importlib.abc.MetaPathFinder],
    ) -> None:
        super().__init__()
        self._instance = instance
        self._old_meta_path = old_meta_path

    def find_spec(
        self,
        fullname: str,
        path: Optional[Sequence[Union[bytes, str]]],
        target: Optional[types.ModuleType] = None,
    ) -> Optional[importlib.machinery.ModuleSpec]:
        _trace("Finding spec for module %s", fullname)
        for finder in self._old_meta_path:
            spec = finder.find_spec(fullname, path, target)
            if not spec:
                continue
            _trace("  Found spec: %s in finder %s", spec, finder)
            # Python's default exec_module does not use our builtins (which has
            # our custom importer), so we must inject out builtins.
            #
            # We do this by shallow-copying the spec and its loader, then
            # replacing the loader's exec_module with our own that delegates to
            # the original after we have set the builtins.
            #
            # We choose shallow copy for the spec and loader instead of a
            # wrapper class because the internal Python code has a lot of hidden
            # loaders and expectations of other attributes. Also both instances
            # often have so few attributes that a shallow copy is cheap.
            if spec.loader:
                spec = copy(spec)
                spec.loader = copy(spec.loader)
                # MyPy needs help
                assert spec.loader
                orig_exec_module = spec.loader.exec_module

                def custom_exec_module(module: types.ModuleType) -> None:
                    # MyPy needs help
                    assert spec
                    _trace(
                        "Manually executing code for module %s at %s from loader %s",
                        fullname,
                        getattr(module, "__path__", "<unknown>"),
                        spec.loader,
                    )
                    if isinstance(spec.loader, importlib.machinery.ExtensionFileLoader):
                        _trace("  Extension module: %s", module.__dict__)
                    # Put our builtins on the module dict before executing
                    module.__dict__[
                        "__builtins__"
                    ] = self._instance._globals_and_locals["__builtins__"]
                    orig_exec_module(module)

                spec.loader.exec_module = custom_exec_module  # type: ignore
            return spec
        return None
