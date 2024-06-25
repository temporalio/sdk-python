from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import timedelta
from typing import Literal, Optional, Union

from typing_extensions import TypeAlias

import temporalio.bridge.worker

_DEFAULT_RESOURCE_ACTIVITY_MAX = 500


@dataclass(frozen=True)
class FixedSizeSlotSupplier:
    """A fixed-size slot supplier that will never issue more than a fixed number of slots."""

    num_slots: int
    """The maximum number of slots that can be issued"""


@dataclass(frozen=True)
class ResourceBasedTunerConfig:
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
class ResourceBasedSlotConfig:
    """Options for a specific slot type being used with a :py:class:`ResourceBasedSlotSupplier`.

    .. warning::
        The resource based tuner is currently experimental.
    """

    minimum_slots: Optional[int] = None
    """Amount of slots that will be issued regardless of any other checks. Defaults to 5 for workflows and 1 for
    activities."""
    maximum_slots: Optional[int] = None
    """Maximum amount of slots permitted. Defaults to 500."""
    ramp_throttle: Optional[timedelta] = None
    """Minimum time we will wait (after passing the minimum slots number) between handing out new slots in milliseconds.
    Defaults to 0 for workflows and 50ms for activities.
    
    This value matters because how many resources a task will use cannot be determined ahead of time, and thus the
    system should wait to see how much resources are used before issuing more slots."""


@dataclass(frozen=True)
class ResourceBasedSlotSupplier:
    """A slot supplier that will dynamically adjust the number of slots based on resource usage.

    .. warning::
        The resource based tuner is currently experimental.
    """

    slot_config: ResourceBasedSlotConfig
    tuner_config: ResourceBasedTunerConfig
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
        max_slots = _DEFAULT_RESOURCE_ACTIVITY_MAX
        ramp_throttle = (
            timedelta(seconds=0) if kind == "workflow" else timedelta(milliseconds=50)
        )
        if slot_supplier.slot_config.minimum_slots is not None:
            min_slots = slot_supplier.slot_config.minimum_slots
        if slot_supplier.slot_config.maximum_slots is not None:
            max_slots = slot_supplier.slot_config.maximum_slots
        if slot_supplier.slot_config.ramp_throttle is not None:
            ramp_throttle = slot_supplier.slot_config.ramp_throttle
        return temporalio.bridge.worker.ResourceBasedSlotSupplier(
            min_slots,
            max_slots,
            int(ramp_throttle / timedelta(milliseconds=1)),
            temporalio.bridge.worker.ResourceBasedTunerConfig(
                slot_supplier.tuner_config.target_memory_usage,
                slot_supplier.tuner_config.target_cpu_usage,
            ),
        )
    else:
        raise TypeError(f"Unknown slot supplier type: {slot_supplier}")


class WorkerTuner(ABC):
    """WorkerTuners allow for the dynamic customization of some aspects of worker configuration"""

    @staticmethod
    def create_resource_based(
        *,
        target_memory_usage: float,
        target_cpu_usage: float,
        workflow_config: Optional[ResourceBasedSlotConfig] = None,
        activity_config: Optional[ResourceBasedSlotConfig] = None,
        local_activity_config: Optional[ResourceBasedSlotConfig] = None,
    ) -> "WorkerTuner":
        """Create a resource-based tuner with the provided options."""
        resource_cfg = ResourceBasedTunerConfig(target_memory_usage, target_cpu_usage)
        wf = ResourceBasedSlotSupplier(
            workflow_config or ResourceBasedSlotConfig(), resource_cfg
        )
        act = ResourceBasedSlotSupplier(
            activity_config or ResourceBasedSlotConfig(), resource_cfg
        )
        local_act = ResourceBasedSlotSupplier(
            local_activity_config or ResourceBasedSlotConfig(), resource_cfg
        )
        return _CompositeTuner(
            wf,
            act,
            local_act,
        )

    @staticmethod
    def create_fixed(
        *,
        workflow_slots: Optional[int],
        activity_slots: Optional[int],
        local_activity_slots: Optional[int],
    ) -> "WorkerTuner":
        """Create a fixed-size tuner with the provided number of slots. Any unspecified slots will default to 100."""
        return _CompositeTuner(
            FixedSizeSlotSupplier(workflow_slots if workflow_slots else 100),
            FixedSizeSlotSupplier(activity_slots if activity_slots else 100),
            FixedSizeSlotSupplier(
                local_activity_slots if local_activity_slots else 100
            ),
        )

    @staticmethod
    def create_composite(
        *,
        workflow_supplier: SlotSupplier,
        activity_supplier: SlotSupplier,
        local_activity_supplier: SlotSupplier,
    ) -> "WorkerTuner":
        """Create a tuner composed of the provided slot suppliers."""
        return _CompositeTuner(
            workflow_supplier,
            activity_supplier,
            local_activity_supplier,
        )

    @abstractmethod
    def _get_workflow_task_slot_supplier(self) -> SlotSupplier:
        raise NotImplementedError

    @abstractmethod
    def _get_activity_task_slot_supplier(self) -> SlotSupplier:
        raise NotImplementedError

    @abstractmethod
    def _get_local_activity_task_slot_supplier(self) -> SlotSupplier:
        raise NotImplementedError

    def _to_bridge_tuner(self) -> temporalio.bridge.worker.TunerHolder:
        return temporalio.bridge.worker.TunerHolder(
            _to_bridge_slot_supplier(
                self._get_workflow_task_slot_supplier(), "workflow"
            ),
            _to_bridge_slot_supplier(
                self._get_activity_task_slot_supplier(), "activity"
            ),
            _to_bridge_slot_supplier(
                self._get_local_activity_task_slot_supplier(), "local_activity"
            ),
        )

    def _get_activities_max(self) -> Optional[int]:
        ss = self._get_activity_task_slot_supplier()
        if isinstance(ss, FixedSizeSlotSupplier):
            return ss.num_slots
        elif isinstance(ss, ResourceBasedSlotSupplier):
            return ss.slot_config.maximum_slots or _DEFAULT_RESOURCE_ACTIVITY_MAX
        return None


@dataclass(frozen=True)
class _CompositeTuner(WorkerTuner):
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
