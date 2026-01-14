"""BashFS: Sandboxed filesystem operations for AI agents."""

from bashfs.core import BashFS, BashResult, ExecutionPreset, HistoryEntry, KVStore
from bashfs.exceptions import (
    BashFSError,
    CommandError,
    ExecutionLimitError,
    FileNotFoundError,
    PermissionError,
    SubprocessCrashed,
    TimeoutError,
)

__all__ = [
    "BashFS",
    "BashFSError",
    "BashResult",
    "CommandError",
    "ExecutionLimitError",
    "ExecutionPreset",
    "FileNotFoundError",
    "HistoryEntry",
    "KVStore",
    "PermissionError",
    "SubprocessCrashed",
    "TimeoutError",
]
