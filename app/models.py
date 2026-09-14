"""接口的响应/消息模型。

请求体没有用模型：``app/routes.py`` 的入口都是 ``await request.body()`` + ``json.loads``，
这样即使前端多传字段、或字段缺失也能自己兜默认值，不会被 pydantic 直接 422 掉。
"""

from typing import Optional, List

from pydantic import BaseModel


class MessageItem(BaseModel):
    role: str
    content: str
    reasoning: Optional[str] = None


class ChatHistoryResponse(BaseModel):
    thread_id: str
    messages: List[MessageItem]


class ThreadInfo(BaseModel):
    thread_id: str
    last_message: Optional[str] = None


class FileAnalysisResponse(BaseModel):
    file_id: str
    filename: str
    file_type: str
    row_count: int
    columns: List[str]
    preview: str
    analysis_type: str
    code: Optional[str] = None
