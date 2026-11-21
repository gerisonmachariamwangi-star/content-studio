import os
import csv
import sys
import hashlib
import hmac
import mimetypes
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, quote

import requests
from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parent
IMAGE_DIR = ROOT / "ToPost"
OUTPUT_FILE = ROOT / "image_urls.csv"

load_dotenv(ROOT / ".env")

ACCESS_KEY = os.getenv("R2_ACCESS_KEY_ID", "").strip()
SECRET_KEY = os.getenv("R2_SECRET_ACCESS_KEY", "").strip()
BUCKET = os.getenv("R2_BUCKET_NAME", "").strip()
ENDPOINT = os.getenv("R2_ENDPOINT_URL", "").strip().rstrip("/")
PUBLIC_URL = os.getenv("R2_PUBLIC_URL", "").strip().rstrip("/")

ALLOWED_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".webp",
    ".bmp", ".tif", ".tiff"
}


def sign_request(method, url, payload=b""):

    parsed = urlsplit(url)

    now = datetime.now(timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = now.strftime("%Y%m%d")

    payload_hash = hashlib.sha256(payload).hexdigest()
    host = parsed.netloc

    canonical_headers = (
        f"host:{host}\n"
        f"x-amz-content-sha256:{payload_hash}\n"
        f"x-amz-date:{amz_date}\n"
    )

    signed_headers = "host;x-amz-content-sha256;x-amz-date"

    canonical_request = "\n".join([
        method,
        parsed.path,
        parsed.query,
        canonical_headers,
        signed_headers,
        payload_hash
    ])

    region = "auto"
    scope = f"{date_stamp}/{region}/s3/aws4_request"

    string_to_sign = "\n".join([
        "AWS4-HMAC-SHA256",
        amz_date,
        scope,
        hashlib.sha256(
            canonical_request.encode()
        ).hexdigest()
    ])

    def hmac_sha256(key, message):
        return hmac.new(
            key,
            message.encode(),
            hashlib.sha256
        ).digest()

    key_date = hmac_sha256(
        ("AWS4" + SECRET_KEY).encode(),
        date_stamp
    )

    key_region = hmac_sha256(key_date, region)
    key_service = hmac_sha256(key_region, "s3")
    signing_key = hmac_sha256(
        key_service,
        "aws4_request"
    )

    signature = hmac.new(
        signing_key,
        string_to_sign.encode(),
        hashlib.sha256
    ).hexdigest()

    authorization = (
        "AWS4-HMAC-SHA256 "
        f"Credential={ACCESS_KEY}/{scope}, "
        f"SignedHeaders={signed_headers}, "
        f"Signature={signature}"
    )

    return {
        "Authorization": authorization,
        "x-amz-date": amz_date,
        "x-amz-content-sha256": payload_hash,
        "Content-Type": "application/octet-stream"
    }


def upload_image(image):

    key = f"media/{image.name}"

    bucket_path = quote(BUCKET, safe="-_.~")
    object_path = quote(key, safe="/-_.~")

    upload_url = (
        f"{ENDPOINT}/{bucket_path}/{object_path}"
    )

    public_url = (
        f"{PUBLIC_URL}/{object_path}"
    )

    content = image.read_bytes()

    content_type = (
        mimetypes.guess_type(image.name)[0]
        or "application/octet-stream"
    )

    headers = sign_request(
        "PUT",
        upload_url,
        content
    )

    headers["Content-Type"] = content_type

    response = requests.put(
        upload_url,
        headers=headers,
        data=content,
        timeout=120
    )

    if not response.ok:
        raise RuntimeError(
            f"Upload failed: HTTP {response.status_code} "
            f"{response.text[:500]}"
        )

    # Verify that the public URL is accessible.
    check = requests.get(
        public_url,
        stream=True,
        timeout=60
    )

    try:
        if not check.ok:
            raise RuntimeError(
                f"Public URL returned HTTP {check.status_code}"
            )

        returned_type = check.headers.get("Content-Type", "")

        if not (returned_type.startswith("image/") or returned_type.startswith("video/") or returned_type == "application/octet-stream"):
            raise RuntimeError(
                f"Unexpected public content type: {returned_type}"
            )
    finally:
        check.close()

    return public_url, len(content), content_type


def main():

    required = {
        "R2_ACCESS_KEY_ID": ACCESS_KEY,
        "R2_SECRET_ACCESS_KEY": SECRET_KEY,
        "R2_BUCKET_NAME": BUCKET,
        "R2_ENDPOINT_URL": ENDPOINT,
        "R2_PUBLIC_URL": PUBLIC_URL
    }

    missing = [
        key for key, value in required.items()
        if not value
    ]

    if missing:
        print("Missing configuration:", ", ".join(missing))
        sys.exit(1)

    if not IMAGE_DIR.exists():
        print("Image folder not found:", IMAGE_DIR)
        sys.exit(1)

    images = sorted(
        [
            f for f in IMAGE_DIR.iterdir()
            if f.is_file()
            and f.suffix.lower() in ALLOWED_EXTENSIONS
        ],
        key=lambda f: f.name.lower()
    )

    if not images:
        print("No supported images found in ToPost.")
        return

    print()
    print("=" * 60)
    print("PINTEREST IMAGE UPLOADER")
    print("=" * 60)
    print("Bucket:", BUCKET)
    print("Images found:", len(images))
    print()

    successful = []

    for index, image in enumerate(images, 1):

        print(f"[{index}/{len(images)}] Uploading: {image.name}")

        try:
            url, size, content_type = upload_image(image)

            successful.append({
                "Filename": image.name,
                "Media URL": url
            })

            print("SUCCESS:", url)
            print(f"Size: {size / 1024:.1f} KB")
            print()

        except Exception as exc:
            print("FAILED:", str(exc))
            print()

    if successful:

        with open(
            OUTPUT_FILE,
            "w",
            newline="",
            encoding="utf-8-sig"
        ) as csvfile:

            writer = csv.DictWriter(
                csvfile,
                fieldnames=["Filename", "Media URL"]
            )

            writer.writeheader()
            writer.writerows(successful)

    print("=" * 60)
    print("UPLOAD COMPLETED")
    print("=" * 60)
    print("Successful:", len(successful))
    print("Failed:", len(images) - len(successful))

    if successful:
        print("URL manifest:", OUTPUT_FILE)

    print()


if __name__ == "__main__":
    main()
