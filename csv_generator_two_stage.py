# BUILD: CAMPAIGN_LIBRARY_TITLE_REFERENCED_IMAGE_PROMPTS_2026-11-04
import csv
import hashlib
import json
import os
import re
import sqlite3
import threading
import unicodedata
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests
from dotenv import load_dotenv
from flask import Flask, Response, jsonify, render_template_string, request, send_file

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")  # load before paths are read
RUNTIME_DIR = Path(os.getenv("CONTENT_STUDIO_TMP", "/tmp/content-studio"))
TO_POST = Path(os.getenv("TOPOST_DIR", "").strip() or (RUNTIME_DIR / "ToPost"))
MANIFEST = RUNTIME_DIR / "image_urls.csv"
CACHE_FILE = RUNTIME_DIR / "ai_metadata_cache.json"
EXPORT_DIR = RUNTIME_DIR / "Exports" / "CSV_Drafts"
for _directory in (TO_POST, EXPORT_DIR):
    _directory.mkdir(parents=True, exist_ok=True)
load_dotenv(ROOT / ".env")

import upload_images
from pymongo import MongoClient, DESCENDING
from pymongo.errors import PyMongoError

_MONGO_CLIENT = None

def mongo_db():
    global _MONGO_CLIENT
    uri = os.getenv("MONGODB_URI", "").strip()
    if not uri:
        raise RuntimeError("MONGODB_URI is missing. Add your MongoDB connection string in Vercel Environment Variables.")
    if _MONGO_CLIENT is None:
        _MONGO_CLIENT = MongoClient(uri, serverSelectionTimeoutMS=8000, connectTimeoutMS=8000, appname="ContentStudio")
    return _MONGO_CLIENT[os.getenv("MONGODB_DATABASE", "content_studio").strip() or "content_studio"]

def all_campaign_drafts():
    return list(mongo_db().campaigns.find({}, {"_id": 0}).sort("updated_at", DESCENDING))

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
# Use OpenRouter's maintained free-model router first. Its available pool
# changes over time, so named free models below are additional fallbacks.
DEFAULT_MODEL = "openrouter/free"
DEFAULT_FALLBACK_MODELS = (
    "nvidia/nemotron-3-ultra-550b-a55b:free",
    "nvidia/nemotron-3.5-lightning:free",
    "dots-studio/dots-3-note-preview:free",
    "nvidia/nemotron-3.5-super-vl:free",
    "liquid/lfm-2.5-2.6b:free",
)
TIMEZONES = [
    "Africa/Nairobi",
    "America/New_York",
    "America/Chicago",
    "America/Denver",
    "America/Los_Angeles",
    "UTC",
]
ALLOWED_EXTENSIONS = set(upload_images.ALLOWED_EXTENSIONS)
UPLOAD_LOCK = threading.Lock()
DRAFT_ID_RE = re.compile(r"^[a-f0-9]{32}$")

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "") or "local-development-only-change-me"
app.config["MAX_CONTENT_LENGTH"] = 30 * 1024 * 1024


def ensure_campaign_tracking_column():
    # Publishing queue intentionally removed from this deployment.
    return None

ensure_campaign_tracking_column()

# -------------------------------------------------------------------
# FILES, MANIFEST AND CAMPAIGN DRAFTS
# -------------------------------------------------------------------

def read_manifest():
    try:
        doc = mongo_db().settings.find_one({"_id": "media_manifest"}) or {}
        return doc.get("files", {})
    except Exception as exc:
        print("Media manifest unavailable:", exc)
        return {}


def save_manifest(uploaded):
    mongo_db().settings.update_one(
        {"_id": "media_manifest"},
        {"$set": {"files": uploaded, "updated_at": datetime.now(timezone.utc).isoformat()}},
        upsert=True,
    )


def make_image_generation_prompt(title):
    """Build an unnumbered, niche-neutral three-line prompt from the exact Pin title, ending in a full stop."""
    clean_title = " ".join(str(title or "").split())
    if not clean_title:
        return ""
    # Always end the title line with exactly one full stop.
    clean_title = re.sub(r"[.!?]+$", "", clean_title).rstrip() + "."
    return (
        f"{clean_title}\n"
        "Create a visually engaging Pinterest pin that clearly communicates the idea and message of the title above.\n"
        "[Do not include any website links or URLs in the image]"
    )


def build_image_generation_prompts(rows):
    """Return one prompt per paragraph, with a blank line between prompts."""
    prompts = [make_image_generation_prompt(row.get("title", "")) for row in rows]
    return "\n\n".join(prompt for prompt in prompts if prompt)


def draft_paths(draft_id):
    if not isinstance(draft_id, str) or not DRAFT_ID_RE.fullmatch(draft_id):
        raise ValueError("Invalid campaign draft ID.")
    return {
        "json": EXPORT_DIR / f"{draft_id}.json",
        "content_csv": EXPORT_DIR / f"{draft_id}_content.csv",
        "final_csv": EXPORT_DIR / f"{draft_id}_final.csv",
    }


def save_draft(draft):
    draft.setdefault("created_at", datetime.now(timezone.utc).isoformat())
    draft["updated_at"] = datetime.now(timezone.utc).isoformat()
    draft.setdefault("archived", False)
    mongo_db().campaigns.update_one(
        {"draft_id": draft["draft_id"]},
        {"$set": draft},
        upsert=True,
    )


def load_draft(draft_id):
    if not isinstance(draft_id, str) or not DRAFT_ID_RE.fullmatch(draft_id):
        raise ValueError("Invalid campaign draft ID.")
    draft = mongo_db().campaigns.find_one({"draft_id": draft_id}, {"_id": 0})
    return draft if isinstance(draft, dict) else None


def campaign_status(draft, queue_counts=None):
    if draft.get("archived"):
        return "archived"
    if not draft.get("final_ready"):
        return "draft"
    if not draft.get("imported"):
        return "ready"

    # New imports are tied to a campaign_id, allowing exact publishing progress.
    if queue_counts:
        total = sum(queue_counts.values())
        published = queue_counts.get("published", 0)
        if total and published == total:
            return "completed"
        if queue_counts.get("failed", 0):
            return "failed"
        return "scheduled"

    # Older imports predate campaign tracking. Keep them in history as scheduled.
    return "scheduled"


def get_campaign_library():
    campaigns = []
    for draft in all_campaign_drafts():
        if draft.get("content_type") == "social":
            continue
        campaign_id = draft.get("draft_id", "")
        if not DRAFT_ID_RE.fullmatch(str(campaign_id)):
            continue
        campaigns.append({
            "draft_id": campaign_id,
            "topic": draft.get("topic") or "Untitled campaign",
            "niche": draft.get("niche", ""),
            "board": draft.get("board", ""),
            "row_count": len(draft.get("rows", [])),
            "image_count": len(draft.get("final_rows", [])) if draft.get("final_ready") else 0,
            "created_at": draft.get("created_at", ""),
            "updated_at": draft.get("updated_at", draft.get("created_at", "")),
            "status": "archived" if draft.get("archived") else ("ready" if draft.get("final_ready") else "draft"),
            "imported": False,
            "imported_count": 0,
            "skipped_count": 0,
            "queue_counts": {},
            "archived": bool(draft.get("archived")),
            "has_content_csv": bool(draft.get("rows")),
            "has_final_csv": bool(draft.get("final_ready") and draft.get("final_rows")),
        })
    return sorted(campaigns, key=lambda item: item.get("updated_at", ""), reverse=True)


