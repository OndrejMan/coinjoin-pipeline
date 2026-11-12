import sys
from pathlib import Path
p = Path("src/coinjoin_pipeline/execution/pbs/index_store.py")
t = p.read_text()
old, new = sys.argv[1], sys.argv[2]
old = old.replace("\\n", "\n")
new = new.replace("\\n", "\n")
assert t.count(old) == 1, old
p.write_text(t.replace(old, new))
