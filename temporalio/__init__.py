"""Python SDK for Temporal.

See the
`Temporal Application Development Guide <https://docs.temporal.io/application-development/?lang=python>`_
and the `GitHub project <https://github.com/temporalio/sdk-python>`_.

Most users will use :py:mod:`client` for creating a client to Temporal and
:py:mod:`worker` to run workflows and activities.
"""

from pkgutil import extend_path as _extend_path

from .service import __version__ as __sdk_version

# After Python finds this ``temporalio`` package, it does not keep searching for
# other ``temporalio`` directories, such as one containing the separately
# installed ``temporalio-openai-agents`` package. Explicitly extend the search
# path so they can be found. Every package that provides ``temporalio/__init__.py``
# must do this: https://docs.python.org/3.14/library/pkgutil.html#pkgutil.extend_path
__path__ = _extend_path(__path__, __name__)

__version__ = __sdk_version
