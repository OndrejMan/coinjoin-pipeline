"""Write an experiment file from YAML paths: experiment.py OUT.yaml PATH=VALUE...

Each value is read as JSON when it parses (true, 4, ["a", "b"]), else kept as a string;
a JSON object stays a string, since the settings that hold JSON (kubernetes.planned_quota) are strings.
The file is JSON, which is valid YAML, so the script needs nothing beyond the standard library.
"""

import json
import sys


def main(target: str, items: list[str]) -> None:
    data: dict[str, object] = {}
    for item in items:
        path, separator, raw = item.partition("=")
        if not separator:
            raise SystemExit(f"expected PATH=VALUE, got {item!r}")
        try:
            value = json.loads(raw)
        except ValueError:
            value = raw
        if isinstance(value, dict):
            value = raw
        node = data
        *parents, last = path.split(".")
        for part in parents:
            node = node.setdefault(part, {})  # type: ignore[assignment]
        node[last] = value
    with open(target, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2)
        handle.write("\n")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2:])
