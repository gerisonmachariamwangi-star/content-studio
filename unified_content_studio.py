"""Unified local Content Studio.

Tabs:
- Pinterest Pins: reuses the existing Pinterest CSV Generator unchanged.
- Social Media Posts: identical workflow to Pinterest Pins — same Campaign Library
  UI, same Content → Images → Export stages, same 8-column Pinterest CSV format,
  same "Publishing disabled" button.

Place this file beside csv_generator_two_stage.py and the existing project modules.
Run: python unified_content_studio.py
"""
from __future__ import annotations

import csv
import json
import os
import re
import shutil
import sys
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from flask import Response, jsonify, render_template_string, request, send_file
from werkzeug.utils import secure_filename

import csv_generator_two_stage as pins
import social_card_renderer as social

app = pins.app
ROOT = pins.ROOT
EXPORT_DIR = pins.EXPORT_DIR
SOCIAL_OUTPUT_DIR = EXPORT_DIR / "Social_Media_Output"
SOCIAL_ASSET_DIR = EXPORT_DIR / "Social_Media_Assets"
SOCIAL_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
SOCIAL_ASSET_DIR.mkdir(parents=True, exist_ok=True)
app.secret_key = pins.app.secret_key

app.config["MAX_CONTENT_LENGTH"] = 800 * 1024 * 1024

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".webm", ".mkv", ".m4v", ".gif"}
AUDIO_EXTENSIONS = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"}

# Publer columns kept for the render/card workflow (not used in main CSV export any more).
PUBLER_COLUMNS = [
    "Date - Intl. format or prompt",
    "Text",
    "Link(s) - Separated by comma for FB carousels",
    "Media URL(s) - Separated by comma",
    "Title - For the video pin PDF ..",
    "Label(s) - Separated by comma",
    "Alt text(s) - Separated by ||",
    "Comment(s) - Separated by ||",
    "Post subtype - I.e. story reel PDF ..",
    "CTA - For FB links or Google",
    "Reminder",
]


def _is_social(draft: dict) -> bool:
    return str(draft.get("content_type", "pinterest")).lower() == "social"


_PUBLER_FIELDS = [
    "Date - Intl. format or prompt",
    "Text",
    "Link(s) - Separated by comma for FB carousels",
    "Media URL(s) - Separated by comma",
    "Title - For the video pin PDF ..",
    "Label(s) - Separated by comma",
    "Alt text(s) - Separated by ||",
    "Comment(s) - Separated by ||",
    "Post subtype - I.e. story reel PDF ..",
    "caption",
    "background_url",
]


def _social_alt_text(item: dict) -> str:
    """Return one sentence for a single image, or ||-joined entries for a carousel."""
    values = item.get("alt_texts")
    if isinstance(values, (list, tuple)):
        cleaned = [" ".join(str(value or "").split()) for value in values]
        cleaned = [value.rstrip(".!?;: ") + "." for value in cleaned if value]
        return " || ".join(cleaned)
    value = item.get("alt_text") or item.get("description") or ""
    value = " ".join(str(value).split()).strip()
    if not value:
        return ""
    return value if value.endswith((".", "!", "?")) else value + "."


def _write_social_content_csv(draft):
    """Write the Publer-format content CSV (no media URLs yet)."""
    rows = []
    for item in draft["rows"]:
        title = item.get("title", "")
        description = item.get("description", "")
        caption = item.get("caption", "") or description  # viral short post text
        link = pins.make_unique_destination_url(
            draft.get("destination_url", ""), title
        )
        pub_date = pins.format_publish_date(
            item.get("scheduled_at", ""), draft.get("timezone", "UTC")
        )
        keywords = ", ".join(item.get("keywords", []))
        rows.append({
            "Date - Intl. format or prompt": pub_date,
            # Text is the primary post description; Caption is reserved for image/text artwork.
            "Text": description,
            "Link(s) - Separated by comma for FB carousels": link,
            "Media URL(s) - Separated by comma": "",
            "Title - For the video pin PDF ..": title,
            "Label(s) - Separated by comma": keywords,
            # One sentence for a single image; carousel alt texts are joined with ||.
            "Alt text(s) - Separated by ||": _social_alt_text(item),
            "Comment(s) - Separated by ||": "",
            "Post subtype - I.e. story reel PDF ..": "",
            # Caption remains the artwork text and image-generation prompt source.
            "caption": caption,
            "background_url": "",
        })
    pins.write_csv(pins.draft_paths(draft["draft_id"])["content_csv"], _PUBLER_FIELDS, rows)


def _write_social_final_csv_publer(draft):
    """Write the Publer-format final CSV with media URLs filled in."""
    rows = []
    for item in draft["final_rows"]:
        title = item.get("title", "")
        description = item.get("description", "")
        caption = item.get("caption", "") or description  # viral short post text
        media_url = item.get("media_url", "")
        link = pins.make_unique_destination_url(
            draft.get("destination_url", "") or item.get("destination_url", ""),
            title,
        )
        pub_date = pins.format_publish_date(
            item.get("scheduled_at", ""),
            item.get("timezone", draft.get("timezone", "UTC")),
        )
        keywords = ", ".join(item.get("keywords", []))
        rows.append({
            "Date - Intl. format or prompt": pub_date,
            # Text is the primary post description; Caption remains the artwork text.
            "Text": description,
            "Link(s) - Separated by comma for FB carousels": link,
            "Media URL(s) - Separated by comma": media_url,
            "Title - For the video pin PDF ..": title,
            "Label(s) - Separated by comma": keywords,
            # One sentence for a single image; carousel alt texts are joined with ||.
            "Alt text(s) - Separated by ||": _social_alt_text(item),
            "Comment(s) - Separated by ||": "",
            "Post subtype - I.e. story reel PDF ..": "",
            "caption": caption,
            "background_url": media_url,
        })
    pins.write_csv(pins.draft_paths(draft["draft_id"])["final_csv"], _PUBLER_FIELDS, rows)


def _generate_social_captions(rows: list, topic: str, niche: str) -> list[str]:
    """Call OpenRouter to generate one short viral caption per row."""
    import json as _json
    import requests as _req

    api_key = os.getenv("OPENROUTER_API_KEY", "").strip()
    if not api_key:
        return [""] * len(rows)

    items = "\n".join(
        f"{i+1}. Title: {row.get('title','')}\n   Description: {row.get('description','')}"
        for i, row in enumerate(rows)
    )

    system_prompt = (
        "You are a viral social media writer. "
        "Write short, punchy captions that feel like popular Instagram or Facebook quote posts. "
        "Use simple language a 5th grader can understand. "
        "Each caption should be 2–5 short sentences or lines. "
        "Make it feel personal, motivating, and relatable — not corporate. "
        "No hashtags. No emojis. No URLs. Plain text only."
    )

    user_prompt = (
        f"Topic: {topic}\nNiche: {niche}\n\n"
        f"Write one viral plain-text caption for each of these {len(rows)} posts. "
        "Return ONLY a JSON array of strings — one caption per item, same order.\n\n"
        f"{items}"
    )

    try:
        resp = _req.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": "openai/gpt-4o-mini",
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "temperature": 0.8,
            },
            timeout=60,
        )
        resp.raise_for_status()
        raw = resp.json()["choices"][0]["message"]["content"].strip()
        # Strip markdown code fences if present
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        captions = _json.loads(raw.strip())
        if isinstance(captions, list) and len(captions) == len(rows):
            return [str(c).strip() for c in captions]
    except Exception as exc:
        print(f"Caption generation failed, falling back to description: {exc}")

        return [str(row.get("description", "")).strip() for row in rows]


def _generate_social_comments(rows: list, topic: str, niche: str) -> list[str]:
    """Call OpenRouter to generate two short, relevant comments per row, joined by ||."""
    import json as _json
    import requests as _req

    api_key = os.getenv("OPENROUTER_API_KEY", "").strip()
    if not api_key:
        return [""] * len(rows)

    items = "\n".join(
        f"{i+1}. Title: {row.get('title','')}\n   Description: {row.get('description','')}"
        for i, row in enumerate(rows)
    )

    system_prompt = (
        "You are a social media engagement writer. "
        "For each post, write two short, relevant comments that feel like real audience reactions. "
        "Use simple language a 5th grader can understand. "
        "Each comment should be 1-2 short sentences and directly relate to the post. "
        "Make them feel personal, warm, and authentic — not corporate. "
        "No hashtags. No emojis. No URLs. Plain text only."
    )

    user_prompt = (
        f"Topic: {topic}\nNiche: {niche}\n\n"
        f"For each of these {len(rows)} posts, write TWO short plain-text comments. "
        "Return ONLY a JSON array of string pairs — each inner array contains exactly two comments "
        "for that post, in the same order as the list below.\n\n"
        f"{items}"
    )

    try:
        resp = _req.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": "openai/gpt-4o-mini",
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "temperature": 0.8,
            },
            timeout=60,
        )
        resp.raise_for_status()
        raw = resp.json()["choices"][0]["message"]["content"].strip()
        # Strip markdown code fences if present
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        comments = _json.loads(raw.strip())
        if isinstance(comments, list) and len(comments) == len(rows):
            result = []
            for pair in comments:
                if isinstance(pair, list) and len(pair) >= 2:
                    result.append(" || ".join(str(c).strip() for c in pair[:2]))
                elif isinstance(pair, str):
                    result.append(pair)
                else:
                    result.append("")
            return result
    except Exception as exc:
        print(f"Comment generation failed: {exc}")

    return [""] * len(rows)


