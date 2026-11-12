"""Comment lines with no code after them at their indentation, new relative to target."""
import subprocess
import sys


def orphans(text: str) -> set[str]:
    lines = text.splitlines()
    found = set()
    for index, line in enumerate(lines):
        stripped = line.lstrip()
        if not stripped.startswith("#") or stripped.startswith("#!") or "noqa" in stripped or "type:" in stripped:
            continue
        indent = len(line) - len(stripped)
        following = next((other for other in lines[index + 1 :] if other.strip()), None)
        if following is None:
            found.add(stripped)
            continue
        other_indent = len(following) - len(following.lstrip())
        if other_indent < indent or (following.lstrip().startswith(("def ", "class ", "@")) and index + 1 < len(lines) and not lines[index + 1].strip()):
            found.add(stripped)
    return found


def files(ref: str | None) -> dict[str, str]:
    names = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", ref] if ref else ["git", "ls-files"],
        capture_output=True, text=True, check=True,
    ).stdout.split()
    result = {}
    for name in names:
        if not name.endswith(".py"):
            continue
        if ref:
            result[name] = subprocess.run(["git", "show", f"{ref}:{name}"], capture_output=True, text=True).stdout
        else:
            try:
                result[name] = open(name, encoding="utf-8").read()
            except OSError:
                pass
    return result


target = {name: orphans(text) for name, text in files("target").items()}
for name, text in sorted(files(sys.argv[1] if len(sys.argv) > 1 else None).items()):
    for comment in sorted(orphans(text) - target.get(name, set())):
        print(f"{name}: {comment}")
