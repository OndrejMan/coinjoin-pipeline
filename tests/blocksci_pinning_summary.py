"""Summarise the first <scope> blocks of a BlockSci index, optionally opened pinned at <pin>.

Helper for test-blocksci-incremental-pinning.sh; runs inside the BlockSci image.
"""

import argparse
import json

import blocksci

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("config")
parser.add_argument("--pin", type=int, help="open the chain with Blockchain(config, pin)")
parser.add_argument("--scope", type=int, help="count spends only for outputs in the first SCOPE blocks")
args = parser.parse_args()

chain = blocksci.Blockchain(args.config, args.pin) if args.pin else blocksci.Blockchain(args.config)
scope = args.scope or len(chain)
outputs = [output for block in chain[:scope] for tx in block for output in tx.outputs]
print(
    json.dumps(
        {
            "blocks": len(chain),
            "txes": sum(block.tx_count for block in chain),
            "last_block": str(chain[len(chain) - 1].hash),
            # Spends are written back into older outputs, so this is where a pinned view can drift.
            "spent_outputs": sum(1 for output in outputs if output.is_spent),
            "spent_later": sum(1 for output in outputs if output.is_spent and output.spending_tx.block_height >= scope),
        },
        sort_keys=True,
    )
)
