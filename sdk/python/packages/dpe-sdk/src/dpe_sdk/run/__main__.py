"""``python -m dpe_sdk.run``：等同 ``dpe-run`` 命令。"""

import sys

from dpe_sdk.run.cli import main

sys.exit(main())
