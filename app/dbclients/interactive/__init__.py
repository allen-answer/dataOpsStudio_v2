"""会话式(交互)连接缝 —— Session Broker 阶段 A 的方言层(设计 §1.2/§4/§5.4)。

`app/broker/`(A3)只依赖本包的协议,不认识任何驱动;R1 红线不受影响
—— 驱动 import 仍只发生在 `app/dbclients/` 内。

既有 job 路径的 `DatabaseAdapter`(每 job 新建连接、用完即弃、纯软取消)
**不受本包影响,也不被本包 import**:共享深模块(类型映射、错误码口径),
不共享连接与执行路径。
"""

from __future__ import annotations

from app.dbclients.interactive.base import (
    BaseCancelChannel,
    BaseInteractiveConnection,
    InvalidDatasourceError,
    validate_session_marker,
)
from app.dbclients.interactive.dm import (
    DM_CANCEL_SQL,
    DM_DEFAULT_LOGIN_TIMEOUT_MS,
    DM_DESTROY_SQL,
    DM_INTERACTIVE_CAPABILITIES,
    DM_SESSION_MARKER_SQL,
    DMCancelChannel,
    DMInteractiveConnection,
)
from app.dbclients.interactive.errors import (
    DM_ERROR_CODES,
    MYSQL_ERROR_CODES,
    DMErrorClassifier,
    MySQLErrorClassifier,
)
from app.dbclients.interactive.mysql import (
    MYSQL_CANCEL_SQL,
    MYSQL_DEFAULT_LOGIN_TIMEOUT_MS,
    MYSQL_DESTROY_SQL,
    MYSQL_INTERACTIVE_CAPABILITIES,
    MYSQL_SAFETY_BELT_MARGIN_SECONDS,
    MYSQL_SESSION_MARKER_SQL,
    MySQLCancelChannel,
    MySQLInteractiveConnection,
)
from app.dbclients.interactive.protocol import (
    CancelChannel,
    CancelChannelError,
    ClassifiedError,
    ErrorCategory,
    ErrorClassifier,
    InteractiveCapabilities,
    InteractiveConnectError,
    InteractiveConnection,
    InteractiveError,
    InteractiveExecuteError,
    ServerCancelSupport,
    SessionNotOpenError,
    SoftCancelledError,
    StatementRequest,
)

__all__ = [
    "DM_CANCEL_SQL",
    "DM_DEFAULT_LOGIN_TIMEOUT_MS",
    "DM_DESTROY_SQL",
    "DM_ERROR_CODES",
    "DM_INTERACTIVE_CAPABILITIES",
    "DM_SESSION_MARKER_SQL",
    "MYSQL_CANCEL_SQL",
    "MYSQL_DEFAULT_LOGIN_TIMEOUT_MS",
    "MYSQL_DESTROY_SQL",
    "MYSQL_ERROR_CODES",
    "MYSQL_INTERACTIVE_CAPABILITIES",
    "MYSQL_SAFETY_BELT_MARGIN_SECONDS",
    "MYSQL_SESSION_MARKER_SQL",
    "BaseCancelChannel",
    "BaseInteractiveConnection",
    "CancelChannel",
    "CancelChannelError",
    "ClassifiedError",
    "DMCancelChannel",
    "DMErrorClassifier",
    "DMInteractiveConnection",
    "ErrorCategory",
    "ErrorClassifier",
    "InteractiveCapabilities",
    "InteractiveConnectError",
    "InteractiveConnection",
    "InteractiveError",
    "InteractiveExecuteError",
    "InvalidDatasourceError",
    "MySQLCancelChannel",
    "MySQLErrorClassifier",
    "MySQLInteractiveConnection",
    "ServerCancelSupport",
    "SessionNotOpenError",
    "SoftCancelledError",
    "StatementRequest",
    "validate_session_marker",
]
