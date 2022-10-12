from __future__ import annotations

import asyncio
import dataclasses
import functools
import importlib
import importlib.abc
import importlib.machinery
import importlib.util
import logging
import math
import operator
import sys
import types
from copy import copy, deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import (
    Any,
    Callable,
    ClassVar,
    Dict,
    List,
    Literal,
    Mapping,
    NoReturn,
    Optional,
    Sequence,
    Set,
    Type,
    TypeVar,
    Union,
    cast,
)

import temporalio.bridge.proto.workflow_activation
import temporalio.bridge.proto.workflow_completion
import temporalio.converter
import temporalio.workflow

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
#   * Existing solutions/ideas:
#     * https://www.attrs.org/en/stable/how-does-it-work.html#immutability
#     * https://github.com/diegojromerolopez/gelidum
#     * https://docs.python.org/3/library/unittest.mock.html#unittest.mock.patch
#     * https://wrapt.readthedocs.io
#     * https://github.com/zopefoundation/RestrictedPython
#     * Immutable proxies
#       * https://codereview.stackexchange.com/questions/27468/creating-proxy-classes-for-immutable-objects
#       * https://stackoverflow.com/a/29986462
#       * https://ruddra.com/python-proxy-object/
#     * https://github.com/pallets/werkzeug/blob/main/src/werkzeug/local.py
#       * This one is good w/ how it proxies

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


@dataclass(frozen=True)
class SandboxRestrictions:
    # Modules which pass through because we know they are side-effect free.
    # These modules will not be reloaded and will share with the host. Compares
    # against the fully qualified module name.
    passthrough_modules: SandboxPattern

    # Modules which cannot even be imported. If possible, use
    # invalid_module_members instead so modules that are unused by running code
    # can still be imported for other non-running code. Compares against the
    # fully qualified module name.
    invalid_modules: SandboxPattern

    # Module members which cannot be accessed. This includes variables,
    # functions, class methods (including __init__, etc). Compares the key
    # against the fully qualified module name then the value against the
    # qualified member name not including the module itself.
    invalid_module_members: SandboxPattern

    passthrough_modules_minimum: ClassVar[SandboxPattern]
    passthrough_modules_with_temporal: ClassVar[SandboxPattern]
    passthrough_modules_maximum: ClassVar[SandboxPattern]
    passthrough_modules_default: ClassVar[SandboxPattern]

    invalid_module_members_default: ClassVar[SandboxPattern]

    default: ClassVar[SandboxRestrictions]


@dataclass(frozen=True)
class SandboxPattern:
    match_self: bool = False
    children: Optional[Mapping[str, SandboxPattern]] = None

    all: ClassVar[SandboxPattern]
    none: ClassVar[SandboxPattern]

    @staticmethod
    def from_value(
        v: Union[SandboxPattern, str, Dict[str, Any], List, Literal[True]]
    ) -> SandboxPattern:
        if v is True:
            return SandboxPattern.all
        if isinstance(v, SandboxPattern):
            return v
        if isinstance(v, str):
            # Dotted string
            pieces = v.split(".", maxsplit=1)
            # Allow escaping dots
            while pieces[0].endswith("\\") and len(pieces) > 1:
                # TODO(cretz): Could stop being lazy and do full unescape here
                if pieces[0].endswith("\\\\"):
                    raise ValueError("Only simple dot escapes supported")
                sub_pieces = pieces[1].split(".", maxsplit=1)
                pieces[0] = pieces[0][:-1] + "." + sub_pieces[0]
                pieces[1] = sub_pieces[1]
            return SandboxPattern(
                children={
                    pieces[0]: SandboxPattern.all
                    if len(pieces) == 1
                    else SandboxPattern.from_value(pieces[1])
                }
            )
        if isinstance(v, dict):
            return SandboxPattern(
                children={k: SandboxPattern.from_value(v) for k, v in v.items()}
            )
        if isinstance(v, list):
            ret = SandboxPattern.none
            for child in v:
                ret = ret | SandboxPattern.from_value(child)
            return ret

    def __or__(self, other: SandboxPattern) -> SandboxPattern:
        if self.match_self or other.match_self:
            return SandboxPattern.all
        if not self.children and not other.children:
            return SandboxPattern.none
        new_children = dict(self.children) if self.children else {}
        if other.children:
            for other_k, other_v in other.children.items():
                if other_k in new_children:
                    new_children[other_k] = new_children[other_k] | other_v
                else:
                    new_children[other_k] = other_v
        return SandboxPattern(children=new_children)

    def matches_dotted_str(self, s: str) -> bool:
        return self.matches_path(s.split("."))

    def matches_path(self, pieces: List[str]) -> bool:
        if self.match_self or not self.children or len(pieces) == 0:
            return self.match_self
        child = self.children.get(pieces[0])
        return child is not None and child.matches_path(pieces[1:])

    def child_from_dotted_str(self, s: str) -> Optional[SandboxPattern]:
        return self.child_from_path(s.split("."))

    def child_from_path(self, v: List[str]) -> Optional[SandboxPattern]:
        if self.match_self:
            return SandboxPattern.all
        if len(v) == 0 or not self.children:
            return None
        child = self.children.get(v[0])
        if not child or len(v) == 1:
            return child
        return child.child_from_path(v[1:])


