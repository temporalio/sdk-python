"""Code that runs inside the workflow sandbox.

.. warning::
    This API for this module is considered unstable and may change in future.
"""

import dataclasses
import logging
from typing import Type

import temporalio.bridge.proto.workflow_activation
import temporalio.bridge.proto.workflow_completion
import temporalio.worker._workflow_instance

logger = logging.getLogger(__name__)

# Set to true to log lots of sandbox details
LOG_TRACE = False


def _trace(message: object, *args: object) -> None:
    if LOG_TRACE:
        logger.debug(message, *args)


class InSandbox:
    """Instance that is expected to run inside a sandbox."""

    def __init__(
        self,
        instance_details: temporalio.worker._workflow_instance.WorkflowInstanceDetails,
        runner_class: Type[temporalio.worker._workflow_instance.WorkflowRunner],
        workflow_class: Type,
    ) -> None:
        """Create in-sandbox instance."""
        _trace("Initializing workflow %s in sandbox", workflow_class)
        # We have to replace the given instance instance details with new one
        # replacing references to the workflow class
        old_defn = instance_details.defn
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
        new_instance_details = dataclasses.replace(instance_details, defn=new_defn)

        # Instantiate the runner and the instance
        self.instance = runner_class().create_instance(new_instance_details)

    def activate(
        self, act: temporalio.bridge.proto.workflow_activation.WorkflowActivation
    ) -> temporalio.bridge.proto.workflow_completion.WorkflowActivationCompletion:
        """Send activation to this instance."""
        return self.instance.activate(act)
