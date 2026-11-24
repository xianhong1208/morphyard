"""The operations registry (see base.py).

Importing this package registers every operation, in the order the MCP tools are
listed. Use `get_operation(name)`, `list_operations()` and `execute(...)`.
"""

from src.domain.operations import (  # noqa: F401  (imported to register)
    convert_ops,
    pdf_basic_ops,
    pdf_image_ops,
    pdf_page_ops,
    pdf_secure_ops,
)
from src.domain.operations.base import (
    Arity,
    Artifact,
    InputSource,
    OpCall,
    Operation,
    OperationRefused,
    OpResult,
    OutputFile,
    execute,
    get_operation,
    list_operations,
)

__all__ = [
    "Arity", "Artifact", "InputSource", "OpCall", "Operation", "OperationRefused",
    "OpResult", "OutputFile", "execute", "get_operation", "list_operations",
]
