import dataclasses
import logging
import typing

import temporalio.worker

logger = logging.getLogger(__name__)


@dataclasses.dataclass(frozen=True)
class InterceptedActivity:
    class_name: str
    name: typing.Optional[str]
    qualname: typing.Optional[str]
    module: typing.Optional[str]
    annotations: typing.Dict[str, typing.Any]
    docstring: typing.Optional[str]


class ReflectionInterceptor(temporalio.worker.Interceptor):
    """Interceptor to check we haven't broken reflection when wrapping the activity."""

    def __init__(self) -> None:
        self._intercepted_activities: list[InterceptedActivity] = []

    def get_intercepted_activities(self) -> typing.List[InterceptedActivity]:
        """Get the list of intercepted activities."""
        return self._intercepted_activities

    def intercept_activity(
        self, next: temporalio.worker.ActivityInboundInterceptor
    ) -> temporalio.worker.ActivityInboundInterceptor:
        """Method called for intercepting an activity.

        Args:
            next: The underlying inbound interceptor this interceptor should
                delegate to.

        Returns:
            The new interceptor that will be used to for the activity.
        """
        return _ReflectionActivityInboundInterceptor(next, self)


class _ReflectionActivityInboundInterceptor(
    temporalio.worker.ActivityInboundInterceptor
):
    def __init__(
        self,
        next: temporalio.worker.ActivityInboundInterceptor,
        root: ReflectionInterceptor,
    ) -> None:
        super().__init__(next)
        self.root = root

    async def execute_activity(
        self, input: temporalio.worker.ExecuteActivityInput
    ) -> typing.Any:
        """Called to invoke the activity."""

        try:
            self.root._intercepted_activities.append(
                InterceptedActivity(
                    class_name=input.fn.__class__.__name__,
                    name=getattr(input.fn, "__name__", None),
                    qualname=getattr(input.fn, "__qualname__", None),
                    module=getattr(input.fn, "__module__", None),
                    docstring=getattr(input.fn, "__doc__", None),
                    annotations=getattr(input.fn, "__annotations__", {}),
                )
            )
        except AttributeError:
            logger.exception(
                "Activity function does not have expected attributes, skipping reflection."
            )

        return await self.next.execute_activity(input)
