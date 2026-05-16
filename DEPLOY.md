# Hosting the TSS Financial Model

The Streamlit app is locked behind a 5-character password. The password is read
from (priority order):

1. `st.secrets["password"]` — populated from `.streamlit/secrets.toml` locally
   or the host's secrets panel in production.
2. Env var `TSS_MODEL_PASSWORD`.
3. Fallback `"tss26"` — used only if neither of the above is set.

**Change the password before deploying** — see each host below for where.

---

## Deployment options

| Host | Cost | Streamlit-friendly | Password | Setup time |
|---|---|---|---|---|
| Streamlit Community Cloud | Free (1 private app) | Native | `secrets.toml` panel | ~5 min |
| Render | Free tier (sleeps after 15 min idle) | Yes (Python web service) | env var | ~5 min |
| Railway / Fly.io | Small free credits | Yes | env var | ~10 min |
| Vercel | Free | **Not directly compatible** | — | See "Vercel" section below |

### 1. Streamlit Community Cloud (recommended)

The fastest path. Requires a GitHub repo containing this folder.

1. Push this folder to a GitHub repo (see "Pushing to GitHub" below).
2. Go to <https://share.streamlit.io>, sign in with GitHub.
3. Click **New app**, pick your repo / branch / `app.py`.
4. Click **Advanced settings → Secrets** and paste:
   ```toml
   password = "your5"
   ```
   (5-character password of your choice.)
5. Click **Deploy**. App URL will be something like
   `https://yourname-tss-financial-model.streamlit.app`.
6. To restrict who can sign in, set **Settings → Sharing → Private** and add
   email addresses that can view. (Free tier allows 1 private app.)

### 2. Render

1. Push to GitHub.
2. <https://render.com> → **New Web Service** → connect repo.
3. Configure:
   - **Build command**: `pip install -r requirements.txt`
   - **Start command**: `streamlit run app.py --server.port=$PORT --server.address=0.0.0.0 --server.headless=true`
   - **Environment**: add `TSS_MODEL_PASSWORD = your5`
4. Free instance type sleeps after 15 minutes of inactivity (first request takes
   ~30s to wake). Upgrade to $7/mo Starter for always-on.

A `Procfile` is included so Render auto-detects the start command.

### 3. Railway / Fly.io / Heroku-likes

Same shape as Render. Set `TSS_MODEL_PASSWORD` as an env var. The `Procfile`
and `requirements.txt` cover Heroku-style buildpacks.

---

## Vercel (honest assessment)

**Streamlit does not run on Vercel.** Vercel is built for stateless serverless
functions (10s default, 60s max execution); Streamlit needs a long-running
Python process plus an active WebSocket between browser and server. There's no
adapter that makes Streamlit work cleanly on Vercel.

If you specifically need a `*.vercel.app` URL, options:

- **Rewrite the UI as a Next.js + React app** that calls Python via Vercel's
  Python serverless runtime (or a separate API host like AWS Lambda). The
  compute engine in `model/` stays as-is. This is an 8–12 hour rewrite.
- **Static export**: re-implement the entire compute engine in JavaScript and
  serve as a static SPA from Vercel. Doable but you lose the Python
  ecosystem (pandas, numpy, openpyxl). Bigger rewrite.
- **Hybrid**: Streamlit on Render/Streamlit Cloud, custom landing page on
  Vercel that links to the Streamlit URL. Cheapest if branding matters.

If you want me to do the Next.js rewrite say the word — it's a meaningful
project (likely a full session). Otherwise Streamlit Community Cloud is the
right answer for getting this online today.

---

## Pushing to GitHub

This folder is now a git repo. To put it on GitHub:

1. Create a new repo at <https://github.com/new>:
   - Name: e.g. `tss-financial-model`
   - **Set to Private** (financials should not be public).
   - Do **not** initialize with README/`.gitignore` (we already have them).
2. Copy the commands GitHub shows you for "push an existing repo from the
   command line". Should look like:
   ```bash
   cd /Users/jon/Downloads/software-society/financial-model
   git remote add origin https://github.com/YOUR_USERNAME/tss-financial-model.git
   git branch -M main
   git push -u origin main
   ```
3. If you have 2FA on, GitHub will prompt for a personal access token — create
   one at <https://github.com/settings/tokens?type=beta> with `repo` scope and
   paste it as the password.

After that, Streamlit Community Cloud (or Render) deploys from the repo in
~2 minutes.

---

## Security notes

- `.streamlit/secrets.toml` is **gitignored** — it's never pushed.
- Real passwords go in the host's secrets panel or env vars, never in code.
- The 5-character password is a thin gate (designed for sharing with a
  prospective investor, not against attackers). If you need real auth, switch
  to OAuth via `streamlit-authenticator` or move to Streamlit Cloud's private
  app feature, which uses Google-account access lists.
- Don't paste your `.xlsx`, `inputs.yaml`, or `inputs.baseline.yaml` to public
  Slack channels / pastebins — they contain projected financials.