def _build_social_image_prompts(rows: list) -> str:
    """Build image generation prompts using the caption field."""
    parts = []
    for row in rows:
        desc = " ".join(str(row.get("caption") or row.get("description") or "").split())
        if not desc:
            continue
        # Ensure single trailing period
        desc = desc.rstrip(".!?").strip() + "."
        parts.append(
            f"{desc}\n"
            "9:16 portrait. Plain lightly textured paper background, warm off-white to light grey. "
            "Text only — no illustrations, no borders, no decorations. "
            "ALL text uses the same old-school Courier typewriter monospace font throughout — bold weight for the headline, regular weight for the body. "
            "First sentence is the headline in large bold Courier typewriter font WITH a bright solid yellow marker highlight painted behind it. "
            "Remaining lines in regular Courier typewriter font, same typeface, left-aligned, generous line spacing, easy to read. "
            "Consistent single font family from top to bottom. Minimal and clean. No logos, no URLs, no watermarks."
        )
    return "\n\n".join(parts)


# Keep social campaigns out of the Pinterest Campaign Library.
_original_get_campaign_library = pins.get_campaign_library

def _pinterest_campaign_library():
    visible = []
    for item in _original_get_campaign_library():
        draft = pins.load_draft(item.get("draft_id", ""))
        if draft and not _is_social(draft):
            visible.append(item)
    return visible

pins.get_campaign_library = _pinterest_campaign_library

# Guard social campaigns from being imported into the Pinterest scheduler via the
# generic Pinterest import endpoint.
_original_import_draft = app.view_functions.get("api_import_draft")
def _guard_social_campaign_import(draft_id):
    draft = pins.load_draft(draft_id)
    if draft and _is_social(draft):
        return jsonify({"error": "Use /api/social/campaigns/<id>/import for Social Media campaigns."}), 400
    return _original_import_draft(draft_id)

if _original_import_draft:
    app.view_functions["api_import_draft"] = _guard_social_campaign_import


