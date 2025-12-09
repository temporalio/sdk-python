from collections.abc import Mapping


class PromMetricMatcher:
    def __init__(self, prom_lines: list[str]) -> None:
        self._prom_lines = prom_lines

    # Intentionally naive metric checker
    def matches_metric_line(
        self, line: str, name: str, at_least_labels: Mapping[str, str], value: int
    ) -> bool:
        # Must have metric name
        if not line.startswith(name + "{"):
            return False
        # Must have labels (don't escape for this test)
        for k, v in at_least_labels.items():
            if f'{k}="{v}"' not in line:
                return False
        return line.endswith(f" {value}")

    def assert_metric_exists(
        self, name: str, at_least_labels: Mapping[str, str], value: int
    ) -> None:
        assert any(
            self.matches_metric_line(line, name, at_least_labels, value)
            for line in self._prom_lines
        )

    def assert_description_exists(self, name: str, description: str) -> None:
        assert f"# HELP {name} {description}" in self._prom_lines
