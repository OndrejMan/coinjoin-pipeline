"""rebase --exec helper: name run-all.sh as the local image builder only in trees that have it."""
from pathlib import Path

if not Path("run-all.sh").exists():
    for path, old, new in (
        (
            "src/coinjoin_pipeline/images.py",
            "# What ./run-all.sh local tags its images as.\n",
            "# The tags a local build of each component's repository produces.\n",
        ),
        (
            "src/coinjoin_pipeline/arguments.py",
            '"images.local_build": "use the images ./run-all.sh local builds (*:local)",',
            '"images.local_build": "use locally built images (*:local)",',
        ),
    ):
        p = Path(path)
        if p.exists() and old in p.read_text():
            p.write_text(p.read_text().replace(old, new))