def write_csv(path, fieldnames, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(path.name + ".tmp")
    with temp_path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    os.replace(temp_path, path)


def format_publish_date(schedule_value, timezone_name):
    """Format a local publish time as ISO 8601 with its UTC offset."""
    try:
        value = datetime.fromisoformat(str(schedule_value))
        zone = ZoneInfo(str(timezone_name))
        if value.tzinfo is None:
            value = value.replace(tzinfo=zone)
        else:
            value = value.astimezone(zone)
        return value.isoformat(timespec="seconds")
    except (TypeError, ValueError, ZoneInfoNotFoundError):
        # Keep the original date visible rather than losing a row if an old
        # campaign contains a legacy or incomplete timezone value.
        return str(schedule_value or "")


def write_content_csv(draft):
    """Write the content-only CSV using the exact requested eight columns.

    Media URL and Thumbnail are intentionally blank until images are linked.
    """
    fields = [
        "Title",
        "Media URL",
        "Pinterest board",
        "Thumbnail",
        "Description",
        "Link",
        "Publish date",
        "Keywords",
    ]
    rows = []
    for item in draft["rows"]:
        rows.append({
            "Title": item.get("title", ""),
            "Media URL": "",
            "Pinterest board": draft.get("board", ""),
            "Thumbnail": "",
            "Description": item.get("description", ""),
            "Link": make_unique_destination_url(
                draft.get("destination_url", ""), item.get("title", "")
            ),
            "Publish date": format_publish_date(
                item.get("scheduled_at", ""), draft.get("timezone", "UTC")
            ),
            "Keywords": "; ".join(item.get("keywords", [])),
        })
    write_csv(draft_paths(draft["draft_id"])["content_csv"], fields, rows)


def write_final_csv(draft):
    """Write the completed CSV with the exact same eight requested columns."""
    fields = [
        "Title",
        "Media URL",
        "Pinterest board",
        "Thumbnail",
        "Description",
        "Link",
        "Publish date",
        "Keywords",
    ]
    rows = []
    for item in draft["final_rows"]:
        rows.append({
            "Title": item.get("title", ""),
            "Media URL": item.get("media_url", ""),
            "Pinterest board": draft.get("board", ""),
            "Thumbnail": "",
            "Description": item.get("description", ""),
            "Link": make_unique_destination_url(
                draft.get("destination_url", "") or item.get("destination_url", ""),
                item.get("title", ""),
            ),
            "Publish date": format_publish_date(
                item.get("scheduled_at", ""), item.get("timezone", draft.get("timezone", "UTC"))
            ),
            "Keywords": "; ".join(item.get("keywords", [])),
        })
    write_csv(draft_paths(draft["draft_id"])["final_csv"], fields, rows)


def get_local_images():
    TO_POST.mkdir(exist_ok=True)
    uploaded = read_manifest()
    files = sorted(
        [
            path for path in TO_POST.iterdir()
            if path.is_file() and path.suffix.lower() in ALLOWED_EXTENSIONS
        ],
        key=lambda path: path.name.casefold(),
    )
    return [
        {
            "filename": path.name,
            "hosted": path.name in uploaded,
            "media_url": uploaded.get(path.name, ""),
            "size": path.stat().st_size,
        }
        for path in files
    ]


# -------------------------------------------------------------------
# OPENROUTER CONTENT GENERATION
# -------------------------------------------------------------------

def load_cache():
    try:
        data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_cache(cache):
    temp_path = CACHE_FILE.with_name(CACHE_FILE.name + ".tmp")
    temp_path.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp_path, CACHE_FILE)


def extract_json(content):
    if isinstance(content, list):
        content = "".join(
            part.get("text", "") for part in content if isinstance(part, dict)
        )
    if not isinstance(content, str):
        raise ValueError("OpenRouter returned an empty response.")
    content = content.strip()
    content = re.sub(r"^```(?:json)?\s*", "", content, flags=re.I)
    content = re.sub(r"\s*```$", "", content)
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        start, end = content.find("{"), content.rfind("}")
        if start >= 0 and end > start:
            return json.loads(content[start:end + 1])
        raise ValueError("OpenRouter did not return valid JSON.")


class OpenRouterQuotaError(RuntimeError):
    """OpenRouter account-level free request limit; other model calls won't help."""


