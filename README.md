# PDF → XML Converter — Web Deployment

## Deploy to Railway (free, ~5 minutes)

### Step 1 — Put the code on GitHub

1. Go to https://github.com/new
2. Create a **new repository** (e.g. `pdf-xml-converter`) — keep it Public
3. Upload these 4 files: `app.py`, `requirements.txt`, `Procfile`, `railway.toml`
   - Click "uploading an existing file" on the repo page
   - Drag all 4 files in → click "Commit changes"

### Step 2 — Deploy on Railway

1. Go to https://railway.app and sign up (free, no credit card)
2. Click **"New Project"** → **"Deploy from GitHub repo"**
3. Authorise Railway to access GitHub → select your `pdf-xml-converter` repo
4. Railway detects Python automatically and deploys in ~2 minutes
5. Click **"Settings"** → **"Generate Domain"** → you get a public URL like:
   `https://pdf-xml-converter-production.up.railway.app`

Share that URL with anyone — done.

### Free tier limits
- 500 hours/month (enough for ~16h/day continuous use)
- 512 MB RAM — handles PDFs up to ~50 pages comfortably
- Sleeps after 10 min inactivity (wakes in ~5 sec on next visit)

### Redeploy after changes
Just push updated files to GitHub — Railway redeploys automatically.