SandboxPattern.none = SandboxPattern()
SandboxPattern.all = SandboxPattern(match_self=True)


SandboxRestrictions.passthrough_modules_minimum = SandboxPattern.from_value(
    [
        # Required due to https://github.com/protocolbuffers/protobuf/issues/10143
        "google.protobuf",
        "grpc",
        # Due to some side-effecting calls made on import, these need to be
        # allowed
        "pathlib",
    ]
)

SandboxRestrictions.passthrough_modules_with_temporal = SandboxRestrictions.passthrough_modules_minimum | SandboxPattern.from_value(
    [
        # Due to Python checks on ABC class extension, we have to include all
        # modules of classes we might extend
        "asyncio",
        "abc",
        "temporalio",
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

SandboxRestrictions.passthrough_modules_maximum = SandboxRestrictions.passthrough_modules_with_temporal | SandboxPattern.from_value(
    [
        # All stdlib modules except "sys" and their children. Children are not
        # listed in stdlib names but we need them because in some cases (e.g. os
        # manually setting sys.modules["os.path"]) they have certain child
        # expectations.
        v
        for v in _stdlib_module_names.split(",")
        if v != "sys"
    ]
)

SandboxRestrictions.passthrough_modules_default = (
    SandboxRestrictions.passthrough_modules_maximum
)

SandboxRestrictions.invalid_module_members_default = SandboxPattern.from_value(
    {
        "__builtins__": [
            "breakpoint",
            "input",
            "open",
        ],
        "datetime": {
            "date": ["today"],
            "datetime": ["now", "today", "utcnow"],
        },
        "os": [
            "getcwd",
            # Cannot easily restrict os.name, it's even used in Python's pathlib
            # and shutil during import time
            # "name",
            # TODO(cretz): The rest
        ],
        "random": [
            "betavariate",
            "choice",
            "choices",
            "expovariate",
            "gammavariate",
            "gauss",
            "getrandbits" "getstate",
            "lognormvariate",
            "normalvariate",
            "paretovariate",
            "randbytes",
            "randint",
            "random",
            "randrange",
            "sample",
            "seed",
            "setstate",
            "shuffle",
            "triangular",
            "uniform",
            "vonmisesvariate",
            "weibullvariate",
            # TODO(cretz): Disallow Random() and SecureRandom() without seeds
        ],
        "readline": SandboxPattern.all,
        "zoneinfo": {
            "ZoneInfo": ["clear_cache", "from_file", "reset_tzpath"],
        }
        # TODO(cretz): The rest
    }
)

SandboxRestrictions.default = SandboxRestrictions(
    passthrough_modules=SandboxRestrictions.passthrough_modules_default,
    invalid_modules=SandboxPattern.none,
    invalid_module_members=SandboxRestrictions.invalid_module_members_default,
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

    def prepare_workflow(self, defn: temporalio.workflow._Definition) -> None:
        _WorkflowInstanceImpl.validate(self, defn)

    async def create_instance(self, det: WorkflowInstanceDetails) -> WorkflowInstance:
        return await _WorkflowInstanceImpl.create(self, det)


_validation_workflow_info = temporalio.workflow.Info(
    attempt=-1,
    continued_run_id=None,
    cron_schedule=None,
    execution_timeout=None,
    headers={},
    namespace="sandbox-validate-namespace",
    parent=None,
    raw_memo={},
    retry_policy=None,
    run_id="sandbox-validate-run_id",
    run_timeout=None,
    search_attributes={},
    start_time=datetime.fromtimestamp(0, timezone.utc),
    task_queue="sandbox-validate-task_queue",
    task_timeout=timedelta(),
    workflow_id="sandbox-validate-workflow_id",
    workflow_type="sandbox-validate-workflow_type",
)


class _WorkflowInstanceImpl(WorkflowInstance):
    @staticmethod
    def validate(
        runner: SandboxedWorkflowRunner, defn: temporalio.workflow._Definition
    ) -> None:
        det = WorkflowInstanceDetails(
            payload_converter_class=temporalio.converter.default().payload_converter_class,
            interceptor_classes=[],
            defn=defn,
            # Just use fake info during validation
            info=_validation_workflow_info,
            randomness_seed=-1,
            extern_functions={},
        )
        # Just run to confirm it doesn't error
        _WorkflowInstanceImpl.init_and_run(runner, det, "")

    @staticmethod
    async def create(
        runner: SandboxedWorkflowRunner, det: WorkflowInstanceDetails
    ) -> _WorkflowInstanceImpl:
        instance = _WorkflowInstanceImpl.init_and_run(
            runner,
            det,
            "__temporal_task = asyncio.create_task(__temporal_instance._sandboxed_create_instance(__temporal_runner_class, __temporal_workflow_class))\n",
        )
        # Wait on creation to complete
        await instance._globals_and_locals.pop("__temporal_task")  # type: ignore
        return instance

    @staticmethod
    def init_and_run(
        runner: SandboxedWorkflowRunner, det: WorkflowInstanceDetails, code: str
    ) -> _WorkflowInstanceImpl:
        _trace("Creating sandboxed instance for %s", det.defn.cls)
        instance = _WorkflowInstanceImpl(runner, det)
        # We're gonna add a task-holder to the globals that we will populate
        # with the task
        instance._globals_and_locals["__temporal_loop"] = asyncio.get_running_loop()
        import_code = (
            f"from {det.defn.cls.__module__} import {det.defn.cls.__name__} as __temporal_workflow_class\n"
            f"from {runner._runner_class.__module__} import {runner._runner_class.__name__} as __temporal_runner_class\n"
            "import asyncio\n"
            # We are importing the top-level temporalio.bridge.worker here
            # because there are technically things we need from the bridge that
            # are not in scope (e.g. the PollShutdownError) but we want them
            # loaded
            "import temporalio.bridge.worker\n"
        )
        instance._run_code(import_code + code)
        del instance._globals_and_locals["__temporal_loop"]
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
        # Restrict some builtins and replace import in builtins
        # TODO(cretz): Restrict
        new_builtins = __builtins__.copy()
        # If there's a builtins restriction, we need to use it
        builtins_pattern = (
            self._runner._restrictions.invalid_module_members.child_from_path(
                ["__builtins__"]
            )
        )
        if builtins_pattern:
            # Python doesn't allow us to wrap the builtins dictionary with our
            # own __getitem__ type (low-level C assertion is performed to
            # confirm it is a dict), so we instead choose to walk the builtins
            # children and specifically set them as not callable
            def restrict_built_in(name: str, *args, **kwargs) -> NoReturn:
                raise RestrictedWorkflowAccessError(f"__builtins__.{name}")

            for k in new_builtins.keys():
                if builtins_pattern.matches_path([k]):
                    new_builtins[k] = functools.partial(restrict_built_in, k)
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
        # # We need to know which modules we've checked for restrictions so we
        # # save the effort of re-checking on successive imports
        self._restriction_checked_modules: Set[str] = set()

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
        # Passthrough modules
        if name not in sys.modules:
            new_sys = name == "sys"
            # Make sure the entire module isn't set as invalid
            if self._runner._restrictions.invalid_modules.matches_dotted_str(name):
                raise RestrictedWorkflowAccessError(name)
            # If it's a passthrough module, just put it in sys.modules from old
            # sys.modules
            if (
                self._runner._restrictions.passthrough_modules.matches_dotted_str(name)
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
        # If this is not already checked and there is a pattern, restrict
        if name not in self._restriction_checked_modules:
            self._restriction_checked_modules.add(name)
            pattern = (
                self._runner._restrictions.invalid_module_members.child_from_dotted_str(
                    name
                )
            )
            if pattern:
                _trace("Restricting module %s during import", name)
                mod = _RestrictedModule(mod, pattern)
                sys.modules[name] = mod

        if new_sys:
            # We have to change the modules and meta path back to the known
            # ones
            _trace("Replacing modules and meta path in sys")
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
            # There are two things the default Python loaders do that we must
            # override here.
            #
            # First, when resolving "from lists"  where the child is a module
            # (e.g. "from foo import bar" where foo.bar is a module), Python
            # internally uses an importer we can't override. Therefore we must
            # perform our restrictions on create_module of the loader.
            #
            # Second, Python's default exec_module does not use our builtins
            # which has our custom importer, so we must inject our builtins at
            # module execution time.
            #
            # We override these two loader methods by shallow-copying the spec
            # and its loader, then replacing the loader's create_module and
            # exec_module with our own that delegates to the original.
            #
            # We choose shallow copy for the spec and loader instead of a
            # wrapper class because the internal Python code has a lot of hidden
            # loaders and expectations of other attributes. Also both instances
            # often have so few attributes that a shallow copy is cheap.
            if spec.loader:
                spec = copy(spec)
                spec.loader = copy(spec.loader)
                # There's always a loader in our experience but Typeshed is
                # saying it's optional
                assert spec.loader
                orig_create_module = spec.loader.create_module
                orig_exec_module = spec.loader.exec_module
                spec.loader.load_module

                def custom_create_module(
                    spec: importlib.machinery.ModuleSpec,
                ) -> Optional[types.ModuleType]:
                    _trace("Applying custom create for module %s", spec.name)
                    mod = orig_create_module(spec)

                    # If this is not already checked and there is a pattern,
                    # restrict
                    if spec.name not in self._instance._restriction_checked_modules:
                        self._instance._restriction_checked_modules.add(spec.name)
                        pattern = self._instance._runner._restrictions.invalid_module_members.child_from_dotted_str(
                            spec.name
                        )
                        if pattern:
                            _trace("Restricting module %s during create", spec.name)
                            # mod might be None which means "defer to default"
                            # loading in Python, so we manually instantiate if
                            # that's the case
                            if not mod:
                                mod = types.ModuleType(spec.name)
                            mod = _RestrictedModule(mod, pattern)
                    return mod

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

                # Replace the methods
                spec.loader.create_module = custom_create_module  # type: ignore
                spec.loader.exec_module = custom_exec_module  # type: ignore
            return spec
        return None


@dataclass
class _RestrictionState:
    name: str
    obj: object
    pattern: SandboxPattern

    def assert_child_not_restricted(self, name: str) -> None:
        if self.pattern.matches_path([name]):
            logger.warning("%s on %s restricted", name, self.name)
            raise RestrictedWorkflowAccessError(f"{self.name}.{name}")


class _RestrictedProxyLookup:
    def __init__(
        self,
        access_func: Optional[Callable] = None,
        *,
        fallback_func: Optional[Callable] = None,
        class_value: Optional[Any] = None,
        is_attr: bool = False,
    ) -> None:
        bind_func: Optional[Callable[[_RestrictedProxy, Any], Callable]]
        if hasattr(access_func, "__get__"):
            # A Python function, can be turned into a bound method.

            def bind_func(instance: _RestrictedProxy, obj: Any) -> Callable:
                return access_func.__get__(obj, type(obj))  # type: ignore

        elif access_func is not None:
            # A C function, use partial to bind the first argument.

            def bind_func(instance: _RestrictedProxy, obj: Any) -> Callable:
                return functools.partial(access_func, obj)  # type: ignore

        else:
            # Use getattr, which will produce a bound method.
            bind_func = None

        self.bind_func = bind_func
        self.fallback_func = fallback_func
        self.class_value = class_value
        self.is_attr = is_attr

    def __set_name__(self, owner: _RestrictedProxy, name: str) -> None:
        self.name = name

    def __get__(self, instance: _RestrictedProxy, owner: Optional[Type] = None) -> Any:
        if instance is None:
            if self.class_value is not None:
                return self.class_value

            return self

        try:
            state: _RestrictionState = object.__getattribute__(
                instance, "__temporal_state"
            )
        except RuntimeError:
            if self.fallback_func is None:
                raise

            fallback = self.fallback_func.__get__(instance, owner)

            if self.is_attr:
                # __class__ and __doc__ are attributes, not methods.
                # Call the fallback to get the value.
                return fallback()

            return fallback

        if self.bind_func is not None:
            return self.bind_func(instance, state.obj)

        return getattr(state.obj, self.name)

    def __repr__(self) -> str:
        return f"proxy {self.name}"

    def __call__(self, instance: _RestrictedProxy, *args: Any, **kwargs: Any) -> Any:
        """Support calling unbound methods from the class. For example,
        this happens with ``copy.copy``, which does
        ``type(x).__copy__(x)``. ``type(x)`` can't be proxied, so it
        returns the proxy type and descriptor.
        """
        return self.__get__(instance, type(instance))(*args, **kwargs)


class _RestrictedProxyIOp(_RestrictedProxyLookup):
    __slots__ = ()

    def __init__(
        self,
        access_func: Optional[Callable] = None,
        *,
        fallback_func: Optional[Callable] = None,
    ) -> None:
        super().__init__(access_func, fallback_func=fallback_func)

        def bind_f(instance: _RestrictedProxy, obj: Any) -> Callable:
            def i_op(self: Any, other: Any) -> _RestrictedProxy:
                f(self, other)  # type: ignore
                return instance

            return i_op.__get__(obj, type(obj))  # type: ignore

        self.bind_f = bind_f


_OpF = TypeVar("_OpF", bound=Callable[..., Any])


def _l_to_r_op(op: _OpF) -> _OpF:
    """Swap the argument order to turn an l-op into an r-op."""

    def r_op(obj: Any, other: Any) -> Any:
        return op(other, obj)

    return cast(_OpF, r_op)


def _is_restrictable(v: Any) -> bool:
    return v is not None and not isinstance(
        v, (bool, int, float, complex, str, bytes, bytearray)
    )


class _RestrictedProxy:

    _get_state: Callable[[], _RestrictionState]

    def __init__(self, name: str, obj: Any, pattern: SandboxPattern) -> None:
        _trace("__init__ on %s", name)
        object.__setattr__(
            self,
            "__temporal_state",
            _RestrictionState(name=name, obj=obj, pattern=pattern),
        )

    def __getattribute__(self, __name: str) -> Any:
        # To prevent recursion, must use __getattribute__ on object to get the
        # restriction state
        state: _RestrictionState = object.__getattribute__(self, "__temporal_state")
        _trace("__getattribute__ %s on %s", __name, state.name)
        state.assert_child_not_restricted(__name)
        # return types.ModuleType.__getattribute__(self, __name)
        ret = object.__getattribute__(self, "__getattr__")(__name)
        # If there is a child pattern, restrict if we can
        child_pattern = state.pattern.child_from_dotted_str(__name)
        if child_pattern and _is_restrictable(ret):
            ret = _RestrictedProxy(f"{state.name}.{__name}", ret, child_pattern)
        return ret

    def __setattr__(self, __name: str, __value: Any) -> None:
        state: _RestrictionState = object.__getattribute__(self, "__temporal_state")
        _trace("__setattr__ %s on %s", __name, state.name)
        state.assert_child_not_restricted(__name)
        setattr(state.obj, __name, __value)

    def __call__(self, *args, **kwargs) -> _RestrictedProxy:
        state: _RestrictionState = object.__getattribute__(self, "__temporal_state")
        _trace("__call__ on %s", state.name)
        ret = state.obj(*args, **kwargs)  # type: ignore
        # Always wrap the result of a call to self with the same restrictions
        # (this is often instantiating a class)
        if _is_restrictable(ret):
            ret = _RestrictedProxy(state.name, ret, state.pattern)
        return ret

    def __getitem__(self, key: Any) -> Any:
        state: _RestrictionState = object.__getattribute__(self, "__temporal_state")
        if isinstance(key, str):
            state.assert_child_not_restricted(key)
        _trace("__getitem__ %s on %s", key, state.name)
        ret = operator.getitem(state.obj, key)  # type: ignore
        # If there is a child pattern, restrict if we can
        if isinstance(key, str) and state.pattern.children:
            child_pattern = state.pattern.children.get(key)
            if child_pattern and _is_restrictable(ret):
                ret = _RestrictedProxy(f"{state.name}.{key}", ret, child_pattern)
        return ret

    __doc__ = _RestrictedProxyLookup(  # type: ignore
        class_value=__doc__, fallback_func=lambda self: type(self).__doc__, is_attr=True
    )
    __wrapped__ = _RestrictedProxyLookup(
        fallback_func=lambda self: self._get_state().obj, is_attr=True
    )
    # __del__ should only delete the proxy
    __repr__ = _RestrictedProxyLookup(  # type: ignore
        repr, fallback_func=lambda self: f"<{type(self).__name__} unbound>"
    )
    __str__ = _RestrictedProxyLookup(str)  # type: ignore
    __bytes__ = _RestrictedProxyLookup(bytes)
    __format__ = _RestrictedProxyLookup()  # type: ignore
    __lt__ = _RestrictedProxyLookup(operator.lt)
    __le__ = _RestrictedProxyLookup(operator.le)
    __eq__ = _RestrictedProxyLookup(operator.eq)  # type: ignore
    __ne__ = _RestrictedProxyLookup(operator.ne)  # type: ignore
    __gt__ = _RestrictedProxyLookup(operator.gt)
    __ge__ = _RestrictedProxyLookup(operator.ge)
    __hash__ = _RestrictedProxyLookup(hash)  # type: ignore
    __bool__ = _RestrictedProxyLookup(bool, fallback_func=lambda self: False)
    __getattr__ = _RestrictedProxyLookup(getattr)
    # __setattr__ = _RestrictedProxyLookup(setattr)  # type: ignore
    __delattr__ = _RestrictedProxyLookup(delattr)  # type: ignore
    __dir__ = _RestrictedProxyLookup(dir, fallback_func=lambda self: [])  # type: ignore
    # __get__ (proxying descriptor not supported)
    # __set__ (descriptor)
    # __delete__ (descriptor)
    # __set_name__ (descriptor)
    # __objclass__ (descriptor)
    # __slots__ used by proxy itself
    # __dict__ (__getattr__)
    # __weakref__ (__getattr__)
    # __init_subclass__ (proxying metaclass not supported)
    # __prepare__ (metaclass)
    __class__ = _RestrictedProxyLookup(
        fallback_func=lambda self: type(self), is_attr=True
    )  # type: ignore
    __instancecheck__ = _RestrictedProxyLookup(
        lambda self, other: isinstance(other, self)
    )
    __subclasscheck__ = _RestrictedProxyLookup(
        lambda self, other: issubclass(other, self)
    )
    # __class_getitem__ triggered through __getitem__
    __len__ = _RestrictedProxyLookup(len)
    __length_hint__ = _RestrictedProxyLookup(operator.length_hint)
    __setitem__ = _RestrictedProxyLookup(operator.setitem)
    __delitem__ = _RestrictedProxyLookup(operator.delitem)
    # __missing__ triggered through __getitem__
    __iter__ = _RestrictedProxyLookup(iter)
    __next__ = _RestrictedProxyLookup(next)
    __reversed__ = _RestrictedProxyLookup(reversed)
    __contains__ = _RestrictedProxyLookup(operator.contains)
    __add__ = _RestrictedProxyLookup(operator.add)
    __sub__ = _RestrictedProxyLookup(operator.sub)
    __mul__ = _RestrictedProxyLookup(operator.mul)
    __matmul__ = _RestrictedProxyLookup(operator.matmul)
    __truediv__ = _RestrictedProxyLookup(operator.truediv)
    __floordiv__ = _RestrictedProxyLookup(operator.floordiv)
    __mod__ = _RestrictedProxyLookup(operator.mod)
    __divmod__ = _RestrictedProxyLookup(divmod)
    __pow__ = _RestrictedProxyLookup(pow)
    __lshift__ = _RestrictedProxyLookup(operator.lshift)
    __rshift__ = _RestrictedProxyLookup(operator.rshift)
    __and__ = _RestrictedProxyLookup(operator.and_)
    __xor__ = _RestrictedProxyLookup(operator.xor)
    __or__ = _RestrictedProxyLookup(operator.or_)
    __radd__ = _RestrictedProxyLookup(_l_to_r_op(operator.add))
    __rsub__ = _RestrictedProxyLookup(_l_to_r_op(operator.sub))
    __rmul__ = _RestrictedProxyLookup(_l_to_r_op(operator.mul))
    __rmatmul__ = _RestrictedProxyLookup(_l_to_r_op(operator.matmul))
    __rtruediv__ = _RestrictedProxyLookup(_l_to_r_op(operator.truediv))
    __rfloordiv__ = _RestrictedProxyLookup(_l_to_r_op(operator.floordiv))
    __rmod__ = _RestrictedProxyLookup(_l_to_r_op(operator.mod))
    __rdivmod__ = _RestrictedProxyLookup(_l_to_r_op(divmod))
    __rpow__ = _RestrictedProxyLookup(_l_to_r_op(pow))
    __rlshift__ = _RestrictedProxyLookup(_l_to_r_op(operator.lshift))
    __rrshift__ = _RestrictedProxyLookup(_l_to_r_op(operator.rshift))
    __rand__ = _RestrictedProxyLookup(_l_to_r_op(operator.and_))
    __rxor__ = _RestrictedProxyLookup(_l_to_r_op(operator.xor))
    __ror__ = _RestrictedProxyLookup(_l_to_r_op(operator.or_))
    __iadd__ = _RestrictedProxyIOp(operator.iadd)
    __isub__ = _RestrictedProxyIOp(operator.isub)
    __imul__ = _RestrictedProxyIOp(operator.imul)
    __imatmul__ = _RestrictedProxyIOp(operator.imatmul)
    __itruediv__ = _RestrictedProxyIOp(operator.itruediv)
    __ifloordiv__ = _RestrictedProxyIOp(operator.ifloordiv)
    __imod__ = _RestrictedProxyIOp(operator.imod)
    __ipow__ = _RestrictedProxyIOp(operator.ipow)
    __ilshift__ = _RestrictedProxyIOp(operator.ilshift)
    __irshift__ = _RestrictedProxyIOp(operator.irshift)
    __iand__ = _RestrictedProxyIOp(operator.iand)
    __ixor__ = _RestrictedProxyIOp(operator.ixor)
    __ior__ = _RestrictedProxyIOp(operator.ior)
    __neg__ = _RestrictedProxyLookup(operator.neg)
    __pos__ = _RestrictedProxyLookup(operator.pos)
    __abs__ = _RestrictedProxyLookup(abs)
    __invert__ = _RestrictedProxyLookup(operator.invert)
    __complex__ = _RestrictedProxyLookup(complex)
    __int__ = _RestrictedProxyLookup(int)
    __float__ = _RestrictedProxyLookup(float)
    __index__ = _RestrictedProxyLookup(operator.index)
    __round__ = _RestrictedProxyLookup(round)
    __trunc__ = _RestrictedProxyLookup(math.trunc)
    __floor__ = _RestrictedProxyLookup(math.floor)
    __ceil__ = _RestrictedProxyLookup(math.ceil)
    __enter__ = _RestrictedProxyLookup()
    __exit__ = _RestrictedProxyLookup()
    __await__ = _RestrictedProxyLookup()
    __aiter__ = _RestrictedProxyLookup()
    __anext__ = _RestrictedProxyLookup()
    __aenter__ = _RestrictedProxyLookup()
    __aexit__ = _RestrictedProxyLookup()
    __copy__ = _RestrictedProxyLookup(copy)
    __deepcopy__ = _RestrictedProxyLookup(deepcopy)


class _RestrictedModule(_RestrictedProxy, types.ModuleType):  # type: ignore
    def __init__(self, mod: types.ModuleType, pattern: SandboxPattern) -> None:
        _RestrictedProxy.__init__(self, mod.__name__, mod, pattern)
        types.ModuleType.__init__(self, mod.__name__, mod.__doc__)


class RestrictedWorkflowAccessError(temporalio.workflow.NondeterminismError):
    def __init__(self, qualified_name: str) -> None:
        super().__init__(f"Cannot access {qualified_name} from inside a workflow.")
        self.qualified_name = qualified_name
