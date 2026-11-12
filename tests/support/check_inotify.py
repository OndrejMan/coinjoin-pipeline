"""Check host limits shared by rootless Podman, DinD and the Wasabi processes."""

from pathlib import Path

SYSCTL_ROOT = Path("/proc/sys/fs/inotify")
MINIMUM_LIMITS = {"max_user_instances": 1024, "max_user_watches": 524288}


def main(root: Path = SYSCTL_ROOT) -> int:
    if not root.is_dir():
        return 0
    problems = []
    for name, minimum in MINIMUM_LIMITS.items():
        try:
            value = int((root / name).read_text().strip())
        except (OSError, ValueError) as error:
            problems.append(f"cannot read fs.inotify.{name}: {error}")
            continue
        if value < minimum:
            problems.append(f"fs.inotify.{name}={value}, need at least {minimum}")
    if not problems:
        return 0
    print("FAIL: insufficient host inotify limits for the rootless Podman emulation:")
    for problem in problems:
        print(f"  {problem}")
    print("DinD can fail with 'failed to create inotify fd: too many open files'.")
    print("Configure the host before rerunning (raising ulimit -n does not raise these limits):")
    print("  sudo sysctl -w fs.inotify.max_user_instances=1024 fs.inotify.max_user_watches=524288")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
