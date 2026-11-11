"""Bot API 10.4 HTML widgets on top of aiogram 3.31.

aiogram 3.31 knows Bot API 10.3, so the widget methods and the widget_query update are
declared here. aiogram 3.32 is expected to ship them (aiogram PR #2004); this module can
then be replaced by the built-in types.

Docs: https://core.telegram.org/bots/widgets
"""

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware, Bot
from aiogram.methods.base import TelegramMethod
from aiogram.types import Chat, Message, ReplyParameters, TelegramObject, Update, User
from pydantic import BaseModel, ConfigDict, Field

log = logging.getLogger(__name__)

# Names to add to allowed_updates. Forwards of shared widgets (shared_widget_query) are
# not used: our widgets are not shared and live only in the private chat with the bot.
WIDGET_UPDATE_TYPES = ("widget_query",)

MAX_HTML_BYTES = 32768
MAX_HTML_COMPRESSED_BYTES = 4096  # zlib level 9
MAX_DATA_BYTES = 1024  # chat_data and answers
MAX_QUERY_BYTES = 64


class SendWidget(TelegramMethod[Message]):
    """Send a message with an HTML widget (private chats only in production)."""

    __returning__ = Message
    __api_method__ = "sendWidget"

    chat_id: int | str
    html: str
    caption: str | None = None
    width: int | None = None
    height: int | None = None
    chat_data: str | None = None
    allow_queries: bool | None = None
    disable_notification: bool | None = None
    reply_parameters: ReplyParameters | None = None


class EditMessageWidgetData(TelegramMethod[Message | bool]):
    """Change the chat state of a widget; the widget gets chatDataChanged."""

    __returning__ = Message | bool
    __api_method__ = "editMessageWidgetData"

    chat_id: int | str
    message_id: int
    chat_data: str


class AnswerWidgetQuery(TelegramMethod[bool]):
    """Answer TelegramWidget.request() within 15 seconds."""

    __returning__ = bool
    __api_method__ = "answerWidgetQuery"

    widget_query_id: str
    data: str | None = None


class WidgetQuery(BaseModel):
    """A request a widget sent with TelegramWidget.request(data)."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    id: str
    from_user: User = Field(alias="from")
    chat: Chat | None = None
    message_id: int | None = None
    inline_message_id: str | None = None
    data: str = ""


type WidgetQueryHandler = Callable[[Bot, WidgetQuery], Awaitable[None]]


class WidgetQueryMiddleware(BaseMiddleware):
    """Outer update middleware: aiogram 3.31 drops widget_query as an unknown update
    type, so the raw field is picked up here before the dispatcher looks at it."""

    def __init__(self, on_query: WidgetQueryHandler) -> None:
        self.on_query = on_query

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        raw = (event.model_extra or {}).get("widget_query") if isinstance(event, Update) else None
        if raw is None:
            return await handler(event, data)
        try:
            query = WidgetQuery.model_validate(raw)
        except ValueError:
            log.warning("unreadable widget_query: %r", raw)
            return None
        await self.on_query(data["bot"], query)
        return None
