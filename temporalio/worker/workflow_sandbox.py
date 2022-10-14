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
    passthrough_modules: SandboxMatcher

    # Modules which cannot even be imported. If possible, use
    # invalid_module_members instead so modules that are unused by running code
    # can still be imported for other non-running code. Compares against the
    # fully qualified module name.
    invalid_modules: SandboxMatcher

    # Module members which cannot be accessed. This includes variables,
    # functions, class methods (including __init__, etc). Compares the key
    # against the fully qualified module name then the value against the
    # qualified member name not including the module itself.
    invalid_module_members: SandboxMatcher

    passthrough_modules_minimum: ClassVar[SandboxMatcher]
    passthrough_modules_with_temporal: ClassVar[SandboxMatcher]
    passthrough_modules_maximum: ClassVar[SandboxMatcher]
    passthrough_modules_default: ClassVar[SandboxMatcher]

    invalid_module_members_default: ClassVar[SandboxMatcher]

    default: ClassVar[SandboxRestrictions]


# TODO(cretz): Document asterisk can be used
@dataclass(frozen=True)
class SandboxMatcher:
    # TODO(cretz): Document that we intentionally use this form instead of a
    # more flexible/abstract matching tree form for optimization reasons
    access: Set[str] = frozenset()  # type: ignore
    use: Set[str] = frozenset()  # type: ignore
    children: Mapping[str, SandboxMatcher] = types.MappingProxyType({})
    match_self: bool = False

    all: ClassVar[SandboxMatcher]
    none: ClassVar[SandboxMatcher]
    all_uses: ClassVar[SandboxMatcher]

    def match_access(self, *child_path: str) -> bool:
        # We prefer to avoid recursion
        matcher: Optional[SandboxMatcher] = self
        for v in child_path:
            # Considered matched if self matches or access matches Note, "use"
            # does not match because we allow it to be accessed but not used.
            assert matcher  # MyPy help
            if matcher.match_self or v in matcher.access or "*" in matcher.access:
                return True
            matcher = self.children.get(v) or self.children.get("*")
            if not matcher:
                return False
            elif matcher.match_self:
                return True
        return False

    def child_matcher(self, *child_path: str) -> Optional[SandboxMatcher]:
        # We prefer to avoid recursion
        matcher: Optional[SandboxMatcher] = self
        for v in child_path:
            # Use all if it matches self, access, _or_ use. Use doesn't match
            # self but matches all children.
            assert matcher  # MyPy help
            if (
                matcher.match_self
                or v in matcher.access
                or v in matcher.use
                or "*" in matcher.access
                or "*" in matcher.use
            ):
                return SandboxMatcher.all
            matcher = matcher.children.get(v) or matcher.children.get("*")
            if not matcher:
                return None
        return matcher

    def __or__(self, other: SandboxMatcher) -> SandboxMatcher:
        if self.match_self or other.match_self:
            return SandboxMatcher.all
        new_children = dict(self.children) if self.children else {}
        if other.children:
            for other_k, other_v in other.children.items():
                if other_k in new_children:
                    new_children[other_k] = new_children[other_k] | other_v
                else:
                    new_children[other_k] = other_v
        return SandboxMatcher(
            access=self.access | other.access,
            use=self.use | other.use,
            children=new_children,
        )


SandboxMatcher.all = SandboxMatcher(match_self=True)
SandboxMatcher.none = SandboxMatcher()
SandboxMatcher.all_uses = SandboxMatcher(use={"*"})

SandboxRestrictions.passthrough_modules_minimum = SandboxMatcher(
    access={
        "grpc",
        # Due to some side-effecting calls made on import, these need to be
        # allowed
        "pathlib",
    },
    # Required due to https://github.com/protocolbuffers/protobuf/issues/10143
    children={"google": SandboxMatcher(access={"protobuf"})},
)

