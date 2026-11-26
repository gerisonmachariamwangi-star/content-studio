# Content Studio — Vercel deployment

This package is configured for content generation and campaign management only:
- Pinterest Pins content generation, image-prompt downloads and CSV downloads
- Social Media content generation, image-prompt downloads and CSV downloads
- Campaign Library with MongoDB persistence, archive and restore
- Browser-to-R2 image uploads using short-lived presigned URLs; media manifest stored in MongoDB

Pinterest OAuth, publishing, scheduler imports and background workers are disabled.

## Deploy
1. Create a new GitHub repository and push this folder's contents.
2. Import that repository into Vercel as a new project.
3. Add the variables listed in `.env.example` in Vercel Project Settings → Environment Variables.
4. Redeploy after adding variables.

## Required variables
- `OPENROUTER_API_KEY`
- `FLASK_SECRET_KEY` (use a long random value)
- `MONGODB_URI` (your MongoDB Atlas connection string)
- `MONGODB_DATABASE` (default: `content_studio`)

## Optional media variables
- `R2_ACCESS_KEY_ID`
- `R2_SECRET_ACCESS_KEY`
- `R2_BUCKET_NAME`
- `R2_ENDPOINT_URL`
- `R2_PUBLIC_URL`

MongoDB stores campaign drafts and the R2 media manifest. Temporary working files and CSVs are generated under `/tmp` during requests; download CSVs when generated. Vercel's temporary filesystem is not permanent storage. For media that must remain available between requests, store it in R2.

## Push from WSL
From the extracted project folder:

```bash
git init
git add .
git commit -m "Deploy Content Studio to Vercel"
git branch -M main
git remote add origin https://github.com/gerisonmachariamwangi-star/content-studio.git
git push -u origin main
```
If `origin` already exists, use `git remote set-url origin https://github.com/gerisonmachariamwangi-star/content-studio.git` instead of `git remote add origin`.

Do not commit `.env` or paste secrets into source files.

## Browser image upload setup
The image picker uploads directly from the browser to Cloudflare R2 using short-lived presigned URLs. Configure your R2 bucket CORS policy to allow PUT requests from your Vercel domain and `http://localhost:5001`, with allowed header `Content-Type` and exposed header `ETag`. Do not expose R2 secret keys in frontend code.

### Cloudflare R2 CORS
In Cloudflare Dashboard → R2 → your bucket → Settings → CORS Policy, add a rule like this (keep the production origin and localhost origins):

```json
[
  {
    "AllowedOrigins": [
      "https://content-studio-ten-tawny.vercel.app",
      "http://localhost:5001",
      "http://127.0.0.1:5001"
    ],
    "AllowedMethods": ["PUT", "GET", "HEAD"],
    "AllowedHeaders": ["Content-Type"],
    "ExposeHeaders": ["ETag"],
    "MaxAgeSeconds": 3600
  }
]
```

The image workflow now uses the browser file picker and presigned R2 uploads. The browser never receives the R2 access key or secret. The existing video-generation workflow still uses local `ToPost/videos/` files and is not converted to browser-based upload in this revision.
