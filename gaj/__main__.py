"""允许 `python3 -m gaj` 调用, 等价于 `python3 -m gaj.cli`。"""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