def _openrouter_request(model, api_key, system_prompt, user_prompt, row_count):
    """Call one OpenRouter model and parse its JSON-only text response.

    Do not force response_format: some current free models do not implement
    that parameter. The prompt requests JSON and extract_json validates it.
    """
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "http://localhost:5001",
        "X-Title": "Pinterest CSV Generator",
    }
    payload = {
        "model": model,
        "temperature": 0.45,
        "max_tokens": min(12000, max(8000, row_count * 600)),
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    }

    try:
        response = requests.post(
            OPENROUTER_URL,
            headers=headers,
            json=payload,
            timeout=(20, 180),
        )
    except requests.RequestException as exc:
        raise RuntimeError(f"Connection to OpenRouter failed: {exc}") from exc

    try:
        body = response.json()
    except ValueError:
        body = {}

    api_error = body.get("error") if isinstance(body, dict) else None
    if not response.ok or api_error:
        metadata = api_error.get("metadata", {}) if isinstance(api_error, dict) else {}
        if isinstance(api_error, dict):
            detail = str(api_error.get("message") or api_error)
            error_code = api_error.get("code") or response.status_code
            provider_name = metadata.get("provider_name") if isinstance(metadata, dict) else None
            provider_code = metadata.get("provider_code") if isinstance(metadata, dict) else None
            if provider_name:
                detail += f" (provider: {provider_name}"
                if provider_code:
                    detail += f", provider code: {provider_code}"
                detail += ")"
        else:
            detail = response.text[:700].replace("\n", " ")
            error_code = response.status_code

        retry_after = response.headers.get("Retry-After")
        retry_note = f" Retry-After: {retry_after}s." if retry_after else ""
        if int(error_code or 0) in (401, 402):
            raise PermissionError(
                f"OpenRouter authentication or credit error (HTTP {error_code}): {detail}"
            )
        # A provider-specific 429 can be avoided by trying another model.
        # An OpenRouter-wide 429 is an account quota limit, so extra model
        # calls would waste the daily allowance and cannot solve it.
        provider_limited = bool(
            isinstance(metadata, dict)
            and (metadata.get("provider_name") or metadata.get("provider_code"))
        ) or "provider returned error" in detail.lower()
        if int(error_code or 0) == 429 and not provider_limited:
            raise OpenRouterQuotaError(
                f"OpenRouter free-request limit reached (HTTP 429): {detail}.{retry_note} "
                "Please wait until the indicated reset time before trying again."
            )
        raise RuntimeError(
            f"OpenRouter model {model} returned HTTP {error_code}: {detail}.{retry_note}"
        )

    choices = body.get("choices") if isinstance(body, dict) else None
    if not isinstance(choices, list) or not choices:
        raise RuntimeError(
            f"Model {model} returned no completion choices."
        )

    choice = choices[0] if isinstance(choices[0], dict) else {}
    message = choice.get("message") or {}
    if not isinstance(message, dict):
        message = {}

    content = message.get("content")
    if content is None or (isinstance(content, str) and not content.strip()):
        finish_reason = choice.get("finish_reason") or "not supplied"
        usage = body.get("usage") if isinstance(body, dict) else None
        completion_details = (
            usage.get("completion_tokens_details", {})
            if isinstance(usage, dict) else {}
        )
        reasoning_tokens = (
            completion_details.get("reasoning_tokens")
            if isinstance(completion_details, dict) else None
        )
        raise RuntimeError(
            f"Model {model} returned an empty message (finish_reason={finish_reason}, "
            f"reasoning_tokens={reasoning_tokens})."
        )

    try:
        result = extract_json(content)
    except (ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Model {model} did not return valid JSON: {exc}") from exc

    content_rows = result.get("rows") if isinstance(result, dict) else None
    if not isinstance(content_rows, list) or len(content_rows) != row_count:
        got = len(content_rows) if isinstance(content_rows, list) else 0
        raise RuntimeError(
            f"Model {model} returned {got} rows instead of {row_count}."
        )

    return content_rows


def generate_campaign_content(row_count, topic, niche, cta, mode="pins"):
    api_key = os.getenv("OPENROUTER_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("OpenRouter API key is missing. Add OPENROUTER_API_KEY to .env.")

    model = os.getenv("OPENROUTER_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL
    fallback_setting = os.getenv("OPENROUTER_FALLBACK_MODELS", "").strip()
    fallback_models = (
        [item.strip() for item in fallback_setting.split(",") if item.strip()]
        if fallback_setting else list(DEFAULT_FALLBACK_MODELS)
    )
    models_to_try = []
    for candidate in [model, *fallback_models]:
        if candidate and candidate not in models_to_try:
            models_to_try.append(candidate)

    cache_material = json.dumps(
        ["campaign-csv-v5", mode, models_to_try, row_count, topic, niche, cta],
        ensure_ascii=False,
    )
    cache_id = hashlib.sha256(cache_material.encode("utf-8")).hexdigest()
    cache = load_cache()
    cached = cache.get("campaigns", {}).get(cache_id)
    if isinstance(cached, list) and len(cached) == row_count:
        return cached, True

    system_prompt = (
        "You are an experienced Pinterest SEO copywriter. Create useful, natural, "
        "distinct Pinterest Pin titles and descriptions. The topic is the PRIMARY SEARCH KEYWORD "
        "and the niche describes the audience and voice. Build every title and description around the primary keyword naturally; do not keyword-stuff. "
        "Do not invent research, search-volume figures, statistics, endorsements, "
        "guarantees, product ingredients, or factual claims. The images will be "
        "attached later, so do not claim to see or describe any image. Return valid JSON only."
    )
    user_prompt = f"""Create exactly {row_count} DIFFERENT Pinterest content rows.

PRIMARY KEYWORD: {topic}
NICHE & AUDIENCE: {niche}
OPTIONAL CTA / INSTRUCTION: {cta or 'Use a natural, low-pressure invitation only when appropriate.'}

For every row provide:
- title: search-friendly Pinterest title, maximum 100 characters (aim for 40-70). Put the primary keyword, or a natural close variant, within the first 40 characters. Mix formats across rows: numbered list, how-to, myths vs facts, question, benefit-led. Each title must describe one clear, visual idea, because the image prompt is generated from the title alone.
- description: useful, natural Pinterest description, maximum 500 characters. Use the primary keyword naturally in the first sentence, write for the stated audience, and include a related keyword or two without repeating the same phrase.
- keywords: 4 to 6 relevant keyword phrases (suggestions, not verified search-volume data).

Make each row meaningfully different in angle, wording, and keyword emphasis. Avoid
repeating the same title or opening sentence. Do not include an image filename, image
URL, board, date, or schedule. Do not include hashtags unless they read naturally.
Do not make medical, financial, or other unsupported promises.

Return ONLY this JSON object, with exactly {row_count} objects in rows:
{{"rows":[{{"title":"...","description":"...","keywords":["...","..."]}}]}}
"""

    if mode == "social":
        # Social posts keep the original, caption-driven wording.
        system_prompt = system_prompt.replace(
            "The topic is the PRIMARY SEARCH KEYWORD and the niche describes the audience and voice. Build every title and description around the primary keyword naturally; do not keyword-stuff. ",
            "Use the supplied topic and niche to choose relevant search phrases naturally; do not keyword-stuff. ")
        user_prompt = user_prompt.replace(f"PRIMARY KEYWORD: {topic}", f"CAMPAIGN TOPIC: {topic}")
        user_prompt = user_prompt.replace(f"NICHE & AUDIENCE: {niche}", f"NICHE: {niche}")
        user_prompt = user_prompt.replace(
            "- title: search-friendly Pinterest title, maximum 100 characters (aim for 40-70). Put the primary keyword, or a natural close variant, within the first 40 characters. Mix formats across rows: numbered list, how-to, myths vs facts, question, benefit-led. Each title must describe one clear, visual idea, because the image prompt is generated from the title alone.",
            "- title: engaging, search-friendly Pinterest title, maximum 100 characters.")
        user_prompt = user_prompt.replace(
            "- description: useful, natural Pinterest description, maximum 500 characters. Use the primary keyword naturally in the first sentence, write for the stated audience, and include a related keyword or two without repeating the same phrase.",
            "- description: useful, natural Pinterest description, maximum 500 characters.")

    content_rows = None
    model_errors = []
    for active_model in models_to_try:
        try:
            content_rows = _openrouter_request(
                active_model, api_key, system_prompt, user_prompt, row_count
            )
            break
        except PermissionError:
            # Invalid API key or insufficient account credits will affect
            # every model, so do not hide the real cause with another call.
            raise
        except OpenRouterQuotaError as exc:
            # This is an OpenRouter-wide quota, not an individual provider
            # failure. Trying more free model slugs would waste requests.
            model_errors.append(f"{active_model}: {exc}")
            break
        except Exception as exc:
            model_errors.append(f"{active_model}: {exc}")
            print(f"OpenRouter generation failed on {active_model}: {exc}")

    if content_rows is None:
        detail = " | ".join(model_errors)
        raise RuntimeError(
            "All configured free OpenRouter routes failed to create usable content. "
            "Try again later if providers are busy. Details: " + detail
        )

    cleaned = []
    titles_seen = set()
    for index, item in enumerate(content_rows, start=1):
        if not isinstance(item, dict):
            raise RuntimeError(f"AI content row {index} is not valid.")
        title = str(item.get("title", "")).strip()
        description = str(item.get("description", "")).strip()
        keywords = item.get("keywords", [])
        if not isinstance(keywords, list):
            keywords = []
        keywords = [str(value).strip() for value in keywords if str(value).strip()][:6]
        if len(title) > 100:
            title = title[:97].rsplit(" ", 1)[0].rstrip(" ,;:") + "..."
        if len(description) > 500:
            description = description[:497].rsplit(" ", 1)[0].rstrip(" ,;:") + "..."
        if not title or not description:
            raise RuntimeError(f"AI content row {index} is missing a title or description.")
        normalized_title = title.casefold()
        if normalized_title in titles_seen:
            raise RuntimeError("The AI returned duplicate titles. Please generate again.")
        titles_seen.add(normalized_title)
        cleaned.append({"title": title, "description": description, "keywords": keywords})

    cache.setdefault("campaigns", {})[cache_id] = cleaned
    save_cache(cache)
    return cleaned, False


# -------------------------------------------------------------------
# SCHEDULING AND URL HELPERS
# -------------------------------------------------------------------

def create_schedule(start_value, interval_hours, row_count, timezone_name):
    if timezone_name not in TIMEZONES:
        raise ValueError("Choose a supported timezone.")
    try:
        start_naive = datetime.fromisoformat(start_value)
        zone = ZoneInfo(timezone_name)
    except (ValueError, ZoneInfoNotFoundError) as exc:
        raise ValueError("Enter a valid start date/time and timezone.") from exc
    if start_naive.tzinfo is not None:
        start_local = start_naive.astimezone(zone)
    else:
        start_local = start_naive.replace(tzinfo=zone)
        round_trip = start_local.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None)
        if round_trip != start_naive:
            raise ValueError("The selected start time does not exist due to a daylight-saving clock change.")
    start_utc = start_local.astimezone(timezone.utc)
    schedules = []
    for index in range(row_count):
        planned_utc = start_utc + timedelta(hours=interval_hours * index)
        planned_local = planned_utc.astimezone(zone)
        schedules.append(planned_local.strftime("%Y-%m-%d %H:%M:%S"))
    return schedules


def local_and_utc(schedule_value, timezone_name):
    if timezone_name not in TIMEZONES:
        raise ValueError("Unsupported timezone.")
    try:
        naive = datetime.fromisoformat(schedule_value)
        zone = ZoneInfo(timezone_name)
    except (ValueError, ZoneInfoNotFoundError) as exc:
        raise ValueError("Invalid scheduled date/time or timezone.") from exc
    if naive.tzinfo is not None:
        local = naive.astimezone(zone)
    else:
        local = naive.replace(tzinfo=zone)
        round_trip = local.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None)
        if round_trip != naive:
            raise ValueError("That local time does not exist because of a daylight-saving clock change.")
    return (
        local.strftime("%Y-%m-%d %H:%M:%S"),
        local.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
    )


def title_to_slug(title):
    """Convert a Pin title into a compact lowercase, hyphen-separated slug."""
    title = " ".join(str(title or "").split())
    if not title:
        return ""

    # Normalize accented characters (e.g. café -> cafe), lowercase, and
    # replace punctuation/whitespace runs with a single hyphen.
    ascii_title = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_title.lower()).strip("-")

    # Keep a deterministic value for titles written entirely in non-Latin
    # characters so the URL parameter is never silently empty.
    if not slug:
        slug = "pin-" + hashlib.sha256(title.encode("utf-8")).hexdigest()[:10]
    return slug


def make_unique_destination_url(base_url, pin_title):
    """Add a readable ?pin=title-slug parameter and preserve other URL data."""
    base_url = str(base_url or "").strip()
    if not base_url:
        return ""

    pin_slug = title_to_slug(pin_title)
    if not pin_slug:
        return base_url

    parts = urlsplit(base_url)
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key.casefold() != "pin"
    ]
    query.append(("pin", pin_slug))
    return urlunsplit((
        parts.scheme,
        parts.netloc,
        parts.path,
        urlencode(query, doseq=True, quote_via=quote),
        parts.fragment,
    ))


# -------------------------------------------------------------------
# UI
# -------------------------------------------------------------------

