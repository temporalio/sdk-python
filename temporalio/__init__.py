"""Python SDK for Temporal.

See the
`Temporal Application Development Guide <https://docs.temporal.io/application-development/?lang=python>`_
and the `GitHub project <https://github.com/temporalio/sdk-python>`_.

Most users will use :py:mod:`client` for creating a client to Temporal and
:py:mod:`worker` to run workflows and activities.
"""

from pkgutil import extend_path as _extend_path

from .service import __version__ as __sdk_version

# For example, find ``temporalio.openai_agents`` in site-packages when this SDK
# is imported from an editable checkout in a different directory.
__path__ = _extend_path(__path__, __name__)

__version__ = __sdk_version
