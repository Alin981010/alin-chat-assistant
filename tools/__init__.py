"""``tools`` 包：供 ``app/`` 复用的工具集。

目前只有文件解析（``tools.file_handler``），被 ``app/routes.py`` 的
``/api/files/upload`` 与 ``/api/chat-with-file*`` 使用。
"""

from .file_handler import FileHandler, ParsedFile

__all__ = ["FileHandler", "ParsedFile"]