HTML = r'''<!doctype html>
<html lang="en" data-platform="pinterest">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Pinterest CSV Generator</title>
<style>/* Visual styles live in static/app-theme.css */</style><link rel="stylesheet" href="{{ url_for('static', filename='app-theme.css') }}">
</head>
<body>
<header>
  <div><div class="brand"><i>●</i> Pinterest CSV Generator</div><div class="sub">Create campaign content first. Add R2 image URLs when your images are ready.</div></div>
  <span class="pill" id="connection">Checking OpenRouter…</span>
</header>
<main>
  <section id="libraryView">
    <section class="hero">
      <div><h1>Campaign Library</h1><p>Your campaigns stay saved here. Open a draft, continue adding images, or revisit a completed campaign.</p></div>
      <button class="primary" id="openCreateLibrary">+ New Campaign</button>
    </section>
    <div class="libbar" id="libraryControls">
      <div class="library-filters" id="libraryFilters">
        <button class="filter-chip active" data-filter="all">All campaigns</button>
        <button class="filter-chip" data-filter="draft">Drafts</button>
        <button class="filter-chip" data-filter="ready">Ready</button>
        <button class="filter-chip" data-filter="scheduled">Scheduled</button>
        <button class="filter-chip" data-filter="completed">Completed</button>
        <button class="filter-chip" data-filter="failed">Failed</button>
        <button class="filter-chip" data-filter="archived">Archived</button>
      </div>
      <input class="library-search" id="campaignSearch" type="search" placeholder="Search campaigns…" aria-label="Search campaigns">
    </div>
    <div class="campaign-list" id="campaignList"><div class="library-empty">Loading saved campaigns…</div></div>
  </section>

  <div id="workspaceView" class="hidden">
    <section class="hero">
      <div><h1 id="workspaceTitle">Campaign workspace</h1><p id="workspaceSubtitle">Continue working on this campaign.</p></div>
      <div class="actions"><button class="outline" id="backToLibrary">← Campaign Library</button><button class="primary" id="openCreate">+ New Campaign</button></div>
    </section>

  <nav class="workflow-nav" aria-label="CSV workflow">
    <button type="button" class="nav-step active" id="navContent">
      <span class="nav-index">1</span><span class="nav-copy"><strong>Content</strong><small id="navContentMeta">Create campaign</small></span>
    </button>
    <span class="toolbar-spacer"></span>
    <button type="button" class="nav-step" id="navImages" disabled>
      <span class="nav-index">2</span><span class="nav-copy"><strong>Images</strong><small id="navImagesMeta">Add R2 URLs</small></span>
    </button>
    <span class="toolbar-spacer"></span>
    <button type="button" class="nav-step" id="navExport" disabled>
      <span class="nav-index">3</span><span class="nav-copy"><strong>Export</strong><small id="navExportMeta">Download / import</small></span>
    </button>
  </nav>

  <div id="message" class="status" role="status"></div>

  <section class="card hidden" id="contentStage">
    <div class="step"><div class="stepnum">1</div><div><h2>Content CSV generated</h2><p>This first CSV contains campaign text and schedule details only. It has no filenames or media URLs.</p></div></div>
    <div class="statline"><span class="count" id="draftRows">0 rows</span><span class="count" id="draftTopic"></span><span class="count" id="draftTimezone"></span></div>
    <div class="actions" style="margin-top:11px"><a class="download-link" id="contentDownload" href="#"><button class="secondary">Download content CSV</button></a><button class="outline" id="showImages">Continue to images →</button></div>
    <details class="preview-details" id="contentPreview">
      <summary><span id="contentPreviewLabel">Preview generated content</span><span class="summary-hint">Expand only when you need to review or edit</span></summary>
      <div class="tablewrap"><table class="table"><thead><tr><th>Row / Date</th><th>Title</th><th>Description</th><th>Keywords</th></tr></thead><tbody id="contentRows"></tbody></table></div>
    </details>
    <div class="prompt-tools" id="imagePromptTools">
      <div class="prompt-tools-head">
        <div><strong>Image generation prompts</strong><div class="small muted" id="imagePromptMeta">One prompt per title, ready for a bulk image generator.</div></div>
        <div class="actions prompt-actions">
          <button type="button" class="secondary" id="copyImagePrompts">Copy all prompts</button>
          <a class="download-link" id="downloadImagePrompts" href="#"><button type="button" class="outline">Download .txt</button></a>
        </div>
      </div>
      <details class="preview-details" id="imagePromptsPreview">
        <summary><span>Preview image prompts</span><span class="summary-hint">A blank line separates each prompt</span></summary>
        <textarea id="imagePromptsText" class="prompt-textarea" readonly spellcheck="false"></textarea>
      </details>
    </div>
  </section>

  <section class="card hidden" id="imageStage">
    <div class="step"><div class="stepnum">2</div><div><h2>Add images and insert their R2 URLs</h2><p>Copy your still images into the ToPost folder, then scan. Videos are ignored for now.</p></div></div>
    <div class="row" style="flex-wrap:wrap"><div><strong id="imageInstruction">Select images for this campaign.</strong><div class="small muted">Files are listed alphabetically. The selected order maps to CSV rows from top to bottom.</div></div><button class="secondary" id="scanImages">Scan ToPost folder</button></div>
    <div class="statline"><span class="count" id="availableCount">No scan yet</span><span class="count" id="selectedCount">0 selected</span></div>
    <details class="preview-details" id="imagePreview">
      <summary><span id="imagePreviewLabel">Choose images from ToPost</span><span class="summary-hint">The selection order matches the CSV rows</span></summary>
      <div id="imageList" class="image-list" style="padding:10px 12px"><div class="empty">Scan ToPost when your campaign images are ready.</div></div>
    </details>
    <div class="actions" style="margin-top:12px"><button class="success" id="insertUrls" disabled>Insert URLs & complete CSV</button></div>
    <div class="small muted" style="margin-top:8px">Only selected images are uploaded. Existing R2 URLs are reused; new images are uploaded once and added to image_urls.csv.</div>
  </section>

  <section class="card hidden" id="finalStage">
    <div class="step"><div class="stepnum">3</div><div><h2>Final CSV is ready</h2><p>Your selected images have been matched to the campaign rows and their public R2 URLs inserted.</p></div></div>
    <div class="statline"><span class="count" id="finalCount">0 images linked</span><span class="count">Media URLs verified during R2 upload</span></div>
    <div class="actions" style="margin-top:14px"><a class="download-link" id="finalDownload" href="#"><button class="primary">Download final CSV</button></a><button class="success" id="importScheduler" disabled>Publishing disabled</button></div>
    <div class="small muted" id="importMessage" style="margin-top:8px">Publishing is disabled. Download your CSV instead.</div>
    <details class="preview-details" id="finalPreview">
      <summary><span id="finalPreviewLabel">Preview completed CSV</span><span class="summary-hint">Hidden to keep the workspace compact</span></summary>
      <div class="tablewrap"><table class="table"><thead><tr><th>Image</th><th>Media URL</th><th>Title</th><th>Scheduled time</th></tr></thead><tbody id="finalRows"></tbody></table></div>
    </details>
  </section>

  <section class="card" id="emptyState"><div class="empty">No campaign CSV yet. Select <strong>Create CSV</strong> to begin.</div></section>
  </div><!-- /workspaceView -->
</main>

<div class="modal-backdrop" id="createModal" aria-hidden="true">
  <form class="modal" id="createForm">
    <div class="modal-head"><div><h2>Create campaign CSV</h2><div class="small muted">Set your content and publishing schedule.</div></div><button class="close" type="button" id="closeModal" aria-label="Close">×</button></div>
    <div class="modal-body">
      <div class="grid2">
        <div class="field"><label for="rowCount">Number of rows</label><input id="rowCount" type="number" min="1" max="50" value="5" required></div>
        <div class="field"><label for="interval">Interval (hours)</label><input id="interval" type="number" min="0.25" max="720" step="0.25" value="3" required></div>
      </div>
      <div class="field"><label for="topic">Primary keyword (what people search on Pinterest)</label><input id="topic" maxlength="180" placeholder="e.g. signs of emotional intelligence in a relationship" required></div>
      <div class="field"><label for="niche">Niche &amp; audience</label><input id="niche" maxlength="180" placeholder="e.g. Relationship psychology · women 25-40" required></div>
            <div class="field"><label for="boardName">Pinterest board name</label><input id="boardName" maxlength="180" placeholder="e.g. Relationship Psychology" required><div class="small muted" style="margin-top:5px">Enter any board name. If it doesn't exist, it will be created when you import the CSV.</div></div>
      <div class="field"><label for="destination">Destination URL (optional)</label><input id="destination" type="url" placeholder="https://example.com/your-page"></div>
      <div class="field"><label for="cta">Optional CTA / extra writing instructions</label><input id="cta" maxlength="300" placeholder="e.g. Encourage readers to explore the full guide"></div>
      <div class="grid2">
        <div class="field"><label for="startAt">Start date & time</label><input id="startAt" type="datetime-local" required></div>
        <div class="field"><label for="timezone">Timezone</label><select id="timezone"><option>Africa/Nairobi</option><option>America/New_York</option><option>America/Chicago</option><option>America/Denver</option><option>America/Los_Angeles</option><option>UTC</option></select></div>
      </div>
      <div class="small muted">Publishing times are calculated by Python using the selected timezone. Each next row is scheduled at the interval you enter.</div>
    </div>
    <div class="modal-foot"><button class="secondary" type="button" id="cancelCreate">Cancel</button><button class="primary" type="submit" id="generateBtn">Generate CSV</button></div>
  </form>
</div>

<script>
const $=id=>document.getElementById(id);
let activeDraft=null;
let availableImages=[];
let boards=[];
let campaignFilter='all';
let campaignItems=[];

function escapeHtml(value){return String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
function formatCampaignDate(value){
  if(!value)return 'Date unavailable';
  try{return new Date(value).toLocaleString([], {year:'numeric',month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'});}
  catch(error){return value;}
}
function showLibrary(){
  $('libraryView').classList.remove('hidden');
  $('workspaceView').classList.add('hidden');
  activeDraft=null;
  loadCampaigns();
}
function showWorkspace(){
  $('libraryView').classList.add('hidden');
  $('workspaceView').classList.remove('hidden');
}
function renderCampaignLibrary(){
  const query=$('campaignSearch').value.trim().toLowerCase();
  const filtered=campaignItems.filter(item=>{
    const matchesFilter=campaignFilter==='all'||item.status===campaignFilter;
    const haystack=[item.topic,item.niche,item.board,item.status].join(' ').toLowerCase();
    return matchesFilter&&(!query||haystack.includes(query));
  });
  if(!filtered.length){
    $('campaignList').innerHTML=`<div class="library-empty">${campaignItems.length?'No campaigns match this filter.':'No saved campaigns yet. Select “New Campaign” to create your first one.'}</div>`;
    return;
  }
  $('campaignList').innerHTML=filtered.map(item=>{
    const created=formatCampaignDate(item.created_at);
    const details=[`${item.row_count} posts`,item.board||'No board',`Updated ${formatCampaignDate(item.updated_at)}`].join(' · ');
    let progress=item.status==='draft'?'Content generated · waiting for images':item.status==='ready'?'Images linked · CSV ready to download':item.status==='scheduled'?'Imported into the Pinterest publishing queue':item.status==='completed'?'All tracked Pins have been published':item.status==='failed'?'One or more Pins need attention':'Archived campaign';
    if(item.imported_count)progress+=` · ${item.imported_count} imported`;
    if(item.skipped_count)progress+=` · ${item.skipped_count} skipped`;
    const download=item.has_final_csv?`<a href="/api/drafts/${item.draft_id}/final.csv"><button class="secondary">Final CSV</button></a>`:item.has_content_csv?`<a href="/api/drafts/${item.draft_id}/content.csv"><button class="secondary">Content CSV</button></a>`:'';
    const archiveButton=item.archived?`<button class="outline" data-restore="${item.draft_id}">Restore</button>`:`<button class="outline" data-archive="${item.draft_id}">Archive</button>`;
    return `<article class="campaign-card">
      <div class="campaign-header">
        <div>
          <div class="campaign-title">${escapeHtml(item.topic)}</div>
          <div class="campaign-meta">${escapeHtml(item.niche)} · ${escapeHtml(details)}</div>
        </div>
        <span class="status-badge ${escapeHtml(item.status)}">${escapeHtml(item.status)}</span>
      </div>
      <div class="campaign-summary">${escapeHtml(progress)} · Created ${escapeHtml(created)}</div>
      <div class="campaign-actions"><button class="primary" data-open="${item.draft_id}">Open</button>${download}${archiveButton}</div>
    </article>`;
  }).join('');
}
async function loadCampaigns(){
  $('campaignList').innerHTML='<div class="library-empty">Loading saved campaigns…</div>';
  try{
    const response=await fetch('/api/campaigns');const data=await response.json();
    if(!response.ok)throw new Error(data.error||'Could not load campaign history.');
    campaignItems=data.campaigns||[];renderCampaignLibrary();
  }catch(error){$('campaignList').innerHTML=`<div class="library-empty">${escapeHtml(error.message)}</div>`;}
}
async function openSavedCampaign(id){
  clearMessage();
  try{
    const response=await fetch(`/api/drafts/${id}`);const data=await response.json();
    if(!response.ok||!data.draft)throw new Error(data.error||'Campaign could not be opened.');
    showWorkspace();showDraft(data.draft);
    $('workspaceTitle').textContent=data.draft.topic||'Campaign workspace';
    $('workspaceSubtitle').textContent=`${data.draft.rows.length} posts · ${data.draft.niche||''} · ${data.draft.board||''}`;
    setWorkflowActive(data.draft.final_ready?'export':'content');
    const target=data.draft.final_ready?'finalStage':'contentStage';
    $(target).scrollIntoView({behavior:'smooth',block:'start'});
  }catch(error){showMessage(error.message,'error');}
}
async function setCampaignArchived(id,archived){
  try{
    const response=await fetch(`/api/campaigns/${id}/${archived?'archive':'restore'}`,{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
    const data=await response.json();if(!response.ok)throw new Error(data.error||'Campaign update failed.');
    await loadCampaigns();
  }catch(error){showMessage(error.message,'error');}
}
function showMessage(text,type='ok'){const el=$('message');el.textContent=text;el.className='status '+type;if(type==='error'){console.error('ERROR MESSAGE:',text);}}
function clearMessage(){const el=$('message');el.textContent='';el.className='status';}
function setWorkflowActive(step){
  const steps=['content','images','export'];
  steps.forEach(name=>$(name==='content'?'navContent':name==='images'?'navImages':'navExport').classList.toggle('active',name===step));
}
function updateWorkflow(draft=activeDraft){
  const hasDraft=!!draft;
  const hasFinal=!!(draft&&draft.final_ready);
  $('navImages').disabled=!hasDraft;
  $('navExport').disabled=!hasFinal;
  $('navContentMeta').textContent=hasDraft?`${draft.rows.length} rows ready`:'Create campaign';
  $('navImagesMeta').textContent=hasDraft?(hasFinal?`${draft.final_rows.length} URLs linked`:'Select images'):'Add R2 URLs';
  $('navExportMeta').textContent=hasFinal?'CSV ready':'Download / import';
  $('navContent').classList.toggle('complete',hasDraft);
  $('navImages').classList.toggle('complete',hasFinal);
  $('navExport').classList.toggle('complete',!!(draft&&draft.imported));
}
function jumpToStage(stage){
  const ids={content:'contentStage',images:'imageStage',export:'finalStage'};
  const target=$(ids[stage]);
  if(!target||target.classList.contains('hidden'))return;
  setWorkflowActive(stage);
  target.scrollIntoView({behavior:'smooth',block:'start'});
}
function openModal(){$('createModal').classList.add('open');$('createModal').setAttribute('aria-hidden','false');}
function closeModal(){$('createModal').classList.remove('open');$('createModal').setAttribute('aria-hidden','true');}
function setDefaultStart(){
  const target=new Date(Date.now()+5*60000);
  const parts=new Intl.DateTimeFormat('en-CA',{timeZone:'Africa/Nairobi',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hourCycle:'h23'}).formatToParts(target);
  const p={};parts.forEach(x=>p[x.type]=x.value);
  $('startAt').value=`${p.year}-${p.month}-${p.day}T${p.hour}:${p.minute}`;
}
// Simplified: No board API lookup - manual board names only (like Social Media tab)
function loadBoards(){}
function toggleCustomBoardField(){}
async function checkStatus(){
  try{const response=await fetch('/api/status');const data=await response.json();$('connection').textContent=data.openrouter_ready?'OpenRouter connected':'OpenRouter key missing';$('connection').style.color=data.openrouter_ready?'#16834a':'#a3212b';}
  catch(error){$('connection').textContent='Setup status unavailable';}
}
function renderContentRows(rows){
  $('contentRows').innerHTML=rows.map(row=>`<tr><td><strong>#${row.row_number}</strong><br><span class="muted">${escapeHtml(row.scheduled_at)}</span></td><td class="titlecell">${escapeHtml(row.title)}</td><td class="desc">${escapeHtml(row.description)}</td><td class="keywords">${escapeHtml((row.keywords||[]).join(', '))}</td></tr>`).join('');
}
function buildImagePrompts(rows){
  return (rows||[]).map(row=>{
    let title=String(row.title??'').replace(/\s+/g,' ').trim();
    if(!title)return '';
    title=title.replace(/[.!?]+$/,'').trim()+'.';
    return `${title}\nCreate a visually engaging Pinterest pin that clearly communicates the idea and message of the title above.\n[Do not include any website links or URLs in the image]`;
  }).filter(Boolean).join('\n\n');
}
function renderImagePrompts(draft){
  const prompts=buildImagePrompts(draft.rows||[]);
  $('imagePromptsText').value=prompts;
  const count=(draft.rows||[]).filter(row=>String(row.title??'').trim()).length;
  $('imagePromptMeta').textContent=`${count} prompts · one prompt per title, ready for your bulk image generator.`;
  $('downloadImagePrompts').href=`/api/drafts/${draft.draft_id}/image-prompts.txt`;
  $('imagePromptsPreview').open=false;
}
async function copyImagePrompts(){
  const text=$('imagePromptsText').value;
  if(!text){showMessage('There are no image prompts to copy.','warn');return;}
  try{
    if(navigator.clipboard&&window.isSecureContext){
      await navigator.clipboard.writeText(text);
    }else{
      $('imagePromptsText').focus();$('imagePromptsText').select();
      const copied=document.execCommand('copy');
      if(!copied)throw new Error('Clipboard access was blocked. Expand the preview and copy the text manually.');
    }
    showMessage('Copied all image generation prompts, including the no-URL instruction, with a blank line between prompts.','ok');
  }catch(error){showMessage(error.message||'Could not copy prompts. Expand the preview and copy them manually.','error');}
}
function showDraft(draft){
  activeDraft=draft;
  showWorkspace();
  $('workspaceTitle').textContent=draft.topic||'Campaign workspace';
  $('workspaceSubtitle').textContent=`${(draft.rows||[]).length} posts · ${draft.niche||''} · ${draft.board||''}`;
  localStorage.setItem('pinterest_csv_draft_id',draft.draft_id);
  $('emptyState').classList.add('hidden');
  $('contentStage').classList.remove('hidden');
  $('imageStage').classList.remove('hidden');
  $('draftRows').textContent=draft.rows.length+' rows';
  $('draftTopic').textContent=draft.topic;
  $('draftTimezone').textContent=draft.timezone;
  $('contentDownload').href=`/api/drafts/${draft.draft_id}/content.csv`;
  $('contentPreviewLabel').textContent=`Preview generated content · ${draft.rows.length} rows`;
  $('contentPreview').open=false;
  renderContentRows(draft.rows);
  renderImagePrompts(draft);
  $('imageInstruction').textContent=`Choose exactly ${draft.rows.length} image(s) for this CSV.`;
  $('finalStage').classList.toggle('hidden',!draft.final_ready);
  $('finalPreview').open=false;
  updateWorkflow(draft);
  if(draft.final_ready){
    $('finalDownload').href=`/api/drafts/${draft.draft_id}/final.csv`;
    $('finalCount').textContent=draft.final_rows.length+' images linked';
    $('finalPreviewLabel').textContent=`Preview completed CSV · ${draft.final_rows.length} rows`;
    renderFinalRows(draft.final_rows);
    if(draft.imported){$('importScheduler').disabled=true;$('importScheduler').textContent='Imported';$('importMessage').textContent='This campaign has already been imported into the scheduler.';}
    else{$('importScheduler').disabled=true;$('importScheduler').textContent='Publishing disabled';$('importMessage').textContent='Publishing is disabled. Download your CSV instead.';}
  }else{
    $('finalRows').innerHTML='';
  }
}
async function restoreDraft(){
  const id=localStorage.getItem('pinterest_csv_draft_id');
  if(!id)return;
  try{
    const response=await fetch(`/api/drafts/${id}`);if(!response.ok)return;
    const data=await response.json();if(data.draft)showDraft(data.draft);
  }catch(error){console.warn('Could not restore previous campaign draft.',error);}
}

async function createCsv(event){
  event.preventDefault();
  clearMessage();
  console.log('=== CSV Generation Started ===');
  
  const rows=Number($('rowCount').value);
  const interval=Number($('interval').value);
  console.log('1. Validating rows and interval:',{rows,interval});
  
  if(!Number.isInteger(rows)||rows<1||rows>50){
    console.error('Validation failed: Invalid row count');
    showMessage('Rows must be between 1 and 50.','error');
    return;
  }
  if(!Number.isFinite(interval)||interval<0.25||interval>720){
    console.error('Validation failed: Invalid interval');
    showMessage('Interval must be between 0.25 and 720 hours.','error');
    return;
  }
  
  const boardName=$('boardName').value.trim();
  console.log('2. Board name:',{boardName});
  
    
  if(!boardName||boardName.length<1||boardName.length>180){
    console.error('Validation failed: Invalid board name');
    showMessage('Enter a Pinterest board name (1-180 characters).','error');
    return;
  }
  
    console.log('3. Board validation passed');
  const destination=$('destination').value.trim();
  console.log('4. Validating destination URL:',destination||'(none)');
    if(destination){
    try{
      const url=new URL(destination);
      if(!['http:','https:'].includes(url.protocol))throw new Error('Invalid protocol');
    }catch(e){
      console.error('Validation failed: Invalid destination URL',e);
      showMessage('Enter a valid destination URL.','error');
      return;
    }
  }
  
  const payload={
    row_count:rows,topic:$('topic').value.trim(),niche:$('niche').value.trim(),
    board_id:'__create_custom_board__',board:boardName,
    destination_url:destination,cta:$('cta').value.trim(),start_at:$('startAt').value,
    interval_hours:interval,timezone:$('timezone').value
  };
  console.log('5. All validations passed. Payload:',payload);
  
    const button=$('generateBtn');
  button.disabled=true;
  button.textContent='Generating…';
  console.log('6. Button disabled, sending request...');
  
  try{
    const response=await fetch('/api/create-csv',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
    console.log('7. Response received, status:',response.status);
    const data=await response.json();
    console.log('8. Response data:',data);
    if(!response.ok)throw new Error(data.error||'CSV generation failed.');
    await loadCampaigns();
    if(data.board_created){await loadBoards();}
    showDraft(data.draft);availableImages=[];$('imageList').innerHTML='<div class="empty">Copy your campaign images into ToPost, then scan the folder.</div>';$('availableCount').textContent='Not scanned';$('selectedCount').textContent='0 selected';$('insertUrls').disabled=true;
    closeModal();
    const boardNotice=data.board_created?` Pinterest board “${data.draft.board}” was created.`:'';
    showMessage(`Created a new saved campaign with ${data.draft.rows.length} rows.${boardNotice} Your earlier campaigns remain in the library.`,'ok');
    console.log('9. SUCCESS! Campaign created:',data.draft.draft_id);
    $('contentStage').scrollIntoView({behavior:'smooth',block:'start'});
  }catch(error){
    console.error('10. ERROR during generation:',error);
    showMessage(error.message,'error');
  }finally{
    button.disabled=false;
    button.textContent='Generate CSV';
    console.log('=== CSV Generation Ended ===');
  }
}

function updateSelectedCount(){
  const selected=document.querySelectorAll('.image-choice:checked').length;
  $('selectedCount').textContent=`${selected} selected`;
  $('insertUrls').disabled=!activeDraft||selected!==activeDraft.rows.length;
  if(activeDraft&&selected!==activeDraft.rows.length){$('insertUrls').title=`Select exactly ${activeDraft.rows.length} images.`;}else{$('insertUrls').title='';}
}
function renderImageChoices(){
  if(!availableImages.length){$('imageList').innerHTML='<div class="empty">No supported images found in ToPost. Add JPG, JPEG, PNG or another supported still-image file and scan again.</div>';updateSelectedCount();return;}
  const needed=activeDraft.rows.length;
  $('imageList').innerHTML=availableImages.map((image,index)=>`<label class="image-item"><input type="checkbox" class="image-choice" data-index="${index}" ${index<needed?'checked':''}><span class="image-name">${escapeHtml(image.filename)}</span><span class="image-state ${image.hosted?'':'pending'}">${image.hosted?'Already on R2':'Will upload to R2'}</span></label>`).join('');
  document.querySelectorAll('.image-choice').forEach(input=>input.addEventListener('change',updateSelectedCount));
  updateSelectedCount();
}
async function scanImages(){
  if(!activeDraft)return;clearMessage();$('scanImages').disabled=true;$('scanImages').textContent='Scanning…';
  try{
    const response=await fetch(`/api/drafts/${activeDraft.draft_id}/scan-images`);const data=await response.json();
    if(!response.ok)throw new Error(data.error||'Could not scan ToPost folder.');
    availableImages=data.images||[];
    $('availableCount').textContent=`${availableImages.length} image(s) found`;
    $('imagePreviewLabel').textContent=`Review available images · ${availableImages.length} found`;
    $('imagePreview').open=true;
    renderImageChoices();
    if(availableImages.length<activeDraft.rows.length){showMessage(`This CSV needs ${activeDraft.rows.length} images, but ToPost currently has ${availableImages.length}. Add ${activeDraft.rows.length-availableImages.length} more supported image(s).`,'warn');}
    else if(availableImages.length>activeDraft.rows.length){showMessage(`Found ${availableImages.length} images. Select exactly ${activeDraft.rows.length}. Files are listed alphabetically; selected order matches CSV row order.`,'ok');}
    else{showMessage(`Found exactly ${availableImages.length} images. Review the order, then insert the URLs.`,'ok');}
  }catch(error){showMessage(error.message,'error');}
  finally{$('scanImages').disabled=false;$('scanImages').textContent='Scan ToPost folder';}
}
function chosenImages(){return [...document.querySelectorAll('.image-choice:checked')].map(el=>availableImages[Number(el.dataset.index)]).filter(Boolean);}
function renderFinalRows(rows){
  $('finalRows').innerHTML=rows.map(row=>`<tr><td>${escapeHtml(row.filename)}</td><td><a href="${escapeHtml(row.media_url)}" target="_blank" rel="noopener">Open R2 URL</a></td><td class="titlecell">${escapeHtml(row.title)}</td><td>${escapeHtml(row.scheduled_at)}<br><span class="muted">${escapeHtml(row.timezone)}</span></td></tr>`).join('');
}
async function insertImageUrls(){
  if(!activeDraft)return;clearMessage();const selected=chosenImages();
  if(selected.length!==activeDraft.rows.length){showMessage(`Select exactly ${activeDraft.rows.length} images before continuing.`,'error');return;}
  const button=$('insertUrls');button.disabled=true;button.textContent='Uploading and inserting URLs…';
  try{
    const response=await fetch(`/api/drafts/${activeDraft.draft_id}/insert-images`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({filenames:selected.map(image=>image.filename)})});
    const data=await response.json();if(!response.ok)throw new Error(data.error||'Could not insert image URLs.');
    activeDraft=data.draft;showDraft(activeDraft);$('imagePreview').open=false;setWorkflowActive('export');showMessage(`Inserted ${data.inserted} verified R2 image URL(s). Your final CSV is ready to download.`,'ok');
    $('finalStage').scrollIntoView({behavior:'smooth',block:'start'});
  }catch(error){showMessage(error.message,'error');button.disabled=false;button.textContent='Insert Image URLs & Complete CSV';}
}
async function importIntoScheduler(){
  if(!activeDraft||!activeDraft.final_ready)return;clearMessage();const button=$('importScheduler');button.disabled=true;button.textContent='Importing…';
  try{
    const response=await fetch(`/api/drafts/${activeDraft.draft_id}/import`,{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
    const data=await response.json();if(!response.ok)throw new Error(data.error||'Import failed.');
    $('importMessage').textContent=`Imported ${data.inserted} Pin(s). Skipped ${data.skipped} already scheduled duplicate(s).`;
    showMessage('Final CSV imported into your existing Pinterest Scheduler queue.','ok');button.textContent='Imported';
  }catch(error){showMessage(error.message,'error');button.disabled=false;button.textContent='Publishing disabled';}
}

$('openCreate').addEventListener('click',openModal);
$('copyImagePrompts').addEventListener('click',copyImagePrompts);
$('openCreateLibrary').addEventListener('click',openModal);
$('backToLibrary').addEventListener('click',showLibrary);
$('campaignSearch').addEventListener('input',renderCampaignLibrary);
$('libraryFilters').addEventListener('click',event=>{
  const button=event.target.closest('[data-filter]');if(!button)return;
  campaignFilter=button.dataset.filter;
  document.querySelectorAll('.filter-chip').forEach(el=>el.classList.toggle('active',el===button));
  renderCampaignLibrary();
});
$('campaignList').addEventListener('click',event=>{
  const open=event.target.closest('[data-open]');
  const archive=event.target.closest('[data-archive]');
  const restore=event.target.closest('[data-restore]');
  if(open)openSavedCampaign(open.dataset.open);
  else if(archive)setCampaignArchived(archive.dataset.archive,true);
  else if(restore)setCampaignArchived(restore.dataset.restore,false);
});
$('navContent').addEventListener('click',()=>jumpToStage('content'));
$('navImages').addEventListener('click',()=>jumpToStage('images'));
$('navExport').addEventListener('click',()=>jumpToStage('export'));
$('closeModal').addEventListener('click',closeModal);
$('cancelCreate').addEventListener('click',closeModal);
$('createModal').addEventListener('click',event=>{if(event.target===$('createModal'))closeModal();});
$('createForm').addEventListener('submit',createCsv);
$('showImages').addEventListener('click',()=>$('imageStage').scrollIntoView({behavior:'smooth',block:'start'}));
$('scanImages').addEventListener('click',scanImages);
$('insertUrls').addEventListener('click',insertImageUrls);
$('importScheduler').addEventListener('click',importIntoScheduler);
console.log('🚀 Initializing Pinterest CSV Generator...');
setDefaultStart();
console.log('✓ Default start time set');
updateWorkflow(null);
console.log('✓ Workflow updated');
loadBoards();
console.log('✓ Board loading initiated');
checkStatus();
console.log('✓ Status check initiated');
loadCampaigns();
console.log('✓ Campaign loading initiated');
console.log('✅ Pinterest tab initialization complete!');
</script>
</body>
</html>'''