SandboxRestrictions.passthrough_modules_with_temporal = SandboxRestrictions.passthrough_modules_minimum | SandboxMatcher(
    access={
        # Due to Python checks on ABC class extension, we have to include all
        # modules of classes we might extend
        "asyncio",
        "abc",
        "temporalio",
    }
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

SandboxRestrictions.passthrough_modules_maximum = SandboxRestrictions.passthrough_modules_with_temporal | SandboxMatcher(
    access={
        # All stdlib modules except "sys" and their children. Children are not
        # listed in stdlib names but we need them because in some cases (e.g. os
        # manually setting sys.modules["os.path"]) they have certain child
        # expectations.
        v
        for v in _stdlib_module_names.split(",")
        if v != "sys"
    }
)

SandboxRestrictions.passthrough_modules_default = (
    SandboxRestrictions.passthrough_modules_maximum
)

# TODO(cretz): Should I make this more declarative in an external file?
SandboxRestrictions.invalid_module_members_default = SandboxMatcher(
    children={
        "__builtins__": SandboxMatcher(
            use={
                "breakpoint",
                "input",
                "open",
            }
        ),
        "bz2": SandboxMatcher(use={"open"}),
        # Python's own re lib registers itself. This is mostly ok to not
        # restrict since it's just global picklers that people may want to
        # register globally.
        # "copyreg": SandboxMatcher.all_uses,
        "datetime": SandboxMatcher(
            children={
                "date": SandboxMatcher(use={"today"}),
                "datetime": SandboxMatcher(use={"now", "today", "utcnow"}),
            }
        ),
        "dbm": SandboxMatcher.all_uses,
        "filecmp": SandboxMatcher.all_uses,
        "fileinput": SandboxMatcher.all_uses,
        "glob": SandboxMatcher.all_uses,
        "gzip": SandboxMatcher(use={"open"}),
        "lzma": SandboxMatcher(use={"open"}),
        "marshal": SandboxMatcher(use={"dump", "load"}),
        # We cannot restrict linecache because some packages like attrs' attr
        # use it during dynamic code generation
        # "linecache": SandboxMatcher.all_uses,
        # We cannot restrict os.name because too many things use it during
        # import side effects (including stdlib imports like pathlib and shutil)
        "os": SandboxMatcher(
            use={
                "getcwd",
            }
        ),
        # Not restricting platform-specific calls as that would be too strict. Just
        # things that are specific to what's on disk.
        "os.path": SandboxMatcher(
            use={
                "abspath",
                "exists",
                "lexists",
                "expanduser",
                "expandvars",
                "getatime",
                "getmtime",
                "getctime",
                "getsize",
                "isabs",
                "isfile",
                "isdir",
                "islink",
                "ismount",
                "realpath",
                "relpath",
                "samefile",
                "sameopenfile",
                "samestat",
            }
        ),
        "pathlib": SandboxMatcher(
            children={
                # We allow instantiation and all PurePath calls on Path, so we
                # have to list what we don't like explicitly here
                "Path": SandboxMatcher(
                    use={
                        "chmod",
                        "cwd",
                        "exists",
                        "expanduser",
                        "glob",
                        "group",
                        "hardlink_to",
                        "home",
                        "is_block_device",
                        "is_char_device",
                        "is_dir",
                        "is_fifo",
                        "is_file",
                        "is_mount",
                        "is_socket",
                        "is_symlink",
                        "iterdir",
                        "lchmod",
                        "link_to",
                        "lstat",
                        "mkdir",
                        "open",
                        "owner",
                        "read_bytes",
                        "read_link",
                        "read_text",
                        "rename",
                        "replace",
                        "resolve",
                        "rglob",
                        "rmdir",
                        "samefile",
                        "stat",
                        "symlink_to",
                        "touch",
                        "unlink",
                        "write_bytes",
                        "write_text",
                    }
                )
            }
        ),
        # Everything but instantiating Random and SecureRandom
        # TODO(cretz): Should I support negation in SandboxMatcher to make this
        # list easier?
        # TODO(cretz): Should I have integration tests for this module and
        # others to ensure new things don't get added in newer Python versions
        # without me being aware?
        "random": SandboxMatcher(
            use={
                "betavariate",
                "choice",
                "choices",
                "expovariate",
                "gammavariate",
                "gauss",
                "getrandbits",
                "getstate",
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
            }
        ),
        "readline": SandboxMatcher.all_uses,
        # Only time-safe comparison remains after these restrictions
        "secrets": SandboxMatcher(
            use={
                "choice",
                "randbelow",
                "randbits",
                "SystemRandom",
                "token_bytes",
                "token_hex",
                "token_urlsafe",
            }
        ),
        "shelve": SandboxMatcher.all_uses,
        "shutil": SandboxMatcher.all_uses,
        # There's a good use case for sqlite in memory, so we're not restricting
        # it in any way. Technically we could restrict some of the global
        # settings, but they are often import side effects usable in and out of
        # the sandbox.
        # "sqlite3": SandboxMatcher.all_uses,
        "tarfile": SandboxMatcher(
            use={"open"},
            children={"TarFile": SandboxMatcher(use={"extract", "extractall"})},
        ),
        "tempfile": SandboxMatcher.all_uses,
        "zipfile": SandboxMatcher(
            children={"ZipFile": SandboxMatcher(use={"extract", "extractall"})}
        ),
        "zoneinfo": SandboxMatcher(
            children={
                "ZoneInfo": SandboxMatcher(
                    use={"clear_cache", "from_file", "reset_tzpath"}
                )
            }
        ),
    }
)

SandboxRestrictions.default = SandboxRestrictions(
    passthrough_modules=SandboxRestrictions.passthrough_modules_default,
    invalid_modules=SandboxMatcher.none,
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
        new_builtins = __builtins__.copy()
        # If there's a builtins restriction, we need to use it
        builtins_matcher = (
            self._runner._restrictions.invalid_module_members.child_matcher(
                "__builtins__"
            )
        )
        if builtins_matcher:
            # Python doesn't allow us to wrap the builtins dictionary with our
            # own __getitem__ type (low-level C assertion is performed to
            # confirm it is a dict), so we instead choose to walk the builtins
            # children and specifically set them as not callable
            def restrict_built_in(name: str, *args, **kwargs) -> NoReturn:
                raise RestrictedWorkflowAccessError(f"__builtins__.{name}")

            for k in new_builtins.keys():
                if builtins_matcher.match_access(k):
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
            if self._runner._restrictions.invalid_modules.match_access(
                *name.split(".")
            ):
                raise RestrictedWorkflowAccessError(name)
            # If it's a passthrough module, just put it in sys.modules from old
            # sys.modules
            if (
                self._runner._restrictions.passthrough_modules.match_access(
                    *name.split(".")
                )
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
        # If this is not already checked and there is a matcher, restrict
        if name not in self._restriction_checked_modules:
            self._restriction_checked_modules.add(name)
            matcher = self._runner._restrictions.invalid_module_members.child_matcher(
                *name.split(".")
            )
            if matcher:
                _trace("Restricting module %s during import", name)
                mod = _RestrictedModule(mod, matcher)
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

                    # If this is not already checked and there is a matcher,
                    # restrict
                    if spec.name not in self._instance._restriction_checked_modules:
                        self._instance._restriction_checked_modules.add(spec.name)
                        matcher = self._instance._runner._restrictions.invalid_module_members.child_matcher(
                            *spec.name.split(".")
                        )
                        if matcher:
                            _trace("Restricting module %s during create", spec.name)
                            # mod might be None which means "defer to default"
                            # loading in Python, so we manually instantiate if
                            # that's the case
                            if not mod:
                                mod = types.ModuleType(spec.name)
                            mod = _RestrictedModule(mod, matcher)
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

                    # Put our builtins on the module dict before executing.
                    #
                    # Python uses this __dict__ as the "globals" when calling
                    # exec on the module. The C layer of Python forces these
                    # globals to be an actual dictionary. Therefore if this
                    # module is a restricted module, its __dict__ will also be
                    # proxied which fails load. We can't just overwrite __dict__
                    # for the life of this run because it's a read only
                    # property. So instead we set it to be bypassed.
                    module.__dict__[
                        "__builtins__"
                    ] = self._instance._globals_and_locals["__builtins__"]
                    state: Optional[_RestrictionState] = None
                    if type(module) is _RestrictedModule:
                        state = _RestrictionState.from_proxy(module)
                        state.wrap_dict = False
                    try:
                        orig_exec_module(module)
                    finally:
                        if state:
                            state.wrap_dict = True

                # Replace the methods
                spec.loader.create_module = custom_create_module  # type: ignore
                spec.loader.exec_module = custom_exec_module  # type: ignore
            return spec
        return None


@dataclass
class _RestrictionState:
    @staticmethod
    def from_proxy(v: _RestrictedProxy) -> _RestrictionState:
        # To prevent recursion, must use __getattribute__ on object to get the
        # restriction state
        return object.__getattribute__(v, "__temporal_state")

    name: str
    obj: object
    matcher: SandboxMatcher
    wrap_dict: bool = True

    def assert_child_not_restricted(self, name: str) -> None:
        if self.matcher.match_access(name):
            logger.warning("%s on %s restricted", name, self.name)
            raise RestrictedWorkflowAccessError(f"{self.name}.{name}")

    def set_on_proxy(self, v: _RestrictedProxy) -> None:
        # To prevent recursion, must use __setattr__ on object to set the
        # restriction state
        object.__setattr__(v, "__temporal_state", self)


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
    def __init__(self, name: str, obj: Any, matcher: SandboxMatcher) -> None:
        _trace("__init__ on %s", name)
        _RestrictionState(name=name, obj=obj, matcher=matcher).set_on_proxy(self)

    def __getattribute__(self, __name: str) -> Any:
        state = _RestrictionState.from_proxy(self)
        _trace("__getattribute__ %s on %s", __name, state.name)
        state.assert_child_not_restricted(__name)
        ret = object.__getattribute__(self, "__getattr__")(__name)

        # If there is a child matcher, restrict if we can. As a special case, we
        # don't wrap __dict__ if we're asked not to.
        if __name != "__dict__" or state.wrap_dict:
            child_matcher = state.matcher.child_matcher(__name)
            if child_matcher and _is_restrictable(ret):
                ret = _RestrictedProxy(f"{state.name}.{__name}", ret, child_matcher)
        return ret

    def __setattr__(self, __name: str, __value: Any) -> None:
        state = _RestrictionState.from_proxy(self)
        _trace("__setattr__ %s on %s", __name, state.name)
        state.assert_child_not_restricted(__name)
        setattr(state.obj, __name, __value)

    def __call__(self, *args, **kwargs) -> _RestrictedProxy:
        state = _RestrictionState.from_proxy(self)
        _trace("__call__ on %s", state.name)
        state.assert_child_not_restricted("__call__")
        ret = state.obj(*args, **kwargs)  # type: ignore
        # Always wrap the result of a call to self with the same restrictions
        # (this is often instantiating a class)
        if _is_restrictable(ret):
            ret = _RestrictedProxy(state.name, ret, state.matcher)
        return ret

    def __getitem__(self, key: Any) -> Any:
        state = _RestrictionState.from_proxy(self)
        if isinstance(key, str):
            state.assert_child_not_restricted(key)
        _trace("__getitem__ %s on %s", key, state.name)
        ret = operator.getitem(state.obj, key)  # type: ignore
        # If there is a child matcher, restrict if we can
        if isinstance(key, str):
            child_matcher = state.matcher.child_matcher(key)
            if child_matcher and _is_restrictable(ret):
                ret = _RestrictedProxy(f"{state.name}.{key}", ret, child_matcher)
        return ret

    __doc__ = _RestrictedProxyLookup(  # type: ignore
        class_value=__doc__, fallback_func=lambda self: type(self).__doc__, is_attr=True
    )
    __wrapped__ = _RestrictedProxyLookup(
        fallback_func=lambda self: _RestrictionState.from_proxy(self).obj, is_attr=True
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
    def __init__(self, mod: types.ModuleType, matcher: SandboxMatcher) -> None:
        _RestrictedProxy.__init__(self, mod.__name__, mod, matcher)
        types.ModuleType.__init__(self, mod.__name__, mod.__doc__)


class RestrictedWorkflowAccessError(temporalio.workflow.NondeterminismError):
    def __init__(self, qualified_name: str) -> None:
        super().__init__(f"Cannot access {qualified_name} from inside a workflow.")
        self.qualified_name = qualified_name
