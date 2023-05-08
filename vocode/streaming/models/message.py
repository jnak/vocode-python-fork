from enum import Enum
from .model import TypedModel
from enum import Enum


# TODO(julien) This is not clear why we need this pattern here
class MessageType(str, Enum):
    BASE = "message_base"
    SSML = "message_ssml"


class BaseMessage(TypedModel, type=MessageType.BASE):
    text: str

    def __str__(self):
        return self.text


class SSMLMessage(BaseMessage, type=MessageType.SSML):
    ssml: str
