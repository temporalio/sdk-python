getattr_calls: list[str] = []


def __getattr__(name: str) -> int:
    getattr_calls.append(name)
    if name == "dynamic_value":
        return 42
    raise AttributeError(name)
