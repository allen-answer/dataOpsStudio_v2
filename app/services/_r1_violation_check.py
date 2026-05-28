"""临时文件:故意违反 R1 红线(services 禁 import 数据库驱动)。

CI 应该用 ruff TID251 banned-api 拦下来。
确认 CI 报红后,本文件 + 本分支立刻删除,不入 main。
"""

import psycopg  # ★ 故意违规,验 R1 红线

_ = psycopg
