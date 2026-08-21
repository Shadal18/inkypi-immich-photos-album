from __future__ import annotations

import io
import logging
import random
import textwrap
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

import requests
from PIL import Image, ImageDraw, ImageOps

from plugins.base_plugin.base_plugin import BasePlugin


LOGGER = logging.getLogger(__name__)


@dataclass
class ImmichPhoto:
    asset_id: str
    filename: str
    created_at: str
    width: int
    height: int


class ImmichPhotosAlbum(BasePlugin):
    def __init__(self, config):
        super().__init__(config)

    def generate_image(self, settings, device_config, inky_display=None):
        server_url = (settings.get("server_url") or "").strip().rstrip("/")
        album_id = (settings.get("album_id") or "").strip()
        api_key_name = (settings.get("api_key_name") or "IMMICH_API_KEY").strip()

        if not server_url:
            return self._error_image(device_config, "Immich server URL is required")

        if not album_id:
            return self._error_image(device_config, "Album ID is required")

        api_key = device_config.get_config(api_key_name)
        if not api_key:
            return self._error_image(
                device_config,
                f"Missing environment key\n{api_key_name}",
            )

        timeout_seconds = self._as_int(
            settings.get("timeout_seconds"),
            default=30,
            minimum=5,
            maximum=120,
        )

        selection_mode = (settings.get("selection") or "random").strip().lower()
        fit_mode = (settings.get("fit_mode") or "fill").strip().lower()
        background = (settings.get("background") or "white").strip().lower()
        show_caption = self._as_bool(settings.get("show_caption"), default=False)
        caption_mode = (settings.get("caption_mode") or "album").strip().lower()
        show_border = self._as_bool(settings.get("show_border"), default=False)
        border_px = self._as_int(
            settings.get("border_px"),
            default=8,
            minimum=0,
            maximum=40,
        )
        enhance_contrast = self._as_bool(
            settings.get("enhance_contrast"),
            default=False,
        )

        canvas_size = self._display_size(device_config)

        LOGGER.info(
            "Immich Photos Album: loading album %s from %s",
            album_id,
            server_url,
        )

        try:
            session = self._create_session(api_key)

            album = self._fetch_album(
                session=session,
                server_url=server_url,
                album_id=album_id,
                timeout_seconds=timeout_seconds,
            )

            album_name = str(album.get("albumName") or "Immich Album")
            photos = self._extract_photos(album.get("assets", []))

            if not photos:
                return self._error_image(
                    device_config,
                    "No image assets found\nin this album",
                )

            selected = self._pick_photo(photos, selection_mode)

            image = self._download_photo(
                session=session,
                server_url=server_url,
                photo=selected,
                timeout_seconds=timeout_seconds,
            )

            if image is None:
                return self._error_image(
                    device_config,
                    "Failed to download\nselected photo",
                )

            return self._compose_canvas(
                image=image,
                canvas_size=canvas_size,
                fit_mode=fit_mode,
                background=background,
                show_border=show_border,
                border_px=border_px,
                show_caption=show_caption,
                caption_text=self._caption_text(
                    album_name=album_name,
                    photo=selected,
                    caption_mode=caption_mode,
                ),
                enhance_contrast=enhance_contrast,
            )

        except Exception as exc:
            LOGGER.exception("Immich Photos Album plugin error: %s", exc)
            return self._error_image(device_config, "Plugin error")

    def _create_session(self, api_key: str) -> requests.Session:
        session = requests.Session()
        session.headers.update(
            {
                "x-api-key": api_key,
                "Accept": "application/json",
                "User-Agent": "InkyPi-Immich-Photos-Album/1.0",
            }
        )
        return session

    def _fetch_album(
        self,
        session: requests.Session,
        server_url: str,
        album_id: str,
        timeout_seconds: int,
    ) -> dict[str, Any]:
        endpoint = f"{server_url}/api/albums/{album_id}"

        response = session.get(endpoint, timeout=timeout_seconds)
        response.raise_for_status()

        payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError("Immich returned an unexpected album response.")

        return payload

    def _extract_photos(self, assets: Any) -> list[ImmichPhoto]:
        if not isinstance(assets, list):
            return []

        photos: list[ImmichPhoto] = []

        for asset in assets:
            if not isinstance(asset, dict):
                continue

            if asset.get("type") != "IMAGE":
                continue

            asset_id = asset.get("id")
            if not isinstance(asset_id, str) or not asset_id:
                continue

            filename = str(
                asset.get("originalFileName")
                or asset.get("originalPath")
                or "Immich Photo"
            )

            created_at = str(
                asset.get("fileCreatedAt")
                or asset.get("localDateTime")
                or asset.get("createdAt")
                or ""
            )

            width = self._as_int(asset.get("exifInfo", {}).get("exifImageWidth"), 0)
            height = self._as_int(asset.get("exifInfo", {}).get("exifImageHeight"), 0)

            photos.append(
                ImmichPhoto(
                    asset_id=asset_id,
                    filename=filename,
                    created_at=created_at,
                    width=width,
                    height=height,
                )
            )

        return photos

    def _pick_photo(
        self,
        photos: list[ImmichPhoto],
        selection_mode: str,
    ) -> ImmichPhoto:
        if selection_mode == "newest":
            return max(photos, key=lambda photo: self._timestamp(photo.created_at))

        if selection_mode == "oldest":
            return min(photos, key=lambda photo: self._timestamp(photo.created_at))

        return random.choice(photos)

    def _timestamp(self, value: str) -> float:
        if not value:
            return 0.0

        try:
            return datetime.fromisoformat(
                value.replace("Z", "+00:00")
            ).timestamp()
        except ValueError:
            return 0.0

    def _download_photo(
        self,
        session: requests.Session,
        server_url: str,
        photo: ImmichPhoto,
        timeout_seconds: int,
    ) -> Optional[Image.Image]:
        image_urls = [
            f"{server_url}/api/assets/{photo.asset_id}/thumbnail?size=preview",
            f"{server_url}/api/assets/{photo.asset_id}/original",
        ]

        for image_url in image_urls:
            try:
                response = session.get(image_url, timeout=timeout_seconds)
                response.raise_for_status()

                image = Image.open(io.BytesIO(response.content))
                image.load()

                return self._to_rgb(image)

            except Exception as exc:
                LOGGER.debug(
                    "Failed to retrieve photo from %s: %s",
                    image_url,
                    exc,
                )

        LOGGER.error("Unable to download Immich asset %s", photo.asset_id)
        return None

    def _to_rgb(self, image: Image.Image) -> Image.Image:
        if image.mode == "RGBA":
            background = Image.new("RGB", image.size, "white")
            background.paste(image, mask=image.getchannel("A"))
            return background

        if image.mode != "RGB":
            return image.convert("RGB")

        return image

    def _compose_canvas(
        self,
        image: Image.Image,
        canvas_size: tuple[int, int],
        fit_mode: str,
        background: str,
        show_border: bool,
        border_px: int,
        show_caption: bool,
        caption_text: str,
        enhance_contrast: bool,
    ) -> Image.Image:
        background_color = "black" if background == "black" else "white"
        canvas = Image.new("RGB", canvas_size, background_color)

        caption_height = 36 if show_caption else 0
        usable_width = canvas_size[0]
        usable_height = max(1, canvas_size[1] - caption_height)

        if show_border:
            usable_width = max(1, usable_width - (border_px * 2))
            usable_height = max(1, usable_height - (border_px * 2))

        if fit_mode == "contain":
            prepared = ImageOps.contain(
                image,
                (usable_width, usable_height),
                method=Image.Resampling.LANCZOS,
            )
        else:
            prepared = ImageOps.fit(
                image,
                (usable_width, usable_height),
                method=Image.Resampling.LANCZOS,
                centering=(0.5, 0.5),
            )

        if enhance_contrast:
            prepared = ImageOps.autocontrast(prepared)

        x = (canvas_size[0] - prepared.width) // 2
        y = max(0, (usable_height - prepared.height) // 2)

        if show_border:
            x = max(border_px, x)
            y += border_px

        canvas.paste(prepared, (x, y))

        if show_border and border_px > 0:
            outline_color = "white" if background_color == "black" else "black"
            draw = ImageDraw.Draw(canvas)

            draw.rectangle(
                [
                    0,
                    0,
                    canvas_size[0] - 1,
                    canvas_size[1] - caption_height - 1,
                ],
                outline=outline_color,
                width=max(1, min(3, border_px // 2 or 1)),
            )

        if show_caption:
            self._draw_caption(
                canvas=canvas,
                text=caption_text,
                background_color=background_color,
            )

        return canvas

    def _draw_caption(
        self,
        canvas: Image.Image,
        text: str,
        background_color: str,
    ) -> None:
        draw = ImageDraw.Draw(canvas)
        width, height = canvas.size
        bar_height = 36
        top = height - bar_height

        bar_color = 0 if background_color == "white" else 255
        text_color = 255 if background_color == "white" else 0

        draw.rectangle([0, top, width, height], fill=bar_color)

        caption = text.strip() or "Immich Album"
        if len(caption) > 44:
            caption = caption[:43] + "…"

        font = getattr(self, "font_small", getattr(self, "font", None))

        if font:
            box = draw.textbbox((0, 0), caption, font=font)
            text_width = box[2] - box[0]
            text_height = box[3] - box[1]

            x = (width - text_width) // 2
            y = top + ((bar_height - text_height) // 2) - box[1]

            draw.text((x, y), caption, fill=text_color, font=font)
        else:
            draw.text((10, top + 10), caption, fill=text_color)

    def _caption_text(
        self,
        album_name: str,
        photo: ImmichPhoto,
        caption_mode: str,
    ) -> str:
        if caption_mode == "filename":
            return photo.filename

        if caption_mode == "date":
            return photo.created_at[:10] if photo.created_at else "Unknown date"

        if caption_mode == "asset_id":
            return f"Photo {photo.asset_id[:12]}"

        return album_name

    def _display_size(self, device_config) -> tuple[int, int]:
        size = device_config.get_resolution()

        if device_config.get_config("orientation") == "vertical":
            return size[::-1]

        return size

    def _error_image(self, device_config, message: str) -> Image.Image:
        canvas = Image.new("RGB", self._display_size(device_config), "white")
        draw = ImageDraw.Draw(canvas)

        text = f"Immich Photos Album\n{message}"
        lines = textwrap.wrap(text, width=24)

        font = getattr(self, "font", None)
        y = max(20, (canvas.height // 2) - (len(lines) * 10))

        for line in lines:
            if font:
                draw.text((20, y), line, fill="black", font=font)
                box = draw.textbbox((20, y), line, font=font)
                y += (box[3] - box[1]) + 4
            else:
                draw.text((20, y), line, fill="black")
                y += 18

        return canvas

    def _as_bool(self, value: Any, default: bool = False) -> bool:
        if value is None:
            return default

        return str(value).strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }

    def _as_int(
        self,
        value: Any,
        default: int,
        minimum: Optional[int] = None,
        maximum: Optional[int] = None,
    ) -> int:
        try:
            number = int(str(value).strip())
        except Exception:
            number = default

        if minimum is not None:
            number = max(minimum, number)

        if maximum is not None:
            number = min(maximum, number)

        return number