SHELL_HTML = r'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Content Studio</title>
<style>/* Visual styles live in static/app-theme.css */</style><link rel="stylesheet" href="{{ url_for('static', filename='app-theme.css') }}"><style>
.cs-header{position:sticky;top:0;z-index:50;height:60px;display:grid;grid-template-columns:1fr auto 1fr;align-items:center;gap:16px;padding:0 28px;background:#fff;border-bottom:1px solid #e5e9f0;box-sizing:border-box}
.cs-brand{display:flex;align-items:center;gap:10px;font-weight:800;font-size:17px;color:#0f172a}
.cs-logo{width:30px;height:30px;border-radius:9px;background:#4f46e5;color:#fff;display:grid;place-items:center;font-size:14px;box-shadow:0 4px 10px rgba(79,70,229,.3)}
.cs-tabs{display:flex;gap:6px}
.cs-tab{border:0;background:transparent;cursor:pointer;padding:8px 18px;border-radius:999px;font:700 14px inherit;font-family:inherit;color:#64748b;transition:background .15s,color .15s}
.cs-tab:hover{background:#f1f5f9;color:#0f172a}
.cs-tab.active{background:#4f46e5;color:#fff;box-shadow:0 4px 12px rgba(79,70,229,.3)}
.cs-status{justify-self:end;display:inline-flex;align-items:center;gap:8px;padding:6px 14px;border-radius:999px;font-size:12px;font-weight:700;background:#f1f5f9;color:#64748b}
.cs-status i{width:8px;height:8px;border-radius:50%;background:currentColor}
.cs-status.ok{background:#ecfdf5;color:#047857}
.cs-status.bad{background:#fef2f2;color:#b91c1c}
.frame-wrap{height:calc(100vh - 60px) !important}
@media(max-width:720px){.cs-header{grid-template-columns:1fr auto;height:auto;padding:10px 16px}.cs-tabs{grid-column:1/-1;order:3;justify-content:center}.frame-wrap{height:calc(100vh - 100px) !important}}
</style></head><body>
<div class="cs-header" role="banner">
  <div class="cs-brand"><span class="cs-logo">◆</span> Content Studio</div>
  <nav class="cs-tabs" aria-label="Content tools">
    <button class="cs-tab active" data-tab="pins">Pinterest Pins</button>
    <button class="cs-tab" data-tab="social">Social Media Posts</button>
  </nav>
  <span class="cs-status" id="csStatus"><i></i><span>Checking OpenRouter…</span></span>
</div>
<div class="frame-wrap">
<iframe id="pinsFrame" class="frame active" src="/pins" title="Pinterest Pins"></iframe>
<iframe id="socialFrame" class="frame" src="/social" title="Social Media Posts"></iframe>
</div>
<script>
const tabs=[...document.querySelectorAll('.cs-tab')];
function selectTab(name){
  tabs.forEach(btn=>btn.classList.toggle('active',btn.dataset.tab===name));
  document.getElementById('pinsFrame').classList.toggle('active',name==='pins');
  document.getElementById('socialFrame').classList.toggle('active',name==='social');
  try{localStorage.setItem('content_studio_active_tab',name)}catch(e){}
}
tabs.forEach(btn=>btn.addEventListener('click',()=>selectTab(btn.dataset.tab)));
fetch('/api/status').then(r=>r.json()).then(d=>{const el=document.getElementById('csStatus');el.classList.add(d.openrouter_ready?'ok':'bad');el.lastChild.textContent=d.openrouter_ready?'OpenRouter connected':'OpenRouter key missing';}).catch(()=>{const el=document.getElementById('csStatus');el.classList.add('bad');el.lastChild.textContent='Status unavailable';});
try{const saved=localStorage.getItem('content_studio_active_tab');if(saved==='social')selectTab('social')}catch(e){}
</script></body></html>'''

# Hide the Pinterest sub-app's own header; the unified shell supplies the shared header.
if "<header>" in pins.HTML:
    pins.HTML = pins.HTML.replace("<header>", '<header style="display:none">', 1)

_original_pins_home = app.view_functions.get("home")

@app.get("/pins", endpoint="unified_pins_home")
def unified_pins_home():
    return _original_pins_home()

def unified_home():
    return render_template_string(SHELL_HTML)

app.view_functions["home"] = unified_home


# ---------------------------------------------------------------------------
# SOCIAL MEDIA POSTS HTML  — identical to Pinterest Pins, social API endpoints
# ---------------------------------------------------------------------------

SOCIAL_HTML = r'''<!doctype html>
<html lang="en" data-platform="meta">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Social Media Posts</title>
<style>/* Visual styles live in static/app-theme.css */</style><link rel="stylesheet" href="{{ url_for('static', filename='app-theme.css') }}">
</head>
<body>
<header style="display:none">
  <div><div class="brand"><i>●</i> Social Media Posts</div><div class="sub">Create social content first. Add R2 image URLs when your images are ready.</div></div>
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
    <div class="step"><div class="stepnum">2</div><div><h2>Add media and insert their R2 URLs</h2><p>Choose still images from your computer, or use MP4 videos generated into ToPost/videos/ when running locally.</p></div></div>
    <div style="display:flex;gap:8px;margin-bottom:14px">
      <button class="secondary media-tab active" id="tabImages" onclick="switchMediaTab('images')">🖼 Still Images</button>
      <button class="secondary media-tab" id="tabVideos" onclick="switchMediaTab('videos')">🎬 Videos (MP4)</button>
    </div>

    <div id="imagePanel">
      <div class="row" style="flex-wrap:wrap"><div><strong id="imageInstruction">Select images for this campaign.</strong><div class="small muted">Files are listed alphabetically. The selected order maps to CSV rows from top to bottom.</div></div><input id="imageFiles" type="file" accept=".jpg,.jpeg,.png,.webp,.bmp,.tif,.tiff,image/*" multiple hidden><button class="secondary" id="scanImages">Choose images</button></div>
      <div class="statline"><span class="count" id="availableCount">No scan yet</span><span class="count" id="selectedCount">0 selected</span></div>
      <details class="preview-details" id="imagePreview">
        <summary><span id="imagePreviewLabel">Choose images from your computer</span><span class="summary-hint">The selection order matches the CSV rows</span></summary>
        <div id="imageList" class="image-list" style="padding:10px 12px"><div class="empty">Choose your campaign images to begin.</div></div>
      </details>
      <div class="actions" style="margin-top:12px"><button class="success" id="insertUrls" disabled>Insert URLs &amp; complete CSV</button></div>
      <div class="small muted" style="margin-top:8px">Selected images upload directly from your browser to R2. Existing R2 URLs are reused when the filename matches.</div>
    </div>

    <div id="videoPanel" style="display:none">
      <!-- Step A: Generate MP4s from audio -->
      <div style="background:#fafbfc;border:1px solid var(--line);border-radius:10px;padding:14px;margin-bottom:14px">
        <strong style="font-size:13px">Step A — Generate MP4s from your images + audio</strong>
        <div class="small muted" style="margin:5px 0 12px">Pick an audio file from your Audio/ folder and click Generate. MP4s are saved to ToPost/videos/.</div>
        <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:10px">
          <label style="font-size:12px;font-weight:650;white-space:nowrap">🎵 Audio:</label>
          <select id="audioFileSelect" style="flex:1;min-width:180px;padding:7px 10px;border-radius:7px;border:1px solid var(--line);background:#fff;color:var(--ink);font:inherit;font-size:13px">
            <option value="">— scan Audio/ folder —</option>
          </select>
          <button class="secondary" type="button" onclick="scanAudioFiles()" style="white-space:nowrap;padding:8px 11px">🔄 Refresh</button>
        </div>
        <div class="actions" style="margin-bottom:8px">
          <button class="primary" type="button" id="btnGenerate" onclick="generateVideos(false)">⚙️ Generate MP4s</button>
          <button class="secondary" type="button" id="btnRegenerate" onclick="generateVideos(true)">🔁 Overwrite existing</button>
        </div>
        <div id="generateLog" style="display:none;background:#1a1d23;color:#d4f5d4;border-radius:8px;padding:11px 13px;font:12px/1.6 ui-monospace,SFMono-Regular,Consolas,monospace;max-height:200px;overflow-y:auto;white-space:pre-wrap;margin-top:8px"></div>
      </div>
      <!-- Step B: Scan, select, upload -->
      <div class="row" style="flex-wrap:wrap"><div><strong id="videoInstruction">Step B — Select videos for this campaign.</strong><div class="small muted">Files are listed alphabetically. The selected order maps to CSV rows from top to bottom.</div></div><button class="secondary" id="scanVideos">Scan ToPost/videos/</button></div>
      <div class="statline"><span class="count" id="videoAvailableCount">No scan yet</span><span class="count" id="videoSelectedCount">0 selected</span></div>
      <details class="preview-details" id="videoPreview">
        <summary><span id="videoPreviewLabel">Choose videos from ToPost/videos/</span><span class="summary-hint">The selection order matches the CSV rows</span></summary>
        <div id="videoList" class="image-list" style="padding:10px 12px"><div class="empty">Scan the videos folder when your MP4s are ready.</div></div>
      </details>
      <div class="actions" style="margin-top:12px"><button class="success" id="insertVideoUrls" disabled>Insert Video URLs &amp; complete CSV</button></div>
      <div class="small muted" style="margin-top:8px">Only selected videos are uploaded. Existing R2 URLs are reused; new videos are uploaded once and added to image_urls.csv.</div>
    </div>
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
      <div class="field"><label for="topic">Topic / Campaign</label><input id="topic" maxlength="180" placeholder="e.g. The Unspoken Truths About Love" required></div>
      <div class="field"><label for="niche">Niche</label><input id="niche" maxlength="180" placeholder="e.g. Emotional Intelligence &amp; Relationship Psychology" required></div>
      <div class="field hidden" id="customBoardWrap"><input id="customBoardName" type="hidden"><input id="board" type="hidden" value="social"></div>
      <div class="field"><label for="destination">Destination URL (optional)</label><input id="destination" type="url" placeholder="https://example.com/your-page"></div>
      <div class="field"><label for="cta">Optional CTA / extra writing instructions</label><input id="cta" maxlength="300" placeholder="e.g. Encourage readers to explore the full guide"></div>
      <div class="grid2">
        <div class="field"><label for="startAt">Start date &amp; time</label><input id="startAt" type="datetime-local" required></div>
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
let boards=[];  // unused in social tab — boards belong to Pinterest Pins only
let campaignFilter='all';
let campaignItems=[];

function escapeHtml(value){return String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
function formatCampaignDate(value){
  if(!value)return 'Date unavailable';
  try{return new Date(value).toLocaleString([],{year:'numeric',month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'});}
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
    $('campaignList').innerHTML=`<div class="library-empty">${campaignItems.length?'No campaigns match this filter.':'No saved campaigns yet. Select "New Campaign" to create your first one.'}</div>`;
    return;
  }
  $('campaignList').innerHTML=filtered.map(item=>{
    const created=formatCampaignDate(item.created_at);
    const details=[`${item.row_count} posts`,item.board||'No board',`Updated ${formatCampaignDate(item.updated_at)}`].join(' · ');
    let progress=item.status==='draft'?'Content generated · waiting for images':item.status==='ready'?'Images linked · CSV ready to download':item.status==='scheduled'?'Imported into the Pinterest publishing queue':item.status==='completed'?'All tracked Pins have been published':item.status==='failed'?'One or more Pins need attention':'Archived campaign';
    if(item.imported_count)progress+=` · ${item.imported_count} imported`;
    if(item.skipped_count)progress+=` · ${item.skipped_count} skipped`;
    const download=item.has_final_csv?`<a href="/api/social/campaigns/${item.draft_id}/final.csv"><button class="secondary">Final CSV</button></a>`:item.has_content_csv?`<a href="/api/social/campaigns/${item.draft_id}/content.csv"><button class="secondary">Content CSV</button></a>`:'';
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
    const response=await fetch('/api/social/campaigns');const data=await response.json();
    if(!response.ok)throw new Error(data.error||'Could not load campaign history.');
    campaignItems=data.campaigns||[];renderCampaignLibrary();
  }catch(error){$('campaignList').innerHTML=`<div class="library-empty">${escapeHtml(error.message)}</div>`;}
}
async function openSavedCampaign(id){
  clearMessage();
  try{
    const response=await fetch(`/api/social/campaigns/${id}`);const data=await response.json();
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
    const response=await fetch(`/api/social/campaigns/${id}/${archived?'archive':'restore'}`,{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
    const data=await response.json();if(!response.ok)throw new Error(data.error||'Campaign update failed.');
    await loadCampaigns();
  }catch(error){showMessage(error.message,'error');}
}
function showMessage(text,type='ok'){const el=$('message');el.textContent=text;el.className='status '+type;}
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
function loadBoards(){}  // no-op — boards not used in social tab
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
    let desc=String(row.caption||row.description||'').replace(/\s+/g,' ').trim();
    if(!desc)return '';
    desc=desc.replace(/[.!?]+$/,'').trim()+'.';
    return `${desc}\n9:16 portrait. Plain lightly textured paper background, warm off-white to light grey. Text only — no illustrations, no borders, no decorations. ALL text uses the same old-school Courier typewriter monospace font throughout — bold weight for the headline, regular weight for the body. First sentence is the headline in large bold Courier typewriter font WITH a bright solid yellow marker highlight painted behind it. Remaining lines in regular Courier typewriter font, same typeface, left-aligned, generous line spacing, easy to read. Consistent single font family from top to bottom. Minimal and clean. No logos, no URLs, no watermarks.`;
  }).filter(Boolean).join('\n\n');
}
function renderImagePrompts(draft){
  const prompts=buildImagePrompts(draft.rows||[]);
  $('imagePromptsText').value=prompts;
  const count=(draft.rows||[]).filter(row=>String(row.description??'').trim()).length;
  $('imagePromptMeta').textContent=`${count} prompts · one prompt per description, ready for your bulk image generator.`;
  $('downloadImagePrompts').href=`/api/social/campaigns/${draft.draft_id}/image-prompts.txt`;
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
  localStorage.setItem('social_csv_draft_id',draft.draft_id);
  $('emptyState').classList.add('hidden');
  $('contentStage').classList.remove('hidden');
  $('imageStage').classList.remove('hidden');
  $('draftRows').textContent=draft.rows.length+' rows';
  $('draftTopic').textContent=draft.topic;
  $('draftTimezone').textContent=draft.timezone;
  $('contentDownload').href=`/api/social/campaigns/${draft.draft_id}/content.csv`;
  $('contentPreviewLabel').textContent=`Preview generated content · ${draft.rows.length} rows`;
  $('contentPreview').open=false;
  renderContentRows(draft.rows);
  renderImagePrompts(draft);
  $('imageInstruction').textContent=`Choose exactly ${draft.rows.length} image(s) for this CSV.`;
  $('finalStage').classList.toggle('hidden',!draft.final_ready);
  $('finalPreview').open=false;
  updateWorkflow(draft);
  if(draft.final_ready){
    $('finalDownload').href=`/api/social/campaigns/${draft.draft_id}/final.csv`;
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
  const id=localStorage.getItem('social_csv_draft_id');
  if(!id)return;
  try{
    const response=await fetch(`/api/social/campaigns/${id}`);if(!response.ok)return;
    const data=await response.json();if(data.draft)showDraft(data.draft);
  }catch(error){console.warn('Could not restore previous social campaign draft.',error);}
}

async function createCsv(event){
  event.preventDefault();clearMessage();
  const rows=Number($('rowCount').value);const interval=Number($('interval').value);
  if(!Number.isInteger(rows)||rows<1||rows>50){showMessage('Rows must be between 1 and 50.','error');return;}
  if(!Number.isFinite(interval)||interval<0.25||interval>720){showMessage('Interval must be between 0.25 and 720 hours.','error');return;}
  const selectedBoard='social';
  const customBoard=false;
  const boardName='Social Media';
  const destination=$('destination').value.trim();
  if(destination){try{const url=new URL(destination);if(!['http:','https:'].includes(url.protocol))throw new Error();}catch(e){showMessage('Enter a valid destination URL.','error');return;}}
  const payload={
    row_count:rows,topic:$('topic').value.trim(),niche:$('niche').value.trim(),
    board_id:customBoard?'__create_custom_board__':selectedBoard,board:boardName,
    destination_url:destination,cta:$('cta').value.trim(),start_at:$('startAt').value,
    interval_hours:interval,timezone:$('timezone').value
  };
  const button=$('generateBtn');button.disabled=true;button.textContent='Generating…';
  try{
    const response=await fetch('/api/social/create-csv',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
    const data=await response.json();if(!response.ok)throw new Error(data.error||'CSV generation failed.');
    await loadCampaigns();
    // no board reload needed for social tab
    showDraft(data.draft);availableImages=[];$('imageList').innerHTML='<div class="empty">Choose your campaign images from your computer.</div>';$('availableCount').textContent='Not scanned';$('selectedCount').textContent='0 selected';$('insertUrls').disabled=true;
    closeModal();
    const boardNotice=data.board_created?` Pinterest board "${data.draft.board}" was created.`:'';
    showMessage(`Created a new saved campaign with ${data.draft.rows.length} rows.${boardNotice} Your earlier campaigns remain in the library.`,'ok');
    $('contentStage').scrollIntoView({behavior:'smooth',block:'start'});
  }catch(error){showMessage(error.message,'error');}
  finally{button.disabled=false;button.textContent='Generate CSV';}
}

function updateSelectedCount(){
  const selected=document.querySelectorAll('.image-choice:checked').length;
  $('selectedCount').textContent=`${selected} selected`;
  $('insertUrls').disabled=!activeDraft||selected!==activeDraft.rows.length;
  if(activeDraft&&selected!==activeDraft.rows.length){$('insertUrls').title=`Select exactly ${activeDraft.rows.length} images.`;}else{$('insertUrls').title='';}
}
function renderImageChoices(){
  if(!availableImages.length){$('imageList').innerHTML='<div class="empty">Choose JPG, PNG, WebP or another supported image from your computer.</div>';updateSelectedCount();return;}
  const needed=activeDraft.rows.length;
  $('imageList').innerHTML=availableImages.map((image,index)=>`<label class="image-item"><input type="checkbox" class="image-choice" data-index="${index}" ${index<needed?'checked':''}><span class="image-name">${escapeHtml(image.filename)}</span><span class="image-state ${image.hosted?'':'pending'}">${image.hosted?'Already on R2':'Will upload to R2'}</span></label>`).join('');
  document.querySelectorAll('.image-choice').forEach(input=>input.addEventListener('change',updateSelectedCount));
  updateSelectedCount();
}
async function scanImages(){
  if(!activeDraft)return;clearMessage();$('imageFiles').value='';$('imageFiles').click();
}
$('imageFiles').addEventListener('change',()=>{
  if(!activeDraft)return;
  const files=[...($('imageFiles').files||[])].filter(file=>/\.(jpe?g|png|webp|bmp|tiff?)$/i.test(file.name));
  availableImages=files.sort((a,b)=>a.name.localeCompare(b.name,undefined,{numeric:true,sensitivity:'base'})).map(file=>({filename:file.name,hosted:false,file}));
  $('availableCount').textContent=`${availableImages.length} image(s) selected`;
  $('imagePreviewLabel').textContent=`Review selected images · ${availableImages.length} found`;$('imagePreview').open=true;renderImageChoices();
  if(availableImages.length<activeDraft.rows.length)showMessage(`This CSV needs ${activeDraft.rows.length} images, but you selected ${availableImages.length}.`,'warn');
  else if(availableImages.length>activeDraft.rows.length)showMessage(`Selected ${availableImages.length} images. Choose exactly ${activeDraft.rows.length}; the first ${activeDraft.rows.length} are ticked in alphabetical order.`,'ok');
  else showMessage(`Selected exactly ${availableImages.length} images. Review the order, then insert the URLs.`,'ok');
});
function chosenImages(){return [...document.querySelectorAll('.image-choice:checked')].map(el=>availableImages[Number(el.dataset.index)]).filter(Boolean);}
async function uploadBrowserImage(image){
  const presignResponse=await fetch('/api/media/presign',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({filename:image.filename,content_type:image.file.type||'application/octet-stream'})});
  const presign=await presignResponse.json();if(!presignResponse.ok)throw new Error(presign.error||`Could not prepare ${image.filename}.`);
  if(presign.already_hosted)return {filename:image.filename,media_url:presign.media_url};
  const put=await fetch(presign.upload_url,{method:'PUT',headers:{'Content-Type':presign.content_type},body:image.file});
  if(!put.ok)throw new Error(`R2 upload failed for ${image.filename} (HTTP ${put.status}). Check your R2 bucket CORS settings.`);
  const confirm=await fetch('/api/media/confirm',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({filename:image.filename,media_url:presign.media_url})});
  const result=await confirm.json();if(!confirm.ok)throw new Error(result.error||`Could not save ${image.filename} to the media library.`);
  return {filename:image.filename,media_url:presign.media_url};
}
function renderFinalRows(rows){
  $('finalRows').innerHTML=rows.map(row=>`<tr><td>${escapeHtml(row.filename)}</td><td><a href="${escapeHtml(row.media_url)}" target="_blank" rel="noopener">Open R2 URL</a></td><td class="titlecell">${escapeHtml(row.title)}</td><td>${escapeHtml(row.scheduled_at)}<br><span class="muted">${escapeHtml(row.timezone)}</span></td></tr>`).join('');
}
async function insertImageUrls(){
  if(!activeDraft)return;clearMessage();const selected=chosenImages();
  if(selected.length!==activeDraft.rows.length){showMessage(`Select exactly ${activeDraft.rows.length} images before continuing.`,'error');return;}
  const button=$('insertUrls');button.disabled=true;button.textContent='Uploading and inserting URLs…';
  try{
    const uploaded=[];for(let i=0;i<selected.length;i++){button.textContent=`Uploading image ${i+1} of ${selected.length}…`;uploaded.push(await uploadBrowserImage(selected[i]));}
    button.textContent='Inserting URLs…';
    const response=await fetch(`/api/social/campaigns/${activeDraft.draft_id}/insert-images`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({images:uploaded})});
    const data=await response.json();if(!response.ok)throw new Error(data.error||'Could not insert image URLs.');
    activeDraft=data.draft;showDraft(activeDraft);$('imagePreview').open=false;setWorkflowActive('export');showMessage(`Inserted ${data.inserted} verified R2 image URL(s). Your final CSV is ready to download.`,'ok');
    $('finalStage').scrollIntoView({behavior:'smooth',block:'start'});
  }catch(error){showMessage(error.message,'error');button.disabled=false;button.textContent='Insert URLs & complete CSV';}
}
async function importIntoScheduler(){
  if(!activeDraft||!activeDraft.final_ready)return;clearMessage();const button=$('importScheduler');button.disabled=true;button.textContent='Importing…';
  try{
    const response=await fetch(`/api/social/campaigns/${activeDraft.draft_id}/import`,{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
    const data=await response.json();if(!response.ok)throw new Error(data.error||'Import failed.');
    $('importMessage').textContent=`Imported ${data.inserted} Pin(s). Skipped ${data.skipped} already scheduled duplicate(s).`;
    showMessage('Final CSV imported into your existing Pinterest Scheduler queue.','ok');button.textContent='Imported';
  }catch(error){showMessage(error.message,'error');button.disabled=false;button.textContent='Publishing disabled';}
}

// ── Video tab ─────────────────────────────────────────────────────────────
let availableVideos=[];
function switchMediaTab(tab){
  const isImages=tab==='images';
  $('imagePanel').style.display=isImages?'':'none';
  $('videoPanel').style.display=isImages?'none':'';
  $('tabImages').classList.toggle('active',isImages);
  $('tabVideos').classList.toggle('active',!isImages);
  if(!isImages){scanAudioFiles();scanVideos();}
}
async function scanAudioFiles(){
  if(!activeDraft)return;
  try{
    const res=await fetch(`/api/social/campaigns/${activeDraft.draft_id}/scan-audio`);
    const data=await res.json();
    const sel=$('audioFileSelect');
    const prev=sel.value;
    sel.innerHTML='<option value="">— choose audio file —</option>';
    (data.files||[]).forEach(f=>{const opt=document.createElement('option');opt.value=f;opt.textContent=f;sel.appendChild(opt);});
    if(prev&&[...sel.options].some(o=>o.value===prev))sel.value=prev;
    else if(data.files&&data.files.length===1)sel.value=data.files[0];
  }catch(e){console.warn('Audio scan failed',e);}
}
async function generateVideos(overwrite){
  if(!activeDraft)return;
  const audio=$('audioFileSelect').value;
  if(!audio){showMessage('Select an audio file from the Audio/ folder first.','error');return;}
  const log=$('generateLog');
  log.style.display='block';log.textContent='';
  $('btnGenerate').disabled=true;$('btnRegenerate').disabled=true;
  try{
    const res=await fetch(`/api/social/campaigns/${activeDraft.draft_id}/generate-videos`,{
      method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({audio_file:audio,overwrite})
    });
    if(!res.ok){const d=await res.json();throw new Error(d.error||'Generate failed.');}
    const reader=res.body.getReader();const dec=new TextDecoder();let buf='';
    while(true){
      const{done,value}=await reader.read();if(done)break;
      buf+=dec.decode(value,{stream:true});
      const lines=buf.split('\n');buf=lines.pop();
      for(const line of lines){
        if(!line.startsWith('data: '))continue;
        const msg=line.slice(6);
        if(msg.startsWith('__done__:')){
          const code=msg.split(':')[1];
          log.textContent+=(code==='0'?'\n✅ Done! Scanning for new videos…':'\n❌ Exited with code '+code);
          log.scrollTop=log.scrollHeight;
          if(code==='0')scanVideos();
          return;
        }
        log.textContent+=msg+'\n';log.scrollTop=log.scrollHeight;
      }
    }
  }catch(e){showMessage(e.message,'error');}
  finally{$('btnGenerate').disabled=false;$('btnRegenerate').disabled=false;}
}
function updateVideoSelectedCount(){
  const checked=document.querySelectorAll('.video-choice:checked').length;
  $('videoSelectedCount').textContent=`${checked} selected`;
  $('insertVideoUrls').disabled=!activeDraft||checked!==activeDraft.rows.length;
}
function renderVideoChoices(){
  if(!availableVideos.length){$('videoList').innerHTML='<div class="empty">No MP4 files found in ToPost/videos/. Run make_videos.py first.</div>';updateVideoSelectedCount();return;}
  const needed=activeDraft.rows.length;
  $('videoList').innerHTML=availableVideos.map((v,i)=>`<label class="image-item"><input type="checkbox" class="video-choice" data-index="${i}" ${i<needed?'checked':''}><span class="image-name">${escapeHtml(v.filename)}</span><span class="image-state ${v.hosted?'':'pending'}">${v.hosted?'Already on R2':'Will upload to R2'}</span></label>`).join('');
  document.querySelectorAll('.video-choice').forEach(el=>el.addEventListener('change',updateVideoSelectedCount));
  updateVideoSelectedCount();
}
async function scanVideos(){
  if(!activeDraft)return;clearMessage();$('scanVideos').disabled=true;$('scanVideos').textContent='Scanning…';
  try{
    const response=await fetch(`/api/social/campaigns/${activeDraft.draft_id}/scan-videos`);
    const data=await response.json();if(!response.ok)throw new Error(data.error||'Could not scan videos folder.');
    availableVideos=data.videos||[];
    $('videoAvailableCount').textContent=`${availableVideos.length} video(s) found`;
    $('videoPreviewLabel').textContent=`Review available videos · ${availableVideos.length} found`;
    $('videoPreview').open=true;
    renderVideoChoices();
    if(availableVideos.length<activeDraft.rows.length)showMessage(`Need ${activeDraft.rows.length} videos but found ${availableVideos.length}. Run make_videos.py to generate more.`,'warn');
    else if(availableVideos.length>activeDraft.rows.length)showMessage(`Found ${availableVideos.length} videos. Select exactly ${activeDraft.rows.length}.`,'ok');
    else showMessage(`Found exactly ${availableVideos.length} videos. Review the order, then insert the URLs.`,'ok');
  }catch(error){showMessage(error.message,'error');}
  finally{$('scanVideos').disabled=false;$('scanVideos').textContent='Scan ToPost/videos/';}
}
function chosenVideos(){return[...document.querySelectorAll('.video-choice:checked')].map(el=>availableVideos[Number(el.dataset.index)]).filter(Boolean);}
async function insertVideoUrls(){
  if(!activeDraft)return;clearMessage();const selected=chosenVideos();
  if(selected.length!==activeDraft.rows.length){showMessage(`Select exactly ${activeDraft.rows.length} videos before continuing.`,'error');return;}
  const button=$('insertVideoUrls');button.disabled=true;button.textContent='Uploading and inserting URLs…';
  try{
    const response=await fetch(`/api/social/campaigns/${activeDraft.draft_id}/insert-videos`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({filenames:selected.map(v=>v.filename)})});
    const data=await response.json();if(!response.ok)throw new Error(data.error||'Could not insert video URLs.');
    activeDraft=data.draft;showDraft(activeDraft);$('videoPreview').open=false;setWorkflowActive('export');
    showMessage(`Inserted ${data.inserted} verified R2 video URL(s). Your final CSV is ready to download.`,'ok');
    $('finalStage').scrollIntoView({behavior:'smooth',block:'start'});
  }catch(error){showMessage(error.message,'error');button.disabled=false;button.textContent='Insert Video URLs & complete CSV';}
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
$('board').addEventListener('change',toggleCustomBoardField);
$('showImages').addEventListener('click',()=>$('imageStage').scrollIntoView({behavior:'smooth',block:'start'}));
$('scanImages').addEventListener('click',scanImages);
$('insertUrls').addEventListener('click',insertImageUrls);
$('scanVideos').addEventListener('click',scanVideos);
$('insertVideoUrls').addEventListener('click',insertVideoUrls);
$('importScheduler').addEventListener('click',importIntoScheduler);
setDefaultStart();updateWorkflow(null);loadBoards();checkStatus();loadCampaigns();
</script>
</body>
</html>'''


@app.get("/social")
def social_home():
    return render_template_string(SOCIAL_HTML)


# ---------------------------------------------------------------------------
# SOCIAL CAMPAIGN LIBRARY HELPERS
# ---------------------------------------------------------------------------

def _social_campaigns():
    campaigns = []
    for draft in pins.all_campaign_drafts():
        if not _is_social(draft):
            continue
        campaign_id = draft.get("draft_id", "")
        if not pins.DRAFT_ID_RE.fullmatch(str(campaign_id)):
            continue
        campaigns.append({
            "draft_id": campaign_id,
            "topic": draft.get("topic") or "Untitled campaign",
            "niche": draft.get("niche", ""),
            "board": draft.get("board", "Social Media"),
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


# ---------------------------------------------------------------------------
# SOCIAL API ENDPOINTS — mirrors the Pinterest /api/... surface
# ---------------------------------------------------------------------------

@app.get("/api/social/campaigns")
def social_campaign_list():
    return jsonify({"campaigns": _social_campaigns()})


@app.get("/api/social/campaigns/<draft_id>")
def social_campaign_get(draft_id):
    try:
        draft = pins.load_draft(draft_id)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    if not draft or not _is_social(draft):
        return jsonify({"error": "Social campaign not found."}), 404
    return jsonify({"draft": draft})


@app.post("/api/social/campaigns/<draft_id>/archive")
def social_archive_campaign(draft_id):
    try:
        draft = pins.load_draft(draft_id)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    if not draft or not _is_social(draft):
        return jsonify({"error": "Social campaign not found."}), 404
    draft["archived"] = True
    draft["archived_at"] = datetime.now(timezone.utc).isoformat()
    pins.save_draft(draft)
    return jsonify({"success": True})


@app.post("/api/social/campaigns/<draft_id>/restore")
def social_restore_campaign(draft_id):
    try:
        draft = pins.load_draft(draft_id)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    if not draft or not _is_social(draft):
        return jsonify({"error": "Social campaign not found."}), 404
    draft["archived"] = False
    draft.pop("archived_at", None)
    pins.save_draft(draft)
    return jsonify({"success": True})


@app.post("/api/social/create-csv")
def social_create_csv():
    data = request.get_json(silent=True) or {}
    topic = str(data.get("topic", "")).strip()
    niche = str(data.get("niche", "")).strip()
    cta = str(data.get("cta", "")).strip()
    board_id = "social"
    board = "Social Media"
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
    if destination_url:
        parsed = urlsplit(destination_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return jsonify({"error": "Destination URL must begin with http:// or https://."}), 400
    if not start_at or timezone_name not in pins.TIMEZONES:
        return jsonify({"error": "Enter a start date/time and supported timezone."}), 400
    if not os.getenv("OPENROUTER_API_KEY", "").strip():
        return jsonify({"error": "OpenRouter API key is missing. Add OPENROUTER_API_KEY to .env first."}), 400

    try:
        schedules = pins.create_schedule(start_at, interval_hours, row_count, timezone_name)
        generated, cached = pins.generate_campaign_content(row_count, topic, niche, cta, mode="social")
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
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
            "caption": "",  # filled in below
        })

    # Generate viral captions for each row (used for image prompts + Facebook plain-text posts)
    captions = _generate_social_captions(rows, topic, niche)
    for row, cap in zip(rows, captions):
        row["caption"] = cap

    draft = {
        "draft_id": draft_id,
        "content_type": "social",
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
        "social_render": None,
    }
    pins.save_draft(draft)
    # Write Publer-format content CSV for social campaigns.
    _write_social_content_csv(draft)
    return jsonify({
        "draft": draft,
        "cached": cached,
        "board_created": board_created,
        "content_csv_url": f"/api/social/campaigns/{draft_id}/content.csv",
    })


@app.get("/api/social/campaigns/<draft_id>/content.csv")
def social_download_content_csv(draft_id):
    try:
        draft = pins.load_draft(draft_id)
        draft_p = pins.draft_paths(draft_id)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    if not draft or not _is_social(draft):
        return jsonify({"error": "Social campaign not found."}), 404
    _write_social_content_csv(draft)
    return send_file(
        draft_p["content_csv"], as_attachment=True,
        download_name=f"social_content_{draft_id[:8]}.csv",
        mimetype="text/csv",
    )


@app.get("/api/social/campaigns/<draft_id>/final.csv")
def social_download_final_csv(draft_id):
    try:
        draft = pins.load_draft(draft_id)
        draft_p = pins.draft_paths(draft_id)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    if not draft or not _is_social(draft):
        return jsonify({"error": "Social campaign not found."}), 404
    if not draft.get("final_ready"):
        return jsonify({"error": "Final CSV is not ready. Insert image URLs first."}), 404
    _write_social_final_csv_publer(draft)
    return send_file(
        draft_p["final_csv"], as_attachment=True,
        download_name=f"social_campaign_{draft_id[:8]}.csv",
        mimetype="text/csv",
    )


@app.get("/api/social/campaigns/<draft_id>/image-prompts.txt")
def social_download_image_prompts(draft_id):
    try:
        draft = pins.load_draft(draft_id)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    if not draft or not _is_social(draft):
        return jsonify({"error": "Social campaign not found."}), 404
    prompt_text = _build_social_image_prompts(draft.get("rows", []))
    if not prompt_text:
        return jsonify({"error": "This campaign has no descriptions to turn into image prompts."}), 400
    return Response(
        prompt_text + "\n",
        mimetype="text/plain; charset=utf-8",
        headers={
            "Content-Disposition": (
                f'attachment; filename="social_image_prompts_{draft_id[:8]}.txt"'
            )
        },
    )


VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".webm"}
VIDEOS_DIR_NAME = "videos"  # subfolder inside TO_POST


def _get_local_videos():
    """Return list of dicts {filename, hosted} for MP4s in ToPost/videos/."""
    videos_dir = pins.TO_POST / VIDEOS_DIR_NAME
    if not videos_dir.is_dir():
        return []
    manifest = pins.read_manifest()
    files = sorted(
        [p for p in videos_dir.iterdir() if p.is_file() and p.suffix.lower() in VIDEO_EXTS],
        key=lambda p: p.name.casefold(),
    )
    return [{"filename": p.name, "hosted": p.name in manifest} for p in files]


@app.get("/api/social/campaigns/<draft_id>/scan-audio")
def social_scan_audio(draft_id):
    """Return list of audio files in the Audio/ subfolder."""
    try:
        draft = pins.load_draft(draft_id)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    if not draft or not _is_social(draft):
        return jsonify({"error": "Social campaign not found."}), 404
    audio_dir = ROOT / "Audio"
    audio_exts = {".mp3", ".wav", ".aac", ".m4a", ".ogg", ".flac"}
    files = []
    if audio_dir.exists():
        files = sorted([
            f.name for f in audio_dir.iterdir()
            if f.is_file() and f.suffix.lower() in audio_exts
        ])
    return jsonify({"files": files})


@app.post("/api/social/campaigns/<draft_id>/generate-videos")
def social_generate_videos(draft_id):
    """Run make_videos.py in the background and stream progress via SSE."""
    import shlex
    import subprocess
    try:
        draft = pins.load_draft(draft_id)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    if not draft or not _is_social(draft):
        return jsonify({"error": "Social campaign not found."}), 404

    data = request.get_json(force=True) or {}
    audio_file = str(data.get("audio_file", "")).strip()
    overwrite = bool(data.get("overwrite", False))

    if not audio_file:
        return jsonify({"error": "No audio file specified."}), 400
    audio_path = ROOT / "Audio" / audio_file
    if not audio_path.exists():
        return jsonify({"error": f"Audio file not found: {audio_file}"}), 400

    make_videos_path = ROOT / "make_videos.py"
    if not make_videos_path.exists():
        return jsonify({"error": "make_videos.py not found in project folder."}), 400

    cmd = [sys.executable, str(make_videos_path),
           "--audio", str(audio_path), "--no-upload"]
    if overwrite:
        cmd.append("--overwrite")

    def stream():
        shell_cmd = " ".join(shlex.quote(c) for c in cmd)
        proc = subprocess.Popen(
            shell_cmd, shell=True, executable="/bin/bash",
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, cwd=str(Path.home()),
        )
        for line in proc.stdout:
            yield f"data: {line.rstrip()}\n\n"
        proc.wait()
        yield f"data: __done__:{proc.returncode}\n\n"

    return Response(
        stream(),
        mimetype="text/event-stream",
        headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
    )


@app.get("/api/social/campaigns/<draft_id>/scan-videos")
def social_scan_videos(draft_id):
    try:
        draft = pins.load_draft(draft_id)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    if not draft or not _is_social(draft):
        return jsonify({"error": "Social campaign not found."}), 404
    return jsonify({"videos": _get_local_videos(), "required": len(draft.get("rows", []))})


@app.post("/api/social/campaigns/<draft_id>/insert-videos")
def social_insert_videos(draft_id):
    try:
        draft = pins.load_draft(draft_id)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    if not draft or not _is_social(draft):
        return jsonify({"error": "Social campaign not found."}), 404
    if draft.get("final_ready"):
        return jsonify({"error": "URLs have already been inserted for this draft."}), 409

    data = request.get_json(silent=True) or {}
    filenames = data.get("filenames")
    needed = len(draft.get("rows", []))
    if not isinstance(filenames, list) or len(filenames) != needed:
        return jsonify({"error": f"Select exactly {needed} videos."}), 400
    if len(set(filenames)) != len(filenames):
        return jsonify({"error": "The same video cannot be selected more than once."}), 400

    videos_dir = pins.TO_POST / VIDEOS_DIR_NAME
    selected_paths = []
    for filename in filenames:
        if not isinstance(filename, str) or Path(filename).name != filename:
            return jsonify({"error": "Invalid video filename."}), 400
        video_path = videos_dir / filename
        if not video_path.is_file() or video_path.suffix.lower() not in VIDEO_EXTS:
            return jsonify({"error": f"Supported video not found in ToPost/videos/: {filename}"}), 400
        selected_paths.append(video_path)

    final_rows = []
    try:
        with pins.UPLOAD_LOCK:
            uploaded = pins.read_manifest()
            for index, (row, video_path) in enumerate(zip(draft["rows"], selected_paths), start=1):
                media_url = uploaded.get(video_path.name, "")
                if not media_url:
                    print(f"Uploading social campaign video to R2: {video_path.name}")
                    media_url, size, content_type = pins.upload_images.upload_image(video_path)
                    uploaded[video_path.name] = media_url
                    pins.save_manifest(uploaded)
                    print(f"R2 upload complete: {video_path.name} ({size} bytes, {content_type})")
                if not media_url.startswith("https://"):
                    raise RuntimeError(f"Invalid public R2 URL for {video_path.name}.")
                final_rows.append({
                    "row_number": index,
                    "filename": video_path.name,
                    "media_url": media_url,
                    "title": row["title"],
                    "description": row["description"],
                    "caption": row.get("caption", "") or row["description"],
                    "keywords": row.get("keywords", []),
                    "destination_url": pins.make_unique_destination_url(
                        draft.get("destination_url", ""), row.get("title", "")
                    ),
                    "scheduled_at": row["scheduled_at"],
                    "timezone": draft.get("timezone", "UTC"),
                })
        draft["final_rows"] = final_rows
        draft["final_ready"] = True
        draft["finalized_at"] = datetime.now(timezone.utc).isoformat()
        pins.save_draft(draft)
        _write_social_final_csv_publer(draft)
    except Exception as exc:
        return jsonify({"error": f"Video URL insertion stopped: {exc}"}), 502

    return jsonify({"draft": draft, "inserted": len(final_rows)})


@app.get("/api/social/campaigns/<draft_id>/scan-images")
def social_scan_images(draft_id):
    try:
        draft = pins.load_draft(draft_id)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    if not draft or not _is_social(draft):
        return jsonify({"error": "Social campaign not found."}), 404
    return jsonify({"images": pins.get_local_images(), "required": len(draft.get("rows", []))})


@app.post("/api/social/campaigns/<draft_id>/insert-images")
def social_insert_images(draft_id):
    try:
        draft = pins.load_draft(draft_id)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    if not draft or not _is_social(draft):
        return jsonify({"error": "Social campaign not found."}), 404
    if draft.get("final_ready"):
        return jsonify({"error": "Image URLs have already been inserted for this draft."}), 409

    data = request.get_json(silent=True) or {}
    images = data.get("images")
    needed = len(draft.get("rows", []))
    if not isinstance(images, list) or len(images) != needed:
        return jsonify({"error": f"Select exactly {needed} images."}), 400
    filenames = [item.get("filename") for item in images if isinstance(item, dict)]
    if len(filenames) != needed or len(set(filenames)) != needed:
        return jsonify({"error": "Invalid or duplicate image selection."}), 400

    public_base = os.getenv("R2_PUBLIC_URL", "").rstrip("/")
    final_rows = []
    try:
        for index, (row, item) in enumerate(zip(draft["rows"], images), start=1):
            filename, media_url = item.get("filename"), item.get("media_url")
            if not isinstance(filename, str) or Path(filename).name != filename or not isinstance(media_url, str) or not public_base or not media_url.startswith(public_base + "/"):
                raise ValueError("An image URL is missing or does not belong to your configured R2 public domain.")
            final_rows.append({
                "row_number": index, "filename": filename, "media_url": media_url,
                "title": row["title"], "description": row["description"],
                "caption": row.get("caption", "") or row["description"],
                "keywords": row.get("keywords", []),
                "destination_url": pins.make_unique_destination_url(draft.get("destination_url", ""), row.get("title", "")),
                "scheduled_at": row["scheduled_at"], "timezone": draft["timezone"],
            })
        draft["final_rows"] = final_rows
        draft["final_ready"] = True
        draft["finalized_at"] = datetime.now(timezone.utc).isoformat()
        pins.save_draft(draft)
        # Write Publer-format final CSV for social campaigns.
        _write_social_final_csv_publer(draft)
    except Exception as exc:
        return jsonify({"error": f"Image URL insertion stopped: {exc}"}), 502

    return jsonify({"inserted": len(final_rows), "draft": draft})


@app.post("/api/social/campaigns/<draft_id>/import")
def social_import_campaign(draft_id):
    return jsonify({"error": "Publishing and scheduler imports are disabled in this Content Studio deployment. Download the CSV instead."}), 410


# ---------------------------------------------------------------------------
# SOCIAL API — capabilities & advanced render endpoints (preserved as-is)
# ---------------------------------------------------------------------------

@app.get("/api/social/capabilities")
def social_capabilities():
    try:
        import PIL  # noqa: F401
        pillow = True
    except Exception:
        pillow = False
    return jsonify({"pillow": pillow, "ffmpeg": social.check_ffmpeg()})


def _save_uploaded_file(file_storage, target_dir: Path, allowed_extensions: set[str]) -> Path:
    original = secure_filename(file_storage.filename or "")
    suffix = Path(original).suffix.lower()
    if not original or suffix not in allowed_extensions:
        raise ValueError(f"Unsupported file type: {original or 'unnamed file'}")
    target_dir.mkdir(parents=True, exist_ok=True)
    dest = target_dir / f"{uuid.uuid4().hex}{suffix}"
    file_storage.save(dest)
    if not dest.is_file() or dest.stat().st_size == 0:
        dest.unlink(missing_ok=True)
        raise ValueError(f"Uploaded file was empty: {original}")
    return dest


def _asset_display_path(path: Path) -> str:
    return str(path.resolve().relative_to(ROOT.resolve()))


def _resolve_social_asset(relative: str) -> Path:
    path = (ROOT / relative).resolve()
    base = SOCIAL_ASSET_DIR.resolve()
    if path != base and base not in path.parents:
        raise ValueError("Invalid saved social asset path.")
    if not path.is_file():
        raise FileNotFoundError(f"Saved asset is missing: {path.name}")
    return path


def _safe_title_filename(value: str, index: int, suffix: str, campaign_id: str = "", render_id: str = "") -> str:
    slug = pins.title_to_slug(value)[:72].strip("-") or "social-card"
    campaign_part = campaign_id[:8] if campaign_id else "campaign"
    render_part = re.sub(r"[^a-zA-Z0-9]", "", render_id)[-10:] or uuid.uuid4().hex[:6]
    return f"social_{campaign_part}_{render_part}_{index:03d}_{slug}{suffix}"


def _publer_date(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError as exc:
        raise ValueError(f"Invalid scheduled date in campaign: {value}") from exc
    return parsed.strftime("%Y/%m/%d %H:%M")


@app.post("/api/social/campaigns/<draft_id>/render")
def social_render_campaign(draft_id):
    try:
        draft = pins.load_draft(draft_id)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    if not draft or not _is_social(draft):
        return jsonify({"error": "Social campaign not found."}), 404

    background_uploads = [item for item in request.files.getlist("backgrounds") if item and item.filename]
    audio_upload = request.files.get("audio")
    background_url = str(request.form.get("background_url", "")).strip()
    size = str(request.form.get("size", "portrait")).strip()
    image_side = str(request.form.get("image_side", "right")).strip().lower()
    logo_url = str(request.form.get("logo_url", social.DEFAULT_LOGO_URL)).strip() or social.DEFAULT_LOGO_URL
    try:
        duration = int(request.form.get("duration", 8))
        quality = int(request.form.get("quality", 95))
    except (TypeError, ValueError):
        return jsonify({"error": "Video duration and JPG quality must be numbers."}), 400

    quote_type = str(draft.get("quote_type", "two_part")).strip().lower()
    if quote_type not in {"two_part", "paper"}:
        quote_type = "two_part"

    row_count = len(draft.get("rows", []))
    if not row_count:
        return jsonify({"error": "This campaign has no content rows."}), 400
    if size not in social.SIZES:
        return jsonify({"error": "Choose square, portrait or story format."}), 400
    if image_side not in {"left", "right"}:
        return jsonify({"error": "Choose left or right text-panel position."}), 400
    if not 3 <= duration <= 45 or not 80 <= quality <= 100:
        return jsonify({"error": "MP4 duration must be 3–45 seconds and JPG quality 80–100."}), 400
    if logo_url:
        parsed_logo = urlsplit(logo_url)
        if parsed_logo.scheme not in {"http", "https"} or not parsed_logo.netloc:
            return jsonify({"error": "Logo URL must be a valid http:// or https:// URL."}), 400

    campaign_asset_dir = SOCIAL_ASSET_DIR / draft_id
    backgrounds_dir = campaign_asset_dir / "backgrounds"
    audio_dir = campaign_asset_dir / "audio"
    settings = draft.get("social_settings") or {}

    try:
        if background_uploads:
            if len(background_uploads) not in {1, row_count}:
                return jsonify({"error": f"Upload either one background for all {row_count} rows, or exactly {row_count} backgrounds (one per row)."}), 400
            background_paths = [
                _save_uploaded_file(item, backgrounds_dir, IMAGE_EXTENSIONS | VIDEO_EXTENSIONS)
                for item in background_uploads
            ]
            backgrounds = [_asset_display_path(path) for path in background_paths]
        elif settings.get("backgrounds"):
            backgrounds = list(settings["backgrounds"])
        elif background_url:
            parsed_bg = urlsplit(background_url)
            if parsed_bg.scheme not in {"http", "https"} or not parsed_bg.netloc:
                return jsonify({"error": "Background URL must be a valid http:// or https:// URL."}), 400
            backgrounds = [background_url]
        else:
            backgrounds = [social.DEFAULT_BG_URL]

        if len(backgrounds) not in {1, row_count}:
            return jsonify({"error": "Saved backgrounds must contain one item or one item per campaign row."}), 400

        audio_path = None
        if audio_upload and audio_upload.filename:
            saved_audio = _save_uploaded_file(audio_upload, audio_dir, AUDIO_EXTENSIONS)
            audio_path = _asset_display_path(saved_audio)
        elif settings.get("audio"):
            audio_path = str(settings["audio"])

        resolved_backgrounds = [
            str(_resolve_social_asset(item)) if not str(item).startswith(("http://", "https://")) else str(item)
            for item in backgrounds
        ]
        resolved_audio = str(_resolve_social_asset(audio_path)) if audio_path else None

        any_video = any(social.is_video(item) for item in resolved_backgrounds)
        if any_video and not resolved_audio:
            return jsonify({"error": "A video background needs an audio track. Upload an MP3, WAV, M4A or other supported audio file."}), 400
        if any_video and not social.check_ffmpeg():
            return jsonify({"error": "FFmpeg was not found on PATH. Image backgrounds can still produce JPG cards, but video cards require FFmpeg."}), 400

        render_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "_" + uuid.uuid4().hex[:6]
        render_dir = SOCIAL_OUTPUT_DIR / draft_id / render_id
        render_dir.mkdir(parents=True, exist_ok=True)
        files = []
        errors = []

        for index, row in enumerate(draft["rows"], start=1):
            background = resolved_backgrounds[0] if len(resolved_backgrounds) == 1 else resolved_backgrounds[index - 1]
            video = social.is_video(background)
            extension = ".mp4" if video else ".jpg"
            filename = _safe_title_filename(row.get("title", ""), index, extension, draft_id, render_id)
            destination = render_dir / filename
            try:
                social.render_card(
                    title=row.get("title", ""),
                    description=row.get("description", ""),
                    background=background,
                    output_path=destination,
                    audio_path=resolved_audio if video else None,
                    size=size,
                    image_side=image_side,
                    duration=duration,
                    quality=quality,
                    logo_url=logo_url,
                    quote_style=quote_type,
                )
                files.append({"row_number": index, "title": row.get("title", ""), "filename": filename, "type": "MP4 video" if video else "JPG image"})
            except Exception as exc:
                errors.append(f"Row {index} ({row.get('title', '')}): {exc}")

        if not files:
            return jsonify({"error": "No social cards could be generated.", "errors": errors}), 502

        zip_path = render_dir / f"social_cards_{draft_id[:8]}.zip"
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for item in files:
                file_path = render_dir / item["filename"]
                archive.write(file_path, arcname=item["filename"])

        draft["social_settings"] = {
            "backgrounds": backgrounds,
            "audio": audio_path,
            "size": size,
            "image_side": image_side,
            "duration": duration,
            "quality": quality,
            "logo_url": logo_url,
            "quote_type": quote_type,
        }
        draft["social_render"] = {
            "render_id": render_id,
            "rendered_at": datetime.now(timezone.utc).isoformat(),
            "success_count": len(files),
            "failed_count": len(errors),
            "files": files,
            "errors": errors,
            "zip_relative": _asset_display_path(zip_path),
            "output_folder": _asset_display_path(render_dir),
            "publer_csv_relative": "",
        }
        pins.save_draft(draft)
        return jsonify({
            "draft": draft,
            "success_count": len(files),
            "failed_count": len(errors),
            "files": files,
            "errors": errors,
            "zip_url": f"/api/social/campaigns/{draft_id}/download.zip",
            "output_folder": _asset_display_path(render_dir),
        })
    except Exception as exc:
        return jsonify({"error": f"Social card generation failed: {exc}"}), 502


def _resolve_rendered_file(draft_id: str, filename: str) -> Path:
    if not isinstance(filename, str) or Path(filename).name != filename:
        raise ValueError("Invalid rendered image filename.")
    draft = pins.load_draft(draft_id)
    if not draft or not _is_social(draft):
        raise FileNotFoundError("Social campaign not found.")
    render_info = draft.get("social_render") or {}
    render_dir = (ROOT / str(render_info.get("output_folder", ""))).resolve()
    base = SOCIAL_OUTPUT_DIR.resolve()
    if base not in render_dir.parents or not render_dir.is_dir():
        raise FileNotFoundError("Generated campaign folder could not be found.")
    path = (render_dir / filename).resolve()
    if render_dir not in path.parents or not path.is_file():
        raise FileNotFoundError(f"Generated image could not be found: {filename}")
    return path


def _write_publer_csv(draft: dict, render_dir: Path) -> Path:
    render_info = draft.get("social_render") or {}
    files = render_info.get("files") or []
    by_row = {}
    for index, item in enumerate(files, start=1):
        row_number = int(item.get("row_number") or index)
        by_row[row_number] = item

    rows = draft.get("rows") or []
    if len(by_row) != len(rows):
        raise ValueError("Every campaign row must have one successfully generated image before creating the Publer CSV.")

    output_rows = []
    base_link = str(draft.get("destination_url") or "").strip()
    for index, row in enumerate(rows, start=1):
        media = by_row.get(index) or {}
        media_url = str(media.get("media_url") or "").strip()
        if not media_url.startswith("https://"):
            raise ValueError(f"The image for campaign row {index} has no verified public R2 URL.")
        title = str(row.get("title") or "").strip()
        description = str(row.get("description") or "").strip()
        link = pins.make_unique_destination_url(base_link, title) if base_link else ""
        output_rows.append({
            PUBLER_COLUMNS[0]: _publer_date(row.get("scheduled_at", "")),
            PUBLER_COLUMNS[1]: description,
            PUBLER_COLUMNS[2]: link,
            PUBLER_COLUMNS[3]: media_url,
            PUBLER_COLUMNS[4]: title,
            PUBLER_COLUMNS[5]: "",
            PUBLER_COLUMNS[6]: _social_alt_text(row),
            PUBLER_COLUMNS[7]: "",
            PUBLER_COLUMNS[8]: "",
            PUBLER_COLUMNS[9]: "",
            PUBLER_COLUMNS[10]: "",
        })

    slug = pins.title_to_slug(draft.get("topic", "social-campaign"))[:48] or "social-campaign"
    output_path = render_dir / f"publer_{slug}_{draft['draft_id'][:8]}.csv"
    temp_path = output_path.with_suffix(".csv.tmp")
    with temp_path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=PUBLER_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(output_rows)
    os.replace(temp_path, output_path)
    render_info["publer_csv_relative"] = _asset_display_path(output_path)
    render_info["publer_csv_created_at"] = datetime.now(timezone.utc).isoformat()
    draft["social_render"] = render_info
    pins.save_draft(draft)
    return output_path


@app.post("/api/social/campaigns/<draft_id>/publer-csv")
def social_upload_images_and_build_publer_csv(draft_id):
    try:
        draft = pins.load_draft(draft_id)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    if not draft or not _is_social(draft):
        return jsonify({"error": "Social campaign not found."}), 404

    render_info = draft.get("social_render") or {}
    files = render_info.get("files") or []
    if not files:
        return jsonify({"error": "Generate your social cards first."}), 400
    if any(Path(str(item.get("filename", ""))).suffix.lower() in VIDEO_EXTENSIONS for item in files):
        return jsonify({"error": "The Publer export currently supports the JPG image-card workflow. MP4/video upload and CSV handling will be added in the next video phase. Your generated video files remain saved locally."}), 400
    if any(Path(str(item.get("filename", ""))).suffix.lower() not in IMAGE_EXTENSIONS for item in files):
        return jsonify({"error": "The render contains an unsupported media file. Only finished image cards can be exported in this phase."}), 400

    required = ["R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET_NAME", "R2_ENDPOINT_URL", "R2_PUBLIC_URL"]
    missing = [key for key in required if not os.getenv(key, "").strip()]
    if missing:
        return jsonify({"error": "R2 configuration is incomplete. Missing: " + ", ".join(missing)}), 400

    render_dir = (ROOT / str(render_info.get("output_folder", ""))).resolve()
    output_base = SOCIAL_OUTPUT_DIR.resolve()
    if output_base not in render_dir.parents or not render_dir.is_dir():
        return jsonify({"error": "The generated image folder could not be found. Please generate the cards again."}), 404

    staging_dir = SOCIAL_ASSET_DIR / draft_id / "r2_staging"
    staging_dir.mkdir(parents=True, exist_ok=True)
    uploaded_now = 0
    try:
        for index, item in enumerate(files, start=1):
            if item.get("media_url", "").startswith("https://"):
                continue
            filename = str(item.get("filename", ""))
            source_path = _resolve_rendered_file(draft_id, filename)
            staged_name = f"social_{draft_id[:12]}_{filename}"
            staged_path = staging_dir / staged_name
            shutil.copy2(source_path, staged_path)
            try:
                public_url, size_bytes, content_type = pins.upload_images.upload_image(staged_path)
            finally:
                staged_path.unlink(missing_ok=True)
            if not str(public_url).startswith("https://"):
                raise RuntimeError(f"R2 returned an invalid public URL for {filename}.")
            item["media_url"] = public_url
            item["r2_size_bytes"] = int(size_bytes)
            item["r2_content_type"] = str(content_type)
            item["r2_uploaded_at"] = datetime.now(timezone.utc).isoformat()
            uploaded_now += 1
            draft["social_render"] = render_info
            pins.save_draft(draft)

        csv_path = _write_publer_csv(draft, render_dir)
        render_info["r2_uploaded"] = True
        draft["social_render"] = render_info
        pins.save_draft(draft)
    except Exception as exc:
        draft["social_render"] = render_info
        pins.save_draft(draft)
        return jsonify({"error": f"R2 upload / Publer CSV creation failed: {exc}", "uploaded_count": sum(1 for item in files if item.get("media_url"))}), 502

    return jsonify({
        "draft": draft,
        "uploaded_count": sum(1 for item in files if item.get("media_url")),
        "uploaded_now": uploaded_now,
        "row_count": len(draft.get("rows", [])),
        "csv_url": f"/api/social/campaigns/{draft_id}/publer-download.csv",
        "output_folder": render_info.get("output_folder", ""),
    })


@app.get("/api/social/campaigns/<draft_id>/publer-download.csv")
def social_download_publer_csv(draft_id):
    try:
        draft = pins.load_draft(draft_id)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    if not draft or not _is_social(draft):
        return jsonify({"error": "Social campaign not found."}), 404
    render_info = draft.get("social_render") or {}
    relative = render_info.get("publer_csv_relative")
    if not relative:
        return jsonify({"error": "Create the Publer CSV after uploading your generated images to R2."}), 404
    path = (ROOT / relative).resolve()
    base = SOCIAL_OUTPUT_DIR.resolve()
    if base not in path.parents or not path.is_file():
        return jsonify({"error": "Publer CSV could not be found. Rebuild it from the campaign."}), 404
    topic_slug = pins.title_to_slug(draft.get("topic", "social-campaign"))[:48] or "social-campaign"
    return send_file(path, as_attachment=True, download_name=f"publer_{topic_slug}_{draft_id[:8]}.csv", mimetype="text/csv")


@app.get("/api/social/campaigns/<draft_id>/download.zip")
def social_download_zip(draft_id):
    try:
        draft = pins.load_draft(draft_id)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    if not draft or not _is_social(draft):
        return jsonify({"error": "Social campaign not found."}), 404
    render_info = draft.get("social_render") or {}
    relative = render_info.get("zip_relative")
    if not relative:
        return jsonify({"error": "This campaign has no generated social-card ZIP yet."}), 404
    try:
        path = (ROOT / relative).resolve()
        base = SOCIAL_OUTPUT_DIR.resolve()
        if base not in path.parents or not path.is_file():
            raise FileNotFoundError
    except (ValueError, FileNotFoundError):
        return jsonify({"error": "Generated social-card ZIP could not be found."}), 404
    return send_file(path, as_attachment=True, download_name=f"social_cards_{draft_id[:8]}.zip")


if __name__ == "__main__":
    print("Unified Content Studio: http://127.0.0.1:5001")
    print("Pinterest Pins and Social Media Posts are available from the top tabs.")
    app.run(host="127.0.0.1", port=5001, debug=False, use_reloader=False)


@app.get("/api/health")
def deployment_health():
    try:
        pins.mongo_db().command("ping")
        return jsonify({"ok": True, "database": "connected", "mode": "content-only"})
    except Exception as exc:
        return jsonify({"ok": False, "database": "unavailable", "error": str(exc)}), 503