# -------------------------------------------------------------------
# PINTEREST BOARD HELPERS
# -------------------------------------------------------------------

def find_or_create_pinterest_board(board_name):
    """Return (board_id, canonical_name, created). Reuse a name match first."""
    from pinterest_client import PinterestClient

    name = str(board_name or "").strip()
    if not name or len(name) > 180:
        raise ValueError("Custom Pinterest board name must be between 1 and 180 characters.")

    client = PinterestClient()

    def find_match(items):
        wanted = name.casefold()
        return next(
            (item for item in items
             if str(item.get("name", "")).strip().casefold() == wanted
             and item.get("id")),
            None,
        )

    existing = find_match(client.list_boards())
    if existing:
        return str(existing["id"]), str(existing.get("name") or name), False

    create_error = None
    created = None
    try:
        # Pinterest API v5 creates a real board and returns its ID.
        created = client.request("POST", "/boards", json={"name": name})
    except Exception as exc:
        create_error = exc

    if isinstance(created, dict):
        # Accept the direct API response, plus common wrapped response shapes.
        board_data = created.get("board") or created.get("data") or created
        if isinstance(board_data, dict) and board_data.get("id"):
            return (
                str(board_data["id"]),
                str(board_data.get("name") or name),
                True,
            )

    # In case the create response was lost or omitted fields, verify by listing
    # again before reporting failure. This also avoids duplicate board creation.
    try:
        after_create = find_match(client.list_boards())
    except Exception as verify_error:
        if create_error:
            raise RuntimeError(
                f"Pinterest could not create or verify board '{name}'. "
                f"Create error: {create_error}. Verification error: {verify_error}"
            ) from verify_error
        raise RuntimeError(
            f"Pinterest returned no board ID for '{name}', and the new board could not be verified: {verify_error}"
        ) from verify_error

    if after_create:
        was_created = create_error is None
        return str(after_create["id"]), str(after_create.get("name") or name), was_created

    if create_error:
        raise RuntimeError(f"Could not create Pinterest board '{name}': {create_error}") from create_error
    raise RuntimeError(f"Pinterest did not return a board ID for '{name}', and the board was not found afterward.")


