from enum import Enum
from .model import TypedModel
from enum import Enum


class MessageType(str, Enum):
    BASE = "message_base"
    SSML = "message_ssml"


class BaseMessage(TypedModel, type=MessageType.BASE):
    text: str

    # Julien - This was needed because some places were treating this messsage as a string
    def __str__(self):
        return self.text


class SSMLMessage(BaseMessage, type=MessageType.SSML):
    ssml: str
