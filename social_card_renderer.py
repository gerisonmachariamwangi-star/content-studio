"""The Habit Forge social-card renderer, adapted from Social Card Generator v5.5.

Image background -> JPG. Video background -> MP4 with an audio track (FFmpeg required).
This module is called by unified_content_studio.py and can also be imported independently.
"""

from __future__ import annotations

import io
import os
import re
import shutil
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

import requests

try:
    from PIL import Image, ImageDraw, ImageEnhance, ImageFont, ImageOps
    PIL_AVAILABLE = True
except ImportError:  # Keep the Pinterest tab usable if Pillow is not installed yet.
    Image = ImageDraw = ImageEnhance = ImageFont = ImageOps = None
    PIL_AVAILABLE = False


def require_pillow():
    if not PIL_AVAILABLE:
        raise RuntimeError(
            "Pillow is required for social-card rendering. In the active Python environment run: python -m pip install Pillow"
        )

GOLD = (196, 154, 98)
WHITE = (255, 255, 255)
DARK = (35, 38, 32)
CHARCOAL = (60, 60, 55)
LINE_COLOR = (180, 175, 160)

PAGE_NAME = "@thehabitforgeus"
BRAND_NAME = "THE HABIT FORGE"

DEFAULT_BG_URL = "https://mzelele.github.io/pinterest-images/pins/Person_walking_mountain_trail.jpg"
DEFAULT_LOGO_URL = "https://mzelele.github.io/pinterest-images/pins/The_Habit_Forge_Blackbg.jpg"
SOCIAL_ICON_URLS = [
    "https://mzelele.github.io/pinterest-images/pins/facebook-icon.png",
    "https://mzelele.github.io/pinterest-images/pins/instagram-icon.png",
    "https://mzelele.github.io/pinterest-images/pins/Threads-logo-m.png",
]

SIZES = {
    "square": (1080, 1080),
    "portrait": (1080, 1350),
    "story": (1080, 1920),
}

_IMAGE_CACHE: dict[str, Image.Image] = {}


def is_video(path_or_url: str | Path) -> bool:
    value = str(path_or_url).lower().split("?", 1)[0]
    return any(value.endswith(ext) for ext in (".mp4", ".mov", ".avi", ".webm", ".gif", ".mkv", ".m4v"))


def ffmpeg_executable() -> str | None:
    return shutil.which("ffmpeg") or shutil.which("ffmpeg.exe")


def check_ffmpeg() -> bool:
    return ffmpeg_executable() is not None


