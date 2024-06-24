from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import timedelta
from typing import Literal, Optional, TypeAlias, Union

import temporalio.bridge.worker


@dataclass(frozen=True)
class FixedSizeSlotSupplier:
    num_slots: int


@dataclass(frozen=True)
class ResourceBasedTunerOptions:
    target_memory_usage: float
    """A value between 0 and 1 that represents the target (system) memory usage. It's not recommended
       to set this higher than 0.8, since how much memory a workflow may use is not predictable, and
       you don't want to encounter OOM errors."""
    target_cpu_usage: float
    """A value between 0 and 1 that represents the target (system) CPU usage. This can be set to 1.0
       if desired, but it's recommended to leave some headroom for other processes."""


@dataclass(frozen=True)
class ResourceBasedSlotOptions:
    minimum_slots: Optional[int]
    maximum_slots: Optional[int]
    ramp_throttle: Optional[timedelta]


@dataclass(frozen=True)
class ResourceBasedSlotSupplier:
    slot_options: ResourceBasedSlotOptions
    tuner_options: ResourceBasedTunerOptions


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
            ramp_throttle,
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
    def get_workflow_task_slot_supplier(self) -> SlotSupplier:
        raise NotImplementedError

    @abstractmethod
    def get_activity_task_slot_supplier(self) -> SlotSupplier:
        raise NotImplementedError

    @abstractmethod
    def get_local_activity_task_slot_supplier(self) -> SlotSupplier:
        raise NotImplementedError


class ResourceBasedTuner(WorkerTuner):
    def __init__(self, options: ResourceBasedTunerOptions):
        self.options = options
        self.workflow_task_options: Optional[ResourceBasedSlotOptions] = None
        self.activity_task_options: Optional[ResourceBasedSlotOptions] = None
        self.local_activity_task_options: Optional[ResourceBasedSlotOptions] = None

    def set_workflow_task_options(self, options: ResourceBasedSlotOptions):
        self.workflow_task_options = options

    def set_activity_task_options(self, options: ResourceBasedSlotOptions):
        self.activity_task_options = options

    def set_local_activity_task_options(self, options: ResourceBasedSlotOptions):
        self.local_activity_task_options = options

    def get_workflow_task_slot_supplier(self) -> SlotSupplier:
        return ResourceBasedSlotSupplier(
            self.workflow_task_options or ResourceBasedSlotOptions(None, None, None),
            self.options,
        )

    def get_activity_task_slot_supplier(self) -> SlotSupplier:
        return ResourceBasedSlotSupplier(
            self.activity_task_options or ResourceBasedSlotOptions(None, None, None),
            self.options,
        )

    def get_local_activity_task_slot_supplier(self) -> SlotSupplier:
        return ResourceBasedSlotSupplier(
            self.local_activity_task_options
            or ResourceBasedSlotOptions(None, None, None),
            self.options,
        )


@dataclass(frozen=True)
class CompositeTuner(WorkerTuner):
    workflow_slot_supplier: SlotSupplier
    activity_slot_supplier: SlotSupplier
    local_activity_slot_supplier: SlotSupplier

    def get_workflow_task_slot_supplier(self) -> SlotSupplier:
        return self.workflow_slot_supplier

    def get_activity_task_slot_supplier(self) -> SlotSupplier:
        return self.activity_slot_supplier

    def get_local_activity_task_slot_supplier(self) -> SlotSupplier:
        return self.local_activity_slot_supplier
