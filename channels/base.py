"""The one message shape every channel hands to an agent, whatever it came from."""
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Image:
    data: bytes
    mime: str


@dataclass
class Message:
    channel: str                    # "whatsapp" today; "instagram" and "messenger" later
    customer_id: str                # the customer's id on that channel (a phone number on WhatsApp)
    text: str = ""                  # typed text, or a photo's caption
    audio: bytes | None = None      # a voice note, if any
    audio_mime: str | None = None
    images: list[Image] = field(default_factory=list)
    rejected_images: int = 0        # photos dropped by the type or size check
    message_id: str = ""
    timestamp: int = 0


class Channel:
    """What an agent needs from a channel: a way to send text back."""

    name = "base"

    def send(self, to, text):
        raise NotImplementedError
