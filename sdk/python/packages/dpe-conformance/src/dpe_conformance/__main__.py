"""``python -m dpe_conformance``：等价于 ``dpe-conformance`` 控制台脚本。"""

from __future__ import annotations

import sys

from dpe_conformance.cli import main

if __name__ == "__main__":
    sys.exit(main())
