"""Cropping faces out of Immich images using Immich's bounding boxes."""

from __future__ import annotations

import io

from PIL import Image, ImageOps

from .immich import Face

Image.MAX_IMAGE_PIXELS = 400_000_000  # large flatbed scans are fine


def load_image(data: bytes) -> Image.Image:
    img = Image.open(io.BytesIO(data))
    img = ImageOps.exif_transpose(img)
    return img.convert("RGB")


def face_box(face: Face, image: Image.Image, margin: float) -> tuple[int, int, int, int]:
    """Bounding box of `face` scaled to `image`, grown by `margin` (fraction of the face size) per side."""
    sx = image.width / face.image_width
    sy = image.height / face.image_height
    x1, x2 = face.x1 * sx, face.x2 * sx
    y1, y2 = face.y1 * sy, face.y2 * sy
    mx, my = (x2 - x1) * margin, (y2 - y1) * margin
    return (
        max(0, round(x1 - mx)),
        max(0, round(y1 - my)),
        min(image.width, round(x2 + mx)),
        min(image.height, round(y2 + my)),
    )


def face_size(face: Face, image: Image.Image) -> float:
    """Shorter side of the face in pixels of `image`."""
    sx = image.width / face.image_width
    sy = image.height / face.image_height
    return min((face.x2 - face.x1) * sx, (face.y2 - face.y1) * sy)


def crop_face(image: Image.Image, face: Face, margin: float) -> Image.Image:
    return image.crop(face_box(face, image, margin))