def load_font(size: int, bold: bool = False):
    candidates = (
        [
            r"C:\Windows\Fonts\georgiab.ttf",
            r"C:\Windows\Fonts\arialbd.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        ]
        if bold
        else [
            r"C:\Windows\Fonts\georgia.ttf",
            r"C:\Windows\Fonts\arial.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        ]
    )
    for candidate in candidates:
        try:
            return ImageFont.truetype(candidate, size=size)
        except OSError:
            continue
    return ImageFont.load_default()


def _load_bytes(path_or_url: str | Path, timeout: int = 60) -> bytes:
    value = str(path_or_url)
    if value.startswith(("http://", "https://")):
        response = requests.get(value, timeout=timeout)
        response.raise_for_status()
        return response.content
    path = Path(value)
    if not path.is_file():
        raise FileNotFoundError(f"Media file not found: {path}")
    return path.read_bytes()


def download_file(url: str, dest_path: str | Path) -> Path:
    dest = Path(dest_path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    response = requests.get(str(url), timeout=120, stream=True)
    response.raise_for_status()
    with dest.open("wb") as handle:
        for chunk in response.iter_content(chunk_size=1024 * 1024):
            if chunk:
                handle.write(chunk)
    return dest


def fetch_image(url_or_path: str | Path):
    require_pillow()
    key = str(url_or_path)
    if key in _IMAGE_CACHE:
        return _IMAGE_CACHE[key].copy()
    data = _load_bytes(url_or_path, timeout=45)
    with Image.open(io.BytesIO(data)) as opened:
        image = opened.copy()
    _IMAGE_CACHE[key] = image.copy()
    return image


def prepare_photo(url_or_path: str | Path, target_width: int, target_height: int):
    require_pillow()
    image = fetch_image(url_or_path)
    image = ImageOps.exif_transpose(image).convert("RGBA")
    image = ImageEnhance.Contrast(image).enhance(1.03)
    source_ratio = image.width / image.height
    target_ratio = target_width / target_height
    if source_ratio > target_ratio:
        new_width, new_height = int(target_height * source_ratio), target_height
    else:
        new_width, new_height = target_width, int(target_width / source_ratio)
    image = image.resize((new_width, new_height), Image.Resampling.LANCZOS)
    left = (new_width - target_width) // 2
    top = (new_height - target_height) // 2
    return image.crop((left, top, left + target_width, top + target_height))


def plain_segments(text: str):
    return [(str(text).replace("[", "").replace("]", ""), False)]


def draw_mixed(
    draw: ImageDraw.ImageDraw,
    segments,
    font,
    x_start: int,
    y_start: int,
    max_width: int,
    default_color=DARK,
    accent_color=GOLD,
    line_spacing: int = 16,
) -> int:
    words = []
    for chunk, gold in segments:
        for word in chunk.split():
            words.append((word, gold))

    lines = []
    current = []
    for word, gold in words:
        test = current + [(word, gold)]
        test_text = " ".join(item[0] for item in test)
        if current and draw.textlength(test_text, font=font) > max_width:
            lines.append(current)
            current = [(word, gold)]
        else:
            current = test
    if current:
        lines.append(current)

    box = draw.textbbox((0, 0), "Ag", font=font)
    line_height = box[3] - box[1] + line_spacing
    y = y_start
    for line in lines:
        x = x_start
        for word, gold in line:
            draw.text((x, y), word, fill=accent_color if gold else default_color, font=font)
            x += draw.textlength(word + " ", font=font)
        y += line_height
    return y


def draw_plain(
    draw: ImageDraw.ImageDraw,
    text: str,
    font,
    x: int,
    y: int,
    max_width: int,
    color=CHARCOAL,
    line_spacing: int = 10,
) -> int:
    words = str(text).split()
    lines = []
    current = []
    for word in words:
        test = current + [word]
        if current and draw.textlength(" ".join(test), font=font) > max_width:
            lines.append(current)
            current = [word]
        else:
            current = test
    if current:
        lines.append(current)

    box = draw.textbbox((0, 0), "Ag", font=font)
    line_height = box[3] - box[1] + line_spacing
    for line in lines:
        draw.text((x, y), " ".join(line), fill=color, font=font)
        y += line_height
    return y


def draw_card_content(
    canvas: Image.Image,
    caption_text: str,
    body_text: str,
    image_side: str,
    logo_url: str,
    canvas_size: tuple[int, int],
    text_color=DARK,
    is_overlay: bool = False,
):
    require_pillow()
    width, height = canvas_size
    margin = int(width * 0.07)
    draw = ImageDraw.Draw(canvas)

    if image_side == "left":
        text_x = int(width * 0.54)
        text_max_width = width - text_x - margin
    else:
        text_x = margin
        text_max_width = int(width * 0.56)

    panel_x1 = text_x - int(margin * 0.3)
    panel_x2 = text_x + text_max_width + int(margin * 0.3)
    rule_pad = int(margin * 0.3)

    if is_overlay:
        panel = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        ImageDraw.Draw(panel).rectangle(
            [panel_x1, 0, panel_x2, height], fill=(15, 15, 15, 155)
        )
        canvas.alpha_composite(panel)
        draw = ImageDraw.Draw(canvas)

    logo_size_top = int(height * 0.058)
    top_bar_y = int(height * 0.034)
    brand_font = load_font(int(width * 0.021), bold=True)
    brand_box = draw.textbbox((0, 0), BRAND_NAME, font=brand_font)
    brand_height = brand_box[3] - brand_box[1]
    brand_y = top_bar_y + (logo_size_top - brand_height) // 2
    brand_color = WHITE if is_overlay else DARK
    draw.text((margin, brand_y), BRAND_NAME, fill=brand_color, font=brand_font)
    brand_width = int(draw.textlength(BRAND_NAME, font=brand_font))
    mid_y = (brand_y + brand_box[1] + brand_y + brand_box[3]) // 2
    rule_color = (200, 195, 180, 180) if is_overlay else LINE_COLOR

    rule_x1 = margin + brand_width + 14
    rule_x2 = width - margin - logo_size_top - 14
    if is_overlay:
        rule_x1 = max(rule_x1, panel_x1 + rule_pad)
        rule_x2 = min(rule_x2, panel_x2 - rule_pad)
    if rule_x2 > rule_x1:
        draw.line([(rule_x1, mid_y), (rule_x2, mid_y)], fill=rule_color, width=2)

    try:
        top_logo = fetch_image(logo_url).convert("RGBA")
        top_logo.thumbnail((logo_size_top, logo_size_top), Image.Resampling.LANCZOS)
        mask = Image.new("L", (logo_size_top, logo_size_top), 0)
        ImageDraw.Draw(mask).ellipse([(0, 0), (logo_size_top - 1, logo_size_top - 1)], fill=255)
        top_logo = top_logo.resize((logo_size_top, logo_size_top), Image.Resampling.LANCZOS)
        top_logo.putalpha(mask)
        canvas.paste(top_logo, (width - margin - logo_size_top, top_bar_y), top_logo)
        draw = ImageDraw.Draw(canvas)
    except Exception as exc:
        print(f"Logo could not be loaded: {exc}")

    quote_y = int(height * 0.14)

    def clean_part(value: str) -> str:
        return re.sub(r"^[.\s]+|[.\s]+$", "", value.strip())

    if "||" in caption_text:
        first, second = caption_text.split("||", 1)
        part_one = plain_segments(clean_part(first))
        part_two = plain_segments(clean_part(second))
    else:
        part_one = plain_segments(caption_text.strip())
        part_two = None

    quote_font = load_font(max(48, int(width * 0.065)), bold=True)
    first_color = WHITE if is_overlay else DARK
    second_color = WHITE if is_overlay else DARK
    y_after = draw_mixed(
        draw, part_one, quote_font, text_x, quote_y, text_max_width,
        default_color=first_color, accent_color=GOLD, line_spacing=int(width * 0.010),
    )
    if part_two:
        y_after += int(height * 0.022)
        y_after = draw_mixed(
            draw, part_two, quote_font, text_x, y_after, text_max_width,
            default_color=GOLD, accent_color=second_color, line_spacing=int(width * 0.010),
        )

    if body_text and body_text.strip():
        body_y = y_after + int(height * 0.030)
        body_font = load_font(int(width * 0.026))
        body_x = text_x + 18
        body_color = (220, 215, 200) if is_overlay else CHARCOAL
        body_end = draw_plain(
            draw, body_text.strip(), body_font, body_x, body_y,
            text_max_width - 18, color=body_color, line_spacing=8,
        )
        draw.line([(text_x, body_y - 4), (text_x, body_end)], fill=GOLD, width=3)

    bottom_y = int(height * 0.880)
    bottom_line_color = (200, 195, 180, 180) if is_overlay else LINE_COLOR
    line_x1, line_x2 = margin, width - margin
    if is_overlay:
        line_x1 = max(line_x1, panel_x1 + rule_pad)
        line_x2 = min(line_x2, panel_x2 - rule_pad)
    if line_x2 > line_x1:
        draw.line([(line_x1, bottom_y), (line_x2, bottom_y)], fill=bottom_line_color, width=1)

    icon_size = int(height * 0.036)
    icon_gap = int(width * 0.014)
    icon_y_top = bottom_y + int(height * 0.014) + int(height * 0.010)
    icon_x = margin
    for icon_url in SOCIAL_ICON_URLS:
        try:
            icon = fetch_image(icon_url).convert("RGBA")
            icon = icon.resize((icon_size, icon_size), Image.Resampling.LANCZOS)
            mask = Image.new("L", (icon_size, icon_size), 0)
            ImageDraw.Draw(mask).ellipse([(0, 0), (icon_size - 1, icon_size - 1)], fill=255)
            icon.putalpha(mask)
            canvas.paste(icon, (icon_x, icon_y_top), icon)
            icon_x += icon_size + icon_gap
        except Exception:
            continue

    draw = ImageDraw.Draw(canvas)
    handle_font = load_font(int(width * 0.022))
    handle_width = draw.textlength(PAGE_NAME, font=handle_font)
    draw.text(
        (width - margin - handle_width, icon_y_top + (icon_size - int(width * 0.022)) // 2),
        PAGE_NAME, fill=WHITE, font=handle_font,
    )
    return canvas


def _wrap_paper_text(draw, text: str, font, max_width: int) -> list[str]:
    words = str(text).split()
    lines: list[str] = []
    current: list[str] = []
    for word in words:
        candidate = current + [word]
        if current and draw.textlength(" ".join(candidate), font=font) > max_width:
            lines.append(" ".join(current))
            current = [word]
        else:
            current = candidate
    if current:
        lines.append(" ".join(current))
    return lines


def draw_paper_quote(
    canvas: Image.Image,
    title: str,
    description: str,
    canvas_size: tuple[int, int],
    *,
    is_overlay: bool = False,
) -> Image.Image:
    """Render a minimal paper quote: warm paper grain, serif copy, yellow title highlight."""
    require_pillow()
    width, height = canvas_size
    alpha = 190

    # Build a subtle natural paper grain without external image assets.
    noise = Image.effect_noise((width, height), 13).convert("L")
    grain = ImageOps.colorize(noise, black=(213, 207, 190), white=(250, 246, 232)).convert("RGBA")
    base = Image.new("RGBA", (width, height), (239, 234, 219, alpha))
    paper = Image.blend(base, grain, 0.40)
    paper.putalpha(alpha)
    canvas.alpha_composite(paper)
    draw = ImageDraw.Draw(canvas)

    # Restrained border like a printed sheet; no logo or social footer in this style.
    border = (48, 45, 38, 235)
    draw.rectangle((2, 2, width - 3, height - 3), outline=border, width=max(2, int(width * 0.006)))
    draw.rectangle((int(width * .025), int(height * .018), int(width * .975), int(height * .982)),
                   outline=(125, 116, 96, 65), width=max(1, int(width * .0015)))

    left = int(width * 0.105)
    max_width = int(width * 0.79)
    top = int(height * 0.135)
    title_size = int(width * 0.071)
    title_font = load_font(title_size, bold=True)
    title_lines = _wrap_paper_text(draw, title.strip(), title_font, max_width)
    while len(title_lines) > 3 and title_size > int(width * 0.052):
        title_size -= 3
        title_font = load_font(title_size, bold=True)
        title_lines = _wrap_paper_text(draw, title.strip(), title_font, max_width)

    title_box = draw.textbbox((0, 0), "Ag", font=title_font)
    title_line_h = title_box[3] - title_box[1] + int(title_size * 0.22)
    y = top
    highlight = (238, 231, 12, 255)
    for line_index, line in enumerate(title_lines):
        line_width = int(draw.textlength(line, font=title_font))
        box = draw.textbbox((0, 0), line, font=title_font)
        text_h = box[3] - box[1]
        pad_x = int(title_size * 0.16)
        pad_y = max(4, int(title_size * 0.05))
        # Slight per-line offset and varied height give the marker a hand-highlighted feel.
        offset = int((line_index % 2) * title_size * 0.025)
        rect = (
            left - pad_x + offset,
            y + max(5, int(title_size * 0.28)),
            left + line_width + pad_x + offset,
            y + text_h + pad_y,
        )
        draw.rounded_rectangle(rect, radius=max(4, int(title_size * .07)), fill=highlight)
        draw.text((left + offset, y - box[1]), line, fill=(20, 20, 18, 255), font=title_font)
        y += title_line_h

    y += int(height * 0.055)
    body_text = str(description or "").strip().replace("\\n", "\n")
    if "\n" in body_text:
        paragraphs = [part.strip() for part in re.split(r"\n\s*\n|\n", body_text) if part.strip()]
    else:
        paragraphs = [part.strip() for part in re.split(r"(?<=[.!?])\s+", body_text) if part.strip()]
    if not paragraphs and body_text:
        paragraphs = [body_text]

    body_size = int(width * 0.052)
    min_body_size = int(width * 0.038)
    available_bottom = int(height * 0.91)
    while body_size >= min_body_size:
        body_font = load_font(body_size, bold=True)
        body_box = draw.textbbox((0, 0), "Ag", font=body_font)
        body_line_h = body_box[3] - body_box[1] + int(body_size * 0.16)
        layout = []
        total_height = 0
        for paragraph in paragraphs:
            lines = _wrap_paper_text(draw, paragraph, body_font, max_width)
            layout.append(lines)
            total_height += len(lines) * body_line_h + int(body_size * 0.62)
        if y + total_height <= available_bottom or body_size == min_body_size:
            break
        body_size -= 2

    body_font = load_font(body_size, bold=True)
    body_box = draw.textbbox((0, 0), "Ag", font=body_font)
    body_line_h = body_box[3] - body_box[1] + int(body_size * 0.16)
    for paragraph in paragraphs:
        for line in _wrap_paper_text(draw, paragraph, body_font, max_width):
            box = draw.textbbox((0, 0), line, font=body_font)
            draw.text((left, y - box[1]), line, fill=(31, 30, 27, 255), font=body_font)
            y += body_line_h
        y += int(body_size * 0.62)

    return canvas


def build_jpg(
    caption_text: str,
    body_text: str,
    background: str | Path,
    image_side: str,
    logo_url: str,
    canvas_size: tuple[int, int],
    quality: int,
    destination: str | Path,
    quote_style: str = "two_part",
) -> Path:
    require_pillow()
    width, height = canvas_size
    # Both quote styles respect the selected still-image background.
    canvas = prepare_photo(background, width, height).convert("RGBA")
    if quote_style == "paper":
        canvas = draw_paper_quote(canvas, caption_text, body_text, canvas_size, is_overlay=True)
    else:
        # Keep the existing native 2-Part Quote rendering unchanged.
        canvas = draw_card_content(
            canvas, caption_text, body_text, image_side, logo_url, canvas_size, is_overlay=False
        )
    dest = Path(destination)
    dest.parent.mkdir(parents=True, exist_ok=True)
    canvas.convert("RGB").save(
        str(dest), "JPEG", quality=quality, optimize=True, progressive=True
    )
    return dest


def build_mp4(
    caption_text: str,
    body_text: str,
    background: str | Path,
    image_side: str,
    logo_url: str,
    canvas_size: tuple[int, int],
    audio_path: str | Path,
    duration: int,
    destination: str | Path,
    temp_dir: str | Path,
    quote_style: str = "two_part",
) -> Path:
    if not check_ffmpeg():
        raise RuntimeError("FFmpeg is not installed or is not available on PATH.")
    if not audio_path or not Path(audio_path).is_file():
        raise FileNotFoundError("A valid audio file is required for video-background cards.")

    # MP4s are capped at 45 seconds regardless of the requested UI value.
    # The background and music are looped only as needed, then both are cut
    # at the same duration. A short fade-out prevents an abrupt audio ending.
    duration = max(1, min(int(duration), 45))

    width, height = canvas_size
    temp_root = Path(temp_dir)
    temp_root.mkdir(parents=True, exist_ok=True)
    dest = Path(destination)
    dest.parent.mkdir(parents=True, exist_ok=True)

    overlay_path = temp_root / f"{dest.stem}_overlay.png"
    overlay = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    if quote_style == "paper":
        overlay = draw_paper_quote(overlay, caption_text, body_text, canvas_size, is_overlay=True)
    else:
        # Keep the existing native 2-Part Quote video overlay unchanged.
        overlay = draw_card_content(
            overlay, caption_text, body_text, image_side, logo_url, canvas_size, is_overlay=True
        )
    overlay.save(overlay_path, "PNG")

    background_value = str(background)
    if background_value.startswith(("http://", "https://")):
        suffix = Path(urlsplit(background_value).path).suffix or ".mp4"
        local_background = temp_root / f"background{suffix}"
        download_file(background_value, local_background)
    else:
        local_background = Path(background_value)
    if not local_background.is_file():
        raise FileNotFoundError(f"Video background not found: {local_background}")

    command = [
        ffmpeg_executable() or "ffmpeg", "-y",
        "-stream_loop", "-1", "-i", str(local_background),
        "-i", str(overlay_path),
        "-stream_loop", "-1", "-i", str(audio_path),
        "-filter_complex",
        f"[0:v]scale={width}:{height},setsar=1[bg];"
        f"[1:v]scale={width}:{height},setsar=1[ov];"
        f"[bg][ov]overlay=0:0[v]",
        "-map", "[v]", "-map", "2:a:0",
        "-af", f"apad,afade=t=out:st={max(0, duration - 2)}:d={min(2, duration)}",
        "-t", str(duration),
        "-c:v", "libx264", "-preset", "fast", "-crf", "23",
        "-c:a", "aac", "-b:a", "128k",
        "-pix_fmt", "yuv420p", "-movflags", "+faststart",
        str(dest),
    ]
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"FFmpeg failed: {result.stderr[-1200:]}")
    return dest


def render_card(
    title: str,
    description: str,
    background: str | Path,
    output_path: str | Path,
    *,
    audio_path: str | Path | None = None,
    size: str = "portrait",
    image_side: str = "right",
    duration: int = 8,
    quality: int = 95,
    logo_url: str = DEFAULT_LOGO_URL,
    quote_style: str = "two_part",
) -> Path:
    require_pillow()
    if size not in SIZES:
        raise ValueError(f"Unsupported canvas size: {size}")
    destination = Path(output_path)
    caption = (title or "").strip()
    body = (description or "").strip()
    quote_style = (quote_style or "two_part").strip().lower()
    if quote_style not in {"two_part", "paper"}:
        raise ValueError("Quote type must be 2-Part Quote or Paper Quotes.")
    if not caption:
        raise ValueError("Card title/caption cannot be blank.")
    if is_video(background):
        if not audio_path:
            raise ValueError("A video background requires an audio track.")
        return build_mp4(
            caption, body, background, image_side, logo_url, SIZES[size],
            audio_path, duration, destination, destination.parent / "_tmp",
            quote_style=quote_style,
        )
    return build_jpg(
        caption, body, background, image_side, logo_url, SIZES[size], quality, destination,
        quote_style=quote_style,
    )
