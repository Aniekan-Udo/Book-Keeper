# Deploying for free: Render + FalkorDB Cloud

What runs where, all on free plans:

| Part | Where | Limits that matter |
|---|---|---|
| App (Docker) | Render free web service | 512 MB, shared CPU; **sleeps after 15 min without traffic** (about a minute to wake); 750 hours a month |
| Postgres | Render free Postgres | 1 GB; **expires 30 days after creation** (14 days' grace, then deleted) |
| Graph memory | FalkorDB Cloud free tier | 100 MB; no TLS, no backups; **stopped after 1 day unused, deleted after 7** |
| Models | OpenRouter (and Groq for voice notes/screenshots) | your credits |

The ledger is in Postgres; the graph can always be rebuilt from it
(`scripts/rebuild_graph.py`), so losing the FalkorDB instance loses learned memory, not money records.

## 1. FalkorDB Cloud

1. Sign up at https://app.falkordb.cloud (no card).
2. Create a **Free** instance. Pick a European region (AWS `eu-central-1` / GCP `europe-west`), near Render's Frankfurt.
3. Note its **host, port, username and password** (on the instance's connection page).

## 2. Render

1. Sign up at https://render.com with GitHub (as `Aniekan-Udo`) and allow access to the `Book-Keeper` repo.
2. **New → Blueprint**, pick the repo, branch `roadmap`. Render reads `render.yaml` and proposes
   the web service `book-keeper` and the database `book-keeper-db`.
3. It asks for the values marked `sync: false`:
   - `FALKORDB_HOST`, `FALKORDB_PORT`, `FALKORDB_USERNAME`, `FALKORDB_PASSWORD`: from step 1
   - `OPENROUTER_API_KEY`: your OpenRouter key
   - `GROQ_API_KEY`: for voice notes and screenshots (the free Groq key is enough)
   - `APP_PUBLIC_URL`: `https://book-keeper.onrender.com` (if that name is taken, Render adds a
     suffix: fix it here after the first deploy)
4. **Apply.** The first build takes a few minutes. On start the app creates its tables
   (`migrate.py`), then serves. Check `https://<your-service>.onrender.com/health`:
   `{"status":"ok", ... "graph":"ok"}`.

## 3. Seed the demo accounts (from your PC, once)

The database only accepts connections from inside Render until you allow your IP:

1. Render → `book-keeper-db` → **Networking** → add your IP (find it at https://ifconfig.me).
2. Copy the database's **External Database URL**.
3. In Git Bash, from the project folder (don't put these in `.env`; they're for this one run):

   ```bash
   POSTGRES_URI='<External Database URL>?ssl=require' \
   FALKORDB_HOST='<host>' FALKORDB_PORT='<port>' FALKORDB_USERNAME='<user>' FALKORDB_PASSWORD='<password>' \
   DEMO_PASSWORD='<choose one>' \
   uv run python -m scripts.seed_demo
   ```

   It prints the two demo accounts. The seed uses a scripted model: no model calls, no cost.
4. Remove your IP from the database's Networking list again.

## 4. Keep it awake

Free services sleep after 15 minutes, and the first visit then waits about a minute: bad
for judges, and WhatsApp gives up on slow webhooks. A free pinger fixes it:

1. Sign up at https://cron-job.org (free).
2. New cron job: URL `https://<your-service>.onrender.com/health`, **every 10 minutes**.

One always-on service uses about 720 of the 750 free hours a month. Each `/health` call
also queries the database and pings FalkorDB, which should keep the graph instance from
counting as unused (check it's still running after the first day).

## 5. Optional

- **Traces:** add `OPIK_API_KEY` and `OPIK_WORKSPACE` in the service's Environment tab.
- **WhatsApp:** set the `WHATSAPP_*` variables (see `.env.example`) and point Meta's webhook at
  `https://<your-service>.onrender.com/webhooks/whatsapp`.

## Things to know

- **Not for real traders as is:** graph traffic to the free FalkorDB isn't encrypted, the
  database expires in 30 days, and nothing is backed up. Fine for a demo with made-up data.
- **Redeploys** happen on every push to `roadmap` (`autoDeploy`). Migrations run on start.
- **After 30 days** the database is deleted unless upgraded; recreate it, redeploy, and seed again.
- **If the graph instance is deleted:** create a new one, update the four `FALKORDB_*`
  values, then rebuild the memory mirror from the ledger. Learned memory is lost; for the demo,
  seeding again with `--reset` brings back the whole month.
