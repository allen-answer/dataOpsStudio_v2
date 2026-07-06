"""文件源行读取器(C-1 文件源对比 domain 层,设计稿 §四.2)。

纯 domain:CSV(stdlib)/ Excel(openpyxl),统一 `RowReader` 契约。
**零 DB 依赖(R1)** —— 1.x 的 `SqlReader` 刻意不移植进本层。
Parquet 归次版(pyarrow 懒依赖),本层暂不含。
"""

from app.domain.readers.base import (
    MaxRowsExceededError,
    ReaderError,
    RowReader,
    build_columns,
    is_blank_row,
    normalize_column_name,
    row_to_dict,
    uniquify_columns,
)
from app.domain.readers.csv_reader import CsvReader
from app.domain.readers.csv_reader import list_columns as csv_list_columns
from app.domain.readers.excel_reader import (
    ExcelReader,
    list_sheets,
)
from app.domain.readers.excel_reader import list_columns as excel_list_columns

__all__ = [
    "CsvReader",
    "ExcelReader",
    "MaxRowsExceededError",
    "ReaderError",
    "RowReader",
    "build_columns",
    "csv_list_columns",
    "excel_list_columns",
    "is_blank_row",
    "list_sheets",
    "normalize_column_name",
    "row_to_dict",
    "uniquify_columns",
]