# -------------------------------------------------------------------
# ROUTES
# -------------------------------------------------------------------

@app.get("/")
def home():
    return render_template_string(HTML)


@app.get("/api/status")
def api_status():
    model = os.getenv("OPENROUTER_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL
    fallback_setting = os.getenv("OPENROUTER_FALLBACK_MODELS", "").strip()
    fallbacks = (
        [item.strip() for item in fallback_setting.split(",") if item.strip()]
        if fallback_setting else list(DEFAULT_FALLBACK_MODELS)
    )
    return jsonify({
        "openrouter_ready": bool(os.getenv("OPENROUTER_API_KEY", "").strip()),
        "model": model,
        "fallback_models": fallbacks,
        "timezone_options": TIMEZONES,
    })


# Board API endpoint removed - using manual board names only (like Social Media tab)
# This avoids the dotenv import error and simplifies the workflow


@app.post("/api/create-csv")
def api_create_csv():
    data = request.get_json(silent=True) or {}
    topic = str(data.get("topic", "")).strip()
    niche = str(data.get("niche", "")).strip()
    cta = str(data.get("cta", "")).strip()
    board_id = str(data.get("board_id", "")).strip()
    board = str(data.get("board", "")).strip()
    destination_url = str(data.get("destination_url", "")).strip()
    start_at = str(data.get("start_at", "")).strip()
    timezone_name = str(data.get("timezone", "")).strip()

    try:
        row_count = int(data.get("row_count", 0))
        interval_hours = float(data.get("interval_hours", 0))
    except (TypeError, ValueError):
        return jsonify({"error": "Rows and interval must be valid numbers."}), 400

    if not 1 <= row_count <= 50:
        return jsonify({"error": "Number of rows must be between 1 and 50."}), 400
    if not 0.25 <= interval_hours <= 720:
        return jsonify({"error": "Interval must be between 0.25 and 720 hours."}), 400
    if not topic or not niche:
        return jsonify({"error": "Enter both a topic and a niche."}), 400
    if len(topic) > 180 or len(niche) > 180 or len(cta) > 300:
        return jsonify({"error": "One or more campaign fields are too long."}), 400
    custom_board_requested = False
    if not board:
        board = "Pinterest"
    if not board_id:
        board_id = "manual"
    if destination_url:
        parsed = urlsplit(destination_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return jsonify({"error": "Destination URL must begin with http:// or https://."}), 400
    if not start_at or timezone_name not in TIMEZONES:
        return jsonify({"error": "Enter a start date/time and supported timezone."}), 400
    if not os.getenv("OPENROUTER_API_KEY", "").strip():
        return jsonify({"error": "OpenRouter API key is missing. Add OPENROUTER_API_KEY to .env first."}), 400

    try:
        schedules = create_schedule(start_at, interval_hours, row_count, timezone_name)
        generated, cached = generate_campaign_content(row_count, topic, niche, cta)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except requests.RequestException as exc:
        return jsonify({"error": f"Could not connect to OpenRouter: {exc}"}), 502
    except Exception as exc:
        return jsonify({"error": str(exc)}), 502

    board_created = False

    draft_id = uuid.uuid4().hex
    rows = []
    for index, (metadata, scheduled_at) in enumerate(zip(generated, schedules), start=1):
        rows.append({
            "row_number": index,
            "title": metadata["title"],
            "description": metadata["description"],
            "keywords": metadata.get("keywords", []),
            "scheduled_at": scheduled_at,
        })

    draft = {
        "draft_id": draft_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "topic": topic,
        "niche": niche,
        "cta": cta,
        "destination_url": destination_url,
        "board": board,
        "board_id": board_id,
        "timezone": timezone_name,
        "start_at": start_at,
        "interval_hours": interval_hours,
        "rows": rows,
        "final_ready": False,
        "final_rows": [],
        "imported": False,
    }
    save_draft(draft)
    write_content_csv(draft)
    return jsonify({
        "draft": draft,
        "cached": cached,
        "board_created": board_created,
        "content_csv_url": f"/api/drafts/{draft_id}/content.csv",
    })


@app.get("/api/campaigns")
def api_campaigns():
    try:
        return jsonify({"campaigns": get_campaign_library()})
    except Exception as exc:
        return jsonify({"error": f"Could not load campaign history: {exc}"}), 500


@app.post("/api/campaigns/<draft_id>/archive")
def api_archive_campaign(draft_id):
    try:
        draft = load_draft(draft_id)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    if not draft:
        return jsonify({"error": "Campaign not found."}), 404
    draft["archived"] = True
    draft["archived_at"] = datetime.now(timezone.utc).isoformat()
    save_draft(draft)
    return jsonify({"success": True})


@app.post("/api/campaigns/<draft_id>/restore")
def api_restore_campaign(draft_id):
    try:
        draft = load_draft(draft_id)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    if not draft:
        return jsonify({"error": "Campaign not found."}), 404
    draft["archived"] = False
    draft.pop("archived_at", None)
    save_draft(draft)
    return jsonify({"success": True})


@app.get("/api/drafts/<draft_id>")
def api_get_draft(draft_id):
    try:
        draft = load_draft(draft_id)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    if not draft:
        return jsonify({"error": "Campaign draft not found."}), 404
    return jsonify({"draft": draft})


@app.get("/api/drafts/<draft_id>/content.csv")
def api_download_content_csv(draft_id):
    try:
        paths = draft_paths(draft_id)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    draft = load_draft(draft_id)
    if not draft:
        return jsonify({"error": "Campaign draft not found."}), 404
    write_content_csv(draft)
    return send_file(
        paths["content_csv"], as_attachment=True,
        download_name=f"pinterest_content_{draft_id[:8]}.csv",
        mimetype="text/csv",
    )


@app.get("/api/drafts/<draft_id>/image-prompts.txt")
def api_download_image_prompts(draft_id):
    try:
        draft = load_draft(draft_id)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    if not draft:
        return jsonify({"error": "Campaign draft not found."}), 404

    prompt_text = build_image_generation_prompts(draft.get("rows", []))
    if not prompt_text:
        return jsonify({"error": "This campaign has no titles to turn into image prompts."}), 400

    return Response(
        prompt_text + "\n",
        mimetype="text/plain; charset=utf-8",
        headers={
            "Content-Disposition": (
                f'attachment; filename="pinterest_image_prompts_{draft_id[:8]}.txt"'
            )
        },
    )


@app.get("/api/drafts/<draft_id>/scan-images")
def api_scan_images(draft_id):
    try:
        draft = load_draft(draft_id)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    if not draft:
        return jsonify({"error": "Campaign draft not found."}), 404
    return jsonify({
        "images": get_local_images(),
        "required": len(draft.get("rows", [])),
    })


@app.post("/api/drafts/<draft_id>/insert-images")
def api_insert_images(draft_id):
    try:
        draft = load_draft(draft_id)
        paths = draft_paths(draft_id)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    if not draft:
        return jsonify({"error": "Campaign draft not found."}), 404
    if draft.get("final_ready"):
        return jsonify({"error": "Image URLs have already been inserted for this draft."}), 409

    data = request.get_json(silent=True) or {}
    filenames = data.get("filenames")
    needed = len(draft.get("rows", []))
    if not isinstance(filenames, list) or len(filenames) != needed:
        return jsonify({"error": f"Select exactly {needed} images."}), 400
    if len(set(filenames)) != len(filenames):
        return jsonify({"error": "The same image cannot be selected more than once."}), 400

    selected_paths = []
    for filename in filenames:
        if not isinstance(filename, str) or Path(filename).name != filename:
            return jsonify({"error": "Invalid image filename."}), 400
        image_path = TO_POST / filename
        if not image_path.is_file() or image_path.suffix.lower() not in ALLOWED_EXTENSIONS:
            return jsonify({"error": f"Supported image not found in ToPost: {filename}"}), 400
        selected_paths.append(image_path)

    uploaded = {}
    final_rows = []
    try:
        with UPLOAD_LOCK:
            uploaded = read_manifest()
            for index, (row, image_path) in enumerate(zip(draft["rows"], selected_paths), start=1):
                media_url = uploaded.get(image_path.name, "")
                if not media_url:
                    print(f"Uploading selected campaign image to R2: {image_path.name}")
                    media_url, size, content_type = upload_images.upload_image(image_path)
                    uploaded[image_path.name] = media_url
                    save_manifest(uploaded)
                    print(f"R2 upload complete: {image_path.name} ({size} bytes, {content_type})")

                if not media_url.startswith("https://"):
                    raise RuntimeError(f"Invalid public R2 URL for {image_path.name}.")

                final_rows.append({
                    "row_number": index,
                    "filename": image_path.name,
                    "media_url": media_url,
                    "title": row["title"],
                    "description": row["description"],
                    "keywords": row.get("keywords", []),
                    "destination_url": make_unique_destination_url(
                        draft.get("destination_url", ""), row.get("title", "")
                    ),
                    "scheduled_at": row["scheduled_at"],
                    "timezone": draft["timezone"],
                })

        draft["final_rows"] = final_rows
        draft["final_ready"] = True
        draft["finalized_at"] = datetime.now(timezone.utc).isoformat()
        save_draft(draft)
        write_final_csv(draft)
    except Exception as exc:
        return jsonify({"error": f"Image URL insertion stopped: {exc}"}), 502

    return jsonify({"inserted": len(final_rows), "draft": draft})


@app.get("/api/drafts/<draft_id>/final.csv")
def api_download_final_csv(draft_id):
    try:
        paths = draft_paths(draft_id)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    draft = load_draft(draft_id)
    if not draft or not draft.get("final_ready"):
        return jsonify({"error": "Final CSV is not ready. Insert image URLs first."}), 404
    write_final_csv(draft)
    return send_file(
        paths["final_csv"], as_attachment=True,
        download_name=f"pinterest_campaign_{draft_id[:8]}.csv",
        mimetype="text/csv",
    )


@app.post("/api/drafts/<draft_id>/import")
def api_import_draft(draft_id):
    return jsonify({"error": "Publishing and scheduler imports are disabled in this Content Studio deployment. Download the CSV instead."}), 410

if __name__ == "__main__":
    print("Pinterest CSV Generator: http://127.0.0.1:5001")
    print("OpenRouter primary:", os.getenv("OPENROUTER_MODEL", DEFAULT_MODEL))
    configured_fallbacks = os.getenv("OPENROUTER_FALLBACK_MODELS", "").strip()
    print("OpenRouter free fallbacks:", configured_fallbacks or ", ".join(DEFAULT_FALLBACK_MODELS))
    app.run(host="127.0.0.1", port=5001, debug=False, use_reloader=False)
