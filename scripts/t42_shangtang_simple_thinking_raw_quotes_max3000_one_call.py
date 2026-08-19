#!/usr/bin/env python3
"""Run the t41 one-call harness with raw-quote packet rendering."""
from __future__ import annotations

import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) != 3:
        raise SystemExit("usage: t42_shangtang_simple_thinking_raw_quotes_max3000_one_call.py SOURCE_ROOT OUTPUT_DIR")

    root = Path(sys.argv[1]).resolve()
    sys.path.insert(0, str(root / "l1" / "tools"))
    sys.path.insert(0, str(root / "l1"))

    import build_research_full_context_human_packet_simple_thinking_raw_quotes as raw_quotes

    # Reuse the already-audited one-call transport harness. The only experiment
    # variable is the packet renderer bound to the expected module name.
    sys.modules["build_research_full_context_human_packet_simple_thinking"] = raw_quotes

    import t41_shangtang_simple_thinking_max3000_one_call as t41

    return t41.main()


if __name__ == "__main__":
    raise SystemExit(main())
