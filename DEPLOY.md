# Deploy guide — getting Moto Tracker online

This puts your app on the internet so a phone (or anyone) can reach it, for **$0**.
Three parts, ~20-30 minutes total:

1. **Push your code to GitHub** (also backs everything up).
2. **Run the data pipelines on GitHub Actions** (free scheduled jobs).
3. **Host the API on Render** (free web service).

All the config files are already in this repo. You only do the clicks below.

> **Your secret stays safe.** Your database password (`DATABASE_URL`) is never in
> the code — `.env` is git-ignored. You'll paste it into GitHub and Render as a
> protected secret, which is the correct, safe way to do it.

---

## Part A — Push to GitHub (~10 min)

1. **Make a GitHub account** if you don't have one: https://github.com/signup
2. **Create a new repository:** https://github.com/new
   - Name: `moto-tracker` (anything is fine).
   - **Visibility: Public** (recommended). GitHub gives *unlimited* free Actions
     minutes to public repos; private repos only get 2,000/month, which the
     results job would burn through. Your code has no secrets in it, so public is
     safe. (Prefer private? It works too — just raise the `results.yml` cron
     interval from `*/10` to e.g. `*/30` to stay under the limit.)
   - **Do NOT** check "Add a README / .gitignore / license" — the repo already has them.
   - Click **Create repository**.
3. **Connect and push.** On the new repo's page, copy its URL (the
   `https://github.com/<you>/moto-tracker.git` one), then run these from the
   project folder (PowerShell):
   ```powershell
   git branch -M main
   git remote add origin https://github.com/<you>/moto-tracker.git
   git push -u origin main
   ```
   The first push opens a browser window to sign in to GitHub — approve it. Done.

---

## Part B — Turn on the pipelines (GitHub Actions) (~5 min)

The workflows in `.github/workflows/` run automatically (news every 20 min,
results every 10 min, schedule weekly). They just need your database secret.

1. In your repo: **Settings → Secrets and variables → Actions → New repository secret**.
2. Name: `DATABASE_URL`. Value: your full Neon connection string (the same one in
   your local `.env`). Click **Add secret**.
3. Go to the **Actions** tab. If prompted, click to enable workflows.
4. Test one now: **Actions → "news" → Run workflow → Run**. After ~1 minute it
   should go green. (The scheduled runs will now happen on their own.)

> The **results** job only does real work while a race is live — most runs are a
> 5-second no-op, which is expected.

---

## Part C — Host the API on Render (~10 min)

1. **Create a Render account:** https://render.com — choose **"Sign in with GitHub"**
   (no credit card needed for the free tier).
2. **New + → Blueprint.** Pick your `moto-tracker` repo. Render reads `render.yaml`
   and proposes the `moto-tracker-api` web service. Click **Apply**.
3. When asked, set the **`DATABASE_URL`** environment variable to your Neon
   connection string (same value as before), then create/deploy.
4. Wait for the build to finish (a few minutes). Render gives you a public URL like
   `https://moto-tracker-api.onrender.com`.
5. **Test it:** open `https://<your-url>.onrender.com/docs` — you should see the
   interactive API. Try `https://<your-url>.onrender.com/standings?series=SX&class=450`.

> **Free-tier sleep:** the API nods off after ~15 min of no traffic, so the first
> request after a quiet spell takes ~30-60s to wake, then it's fast again. Perfectly
> fine for a personal app. Upgrade to Render's paid instance (~$7/mo) later if you
> want it always instant.

---

## You're live 🎉

- **Data** stays fresh automatically (GitHub Actions).
- **API** is reachable from anywhere (Render) — this is the URL your iPhone app will call.
- **Cost:** $0.

### Next
Point the future Expo/React Native app at your Render API URL. (Apple charges
$99/yr only when you publish to the App Store — building and testing on your own
iPhone via Expo Go is free.)

### If something breaks
- **Actions job red?** Open it → read the log. Usually a missing/incorrect
  `DATABASE_URL` secret.
- **Render build fails?** Check the deploy log. Confirm `DATABASE_URL` is set and
  the start command is `uvicorn src.api.main:app --host 0.0.0.0 --port $PORT`.
- **API 503 on /health?** The database env var is wrong or Neon is unreachable.
