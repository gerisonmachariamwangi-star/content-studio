# Content Studio — Vercel deployment

This package is configured for content generation and campaign management only:
- Pinterest Pins content generation, image-prompt downloads and CSV downloads
- Social Media content generation, image-prompt downloads and CSV downloads
- Campaign Library with MongoDB persistence, archive and restore
- Cloudflare R2 configuration retained for media upload features

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
