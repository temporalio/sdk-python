from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import timedelta
from typing import Literal, Optional, Union

from typing_extensions import TypeAlias

import temporalio.bridge.worker


@dataclass(frozen=True)
class FixedSizeSlotSupplier:
    """A fixed-size slot supplier that will never issue more than a fixed number of slots."""

    num_slots: int
    """The maximum number of slots that can be issued"""


@dataclass(frozen=True)
class ResourceBasedTunerOptions:
    """Options for a :py:class:`ResourceBasedTuner` or a :py:class:`ResourceBasedSlotSupplier`.

    .. warning::
        The resource based tuner is currently experimental.
    """

    target_memory_usage: float
    """A value between 0 and 1 that represents the target (system) memory usage. It's not recommended
       to set this higher than 0.8, since how much memory a workflow may use is not predictable, and
       you don't want to encounter OOM errors."""
    target_cpu_usage: float
    """A value between 0 and 1 that represents the target (system) CPU usage. This can be set to 1.0
       if desired, but it's recommended to leave some headroom for other processes."""


@dataclass(frozen=True)
class ResourceBasedSlotOptions:
    """Options for a specific slot type being used with a :py:class:`ResourceBasedSlotSupplier`.

    .. warning::
        The resource based tuner is currently experimental.
    """

    minimum_slots: Optional[int]
    """Amount of slots that will be issued regardless of any other checks"""
    maximum_slots: Optional[int]
    """Maximum amount of slots permitted"""
    ramp_throttle: Optional[timedelta]
    """Minimum time we will wait (after passing the minimum slots number) between handing out new slots in milliseconds.
    This value matters because how many resources a task will use cannot be determined ahead of time, and thus the
    system should wait to see how much resources are used before issuing more slots."""


@dataclass(frozen=True)
class ResourceBasedSlotSupplier:
    """A slot supplier that will dynamically adjust the number of slots based on resource usage.

    .. warning::
        The resource based tuner is currently experimental.
    """

    slot_options: ResourceBasedSlotOptions
    tuner_options: ResourceBasedTunerOptions
    """Options for the tuner that will be used to adjust the number of slots. When used with a
    :py:class:`CompositeTuner`, all resource-based slot suppliers must use the same tuner options."""


SlotSupplier: TypeAlias = Union[FixedSizeSlotSupplier, ResourceBasedSlotSupplier]


def _to_bridge_slot_supplier(
    slot_supplier: SlotSupplier, kind: Literal["workflow", "activity", "local_activity"]
) -> temporalio.bridge.worker.SlotSupplier:
    if isinstance(slot_supplier, FixedSizeSlotSupplier):
        return temporalio.bridge.worker.FixedSizeSlotSupplier(slot_supplier.num_slots)
    elif isinstance(slot_supplier, ResourceBasedSlotSupplier):
        min_slots = 5 if kind == "workflow" else 1
        max_slots = 500
        ramp_throttle = (
            timedelta(seconds=0) if kind == "workflow" else timedelta(milliseconds=50)
        )
        if slot_supplier.slot_options.minimum_slots is not None:
            min_slots = slot_supplier.slot_options.minimum_slots
        if slot_supplier.slot_options.maximum_slots is not None:
            max_slots = slot_supplier.slot_options.maximum_slots
        if slot_supplier.slot_options.ramp_throttle is not None:
            ramp_throttle = slot_supplier.slot_options.ramp_throttle
        return temporalio.bridge.worker.ResourceBasedSlotSupplier(
            min_slots,
            max_slots,
            int(ramp_throttle / timedelta(milliseconds=1)),
            temporalio.bridge.worker.ResourceBasedTunerOptions(
                slot_supplier.tuner_options.target_memory_usage,
                slot_supplier.tuner_options.target_cpu_usage,
            ),
        )
    else:
        raise TypeError(f"Unknown slot supplier type: {slot_supplier}")


class WorkerTuner(ABC):
    """WorkerTuners allow for the dynamic customization of some aspects of worker configuration"""

    @abstractmethod
    def _get_workflow_task_slot_supplier(self) -> SlotSupplier:
        raise NotImplementedError

    @abstractmethod
    def _get_activity_task_slot_supplier(self) -> SlotSupplier:
        raise NotImplementedError

    @abstractmethod
    def _get_local_activity_task_slot_supplier(self) -> SlotSupplier:
        raise NotImplementedError


class ResourceBasedTuner(WorkerTuner):
    """This tuner attempts to maintain certain levels of resource usage when under load.

    .. warning::
        The resource based tuner is currently experimental.
    """

    def __init__(self, options: ResourceBasedTunerOptions):
        """Create a new ResourceBasedTuner

        Args:
            options: Specify the target resource usage levels for the tuner
        """
        self.options = options
        self.workflow_task_options: Optional[ResourceBasedSlotOptions] = None
        self.activity_task_options: Optional[ResourceBasedSlotOptions] = None
        self.local_activity_task_options: Optional[ResourceBasedSlotOptions] = None

    def set_workflow_task_options(self, options: ResourceBasedSlotOptions):
        """Set options for the workflow task slot supplier"""
        self.workflow_task_options = options

    def set_activity_task_options(self, options: ResourceBasedSlotOptions):
        """Set options for the activity task slot supplier"""
        self.activity_task_options = options

    def set_local_activity_task_options(self, options: ResourceBasedSlotOptions):
        """Set options for the local activity task slot supplier"""
        self.local_activity_task_options = options

    def _get_workflow_task_slot_supplier(self) -> SlotSupplier:
        return ResourceBasedSlotSupplier(
            self.workflow_task_options or ResourceBasedSlotOptions(None, None, None),
            self.options,
        )

    def _get_activity_task_slot_supplier(self) -> SlotSupplier:
        return ResourceBasedSlotSupplier(
            self.activity_task_options or ResourceBasedSlotOptions(None, None, None),
            self.options,
        )

    def _get_local_activity_task_slot_supplier(self) -> SlotSupplier:
        return ResourceBasedSlotSupplier(
            self.local_activity_task_options
            or ResourceBasedSlotOptions(None, None, None),
            self.options,
        )


@dataclass(frozen=True)
class CompositeTuner(WorkerTuner):
    """This tuner allows for different slot suppliers for different slot types."""

    workflow_slot_supplier: SlotSupplier
    activity_slot_supplier: SlotSupplier
    local_activity_slot_supplier: SlotSupplier

    def _get_workflow_task_slot_supplier(self) -> SlotSupplier:
        return self.workflow_slot_supplier

    def _get_activity_task_slot_supplier(self) -> SlotSupplier:
        return self.activity_slot_supplier

    def _get_local_activity_task_slot_supplier(self) -> SlotSupplier:
        return self.local_activity_slot_supplier
