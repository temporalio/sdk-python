
from functools import partial
from typing import (
    Any,
    Callable,
    Optional,
    Type,
    TypeVar,
    overload,
)

WorkflowClass = TypeVar("WorkflowClass", bound=Type)


@overload
def defn(fn: WorkflowClass) -> WorkflowClass:
    ...


@overload
def defn(*, name: str) -> Callable[[WorkflowClass], WorkflowClass]:
    ...


def defn(cls: Optional[WorkflowClass] = None, *, name: Optional[str] = None):
    """Decorator for workflow classes.

    Activities can be async or non-async.

    Args:
        cls: The class to decorate.
        name: Name to use for the workflow. Defaults to class ``__name__``.
    """

    def with_name(name: str, cls: WorkflowClass) -> WorkflowClass:
        # TODO(cretz): Validate the workflow
        # Set the name
        setattr(cls, "__temporal_workflow_name", name)
        return cls

    # If name option is available, return decorator function
    if name is not None:
        return partial(with_name, name)
    if cls is None:
        raise RuntimeError("Cannot invoke defn without class or name")
    # Otherwise just run decorator function
    return with_name(cls.__name__, cls)