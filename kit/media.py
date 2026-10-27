"""Checks for customer photos, shared by every channel.

Cloned from our earlier captions chat (fetch, base64, send as an image part),
with a type and size check added before anything reaches the model.
"""
import base64
import hashlib

IMAGE_TYPES = ("image/jpeg", "image/png", "image/webp")
MAX_IMAGE_BYTES = 5 * 1024 * 1024  # 5 MB


def sniff(data):
    """The image type from the file's first bytes, or None. The declared type can lie."""
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def check_image(data, mime):
    """(ok, reason). ok only for a jpeg, png or webp under the size cap whose bytes match its type."""
    mime = (mime or "").split(";")[0].strip().lower()
    if mime not in IMAGE_TYPES:
        return False, "type"
    if not data or len(data) > MAX_IMAGE_BYTES:
        return False, "size"
    if sniff(data) != mime:
        return False, "type"
    return True, ""


def data_uri(data, mime):
    return f"data:{mime};base64," + base64.b64encode(data).decode("ascii")


def image_part(data, mime):
    """An OpenAI-style image content part with a data: URI."""
    return {"type": "image_url", "image_url": {"url": data_uri(data, mime)}}


def fingerprint(kind, data, mime):
    """What SQLite keeps of a media file: its hash and type, never the bytes."""
    return {"kind": kind, "sha256": hashlib.sha256(data).hexdigest(), "mime": mime}
