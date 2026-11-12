# Scout: unfinished UK sites on Base44, Lovable, Replit, Emergent, Bolt, Netlify, Vercel… → WhatsApp + email + Instagram replies → clients

## Deploy on Railway from GitHub
Railway runs Scout and its WhatsApp Web browser as two services. The WhatsApp service needs its own persistent volume to keep the linked-device session after deploys.

1. Push this project to a **private GitHub repository** (the root `.gitignore` excludes local leads, settings, sessions, and logs). In Railway, create a project from that repository.
2. Deploy the repository root as the **Scout** service. Railway will use `Dockerfile`. Add a persistent volume mounted at `/data` before deploying, set the healthcheck path to `/health`, and add:
   - `SCOUT_PASSWORD` — a strong dashboard password.
   - `WA_SECRET` — a random secret token, also set on the WhatsApp service.
   - `SCOUT_DATA_DIR=/data`
   - `WA_URL=http://${{WhatsApp.RAILWAY_PRIVATE_DOMAIN}}:${{WhatsApp.PORT}}`
   Add a public domain to this service when you are ready to access the dashboard.
3. Add a second service from the same repository named **WhatsApp**, with its root directory set to `/wa-service`. Railway will use `wa-service/Dockerfile`. Add a volume mounted at `/session`, set the healthcheck path to `/health`, and add:
   - `WA_SECRET` — exactly the same token as on Scout.
   - `SESSION_DIR=/session`
   - `SCOUT_URL=http://${{Scout.RAILWAY_PRIVATE_DOMAIN}}:${{Scout.PORT}}`
   Do not add a public domain to this service.
4. Generate the `WA_SECRET` locally (for example, `python -c "import secrets; print(secrets.token_hex(32))"`), then paste the same value into both services. Keep `SCOUT_PASSWORD` separate. Railway supplies `PORT` to both containers.
5. After both services are deployed, open the Scout public URL and log in. Connect WhatsApp from **Settings → WhatsApp automation** by scanning the QR code. Search works without SearXNG; add a private SearXNG service separately if you want that search provider.

### Connect Instagram messaging
Instagram is for conversations started by people messaging your professional account. Scout does not send cold Instagram DMs or automatic follow-ups; replies are sent manually from the **Instagram inbox** and only within Meta's 24-hour messaging window.

1. Use an Instagram professional account linked to a Facebook Page. In Meta for Developers, configure the Messenger API for Instagram with the `instagram_manage_messages` permission and subscribe the account to the `messages` webhook field. Meta may require app review/business verification before messaging works outside app roles.
2. In the **Scout** service's environment, set:
   - `INSTAGRAM_ACCOUNT_ID` — the Instagram professional account ID.
   - `INSTAGRAM_ACCESS_TOKEN` — a Page access token with Instagram messaging permission.
   - `INSTAGRAM_APP_SECRET` — the Meta app secret, used to verify webhook signatures.
   - `INSTAGRAM_VERIFY_TOKEN` — a private value you choose; enter the same value in the Meta webhook setup.
   - `INSTAGRAM_GRAPH_VERSION` — optional Graph API version (defaults to `v22.0`).
   Keep these values in Railway/server environment variables, never in Git or a browser form.
3. Redeploy Scout and open **Settings → Instagram Messaging API**. Copy the displayed HTTPS webhook callback URL into the Meta app's Instagram messaging webhook configuration, use the same verify token, and subscribe to `messages`. Subscribe the Instagram account to the app as required in Meta's dashboard.
4. Send a test DM to the Instagram account. It should appear in **Instagram inbox**. Open it, write a reply, and click **Send Instagram reply**.

The inbox is separate from site leads because Instagram's recipient IDs are scoped to the Meta app. The API does not provide general cold-DM access to arbitrary handles.

The `/data` volume keeps Scout's database and backups across deploys; `/session` keeps the WhatsApp login. Railway provides `RAILWAY_VOLUME_MOUNT_PATH` when a volume is attached, and Scout checks that `SCOUT_DATA_DIR` is at or inside that path rather than relying on container mount details. Scout refuses to start if no volume is attached or its data directory is outside the volume, rather than silently starting with an empty database after an update. If the database file is missing or unreadable, Scout tries to restore the newest valid backup; an unreadable original is preserved under `backups/unreadable/`. A new Railway volume still starts empty—the local `scout_data.json` and exported lead files are intentionally not included in the Docker image or Git. Back up or migrate local data separately before switching to a new volume.

## 0. Run it 24/7 on your Hyonix server (recommended)
Once it's on the server, everything runs without you: Autopilot, emails, WhatsApp (max 100/day), follow-ups, reply detection and daily backups.
Open the dashboard from any phone or laptop with your password.

### If your server is Linux (Ubuntu/Debian)
1. Upload `scout-app.zip` to the server, or download it there, then connect with SSH:
   `ssh root@YOUR-SERVER-IP`
2. Run:
   ```
   apt-get install -y unzip && unzip scout-app.zip && cd scout-app
   bash setup-server.sh
   ```
   It installs Docker, asks you for a dashboard password (and optionally your domain for HTTPS), then starts everything.
3. Open `http://YOUR-SERVER-IP:8000` and log in.
It restarts by itself after crashes and reboots. Useful commands, run in the scout-app folder:
`docker compose logs -f` (see what it's doing) · `docker compose restart` · `docker compose down` (stop).
Your data lives in `scout-app/data/` (including 14 days of automatic backups).

### If your server is Windows (Remote Desktop)
1. On the server, install **Python 3.11+** (tick "Add to PATH") and **Node.js LTS** (nodejs.org).
2. Unzip, then double-click `start-windows.bat` once. This sets up Scout and opens it. Close it when it's done.
3. Right-click `install-autostart-windows.bat` → **Run as administrator**, and choose a password.
   Scout now starts with the server, runs even when you're logged out, and is reachable at `http://YOUR-SERVER-IP:8000`.
   (If the page doesn't load, open port 8000 in Hyonix's firewall panel as well.)

### Connect WhatsApp (both servers)
Dashboard → **Settings → WhatsApp automation**. Scan the QR code with the phone that has your **outreach WhatsApp number**
(WhatsApp → Settings → Linked devices → Link a device). Keep that phone switched on with internet every few days so the link stays active.
Then tick "Send automatically", set your daily number (max 100) and save.
Warm-up starts a new number at 10/day and adds 5 each day, so 100/day is reached after about 2–3 weeks. Leave it on.

## 1. Or run it on your laptop

**One-time:** install Python 3.11 or newer from python.org.
On Windows, tick **"Add Python to PATH"** on the first installer screen.

**Windows:** unzip the folder and double-click `start-windows.bat`.
**Mac:** unzip the folder, right-click `start-mac.command` and choose **Open** (the first time only, because macOS asks).

The first run sets everything up, which takes about 3–5 minutes. After that it starts in seconds.
Scout opens in your browser at http://localhost:8000.

**Keep the black window open.** Scout, the Autopilot and the follow-ups only run while it's open.
- Mac: the launcher keeps the Mac awake for you.
- Windows: Settings → System → Power → set "Sleep" to **Never** when plugged in.
If the laptop is off, nothing is lost. Anything that was due goes out next time you start Scout, within sending hours.

## 2. Set up (Settings page, ~10 minutes)
1. **Your name and portfolio link.** These are used in every message.
2. **Gemini key** (aistudio.google.com) gives you AI-written replies. Finding sites needs **no key** (see "How Scout finds sites" below).
3. **Email sending:** choose your provider, then enter your address and an **app password** (not your normal password).
   - Gmail/Google Workspace: turn on 2-Step Verification → myaccount.google.com/apppasswords
   - Zoho: Settings → Security → App Passwords
   - Click **Send test email to myself** and **Test inbox connection**. Both must work.
4. On **Today**, switch **Email sending** to LIVE.

## 3. Set your daily target (Autopilot)
On Today: switch Autopilot ON, set **Daily target** (e.g. 20), and tick WhatsApp and/or Email.
Every weekday from 07:00 UK time, Autopilot:
1. starts the follow-up sequence for the most broken UK sites it already has
2. when it runs out, searches Google (niche × city), scans the new sites, verifies contacts, and starts those
3. stops when today's target is hit, then starts again tomorrow.

## 4. Your daily routine (about 15 minutes)
Open **Today**:
1. **Replies waiting**: email replies appear automatically. Click Reply and use "✨ Draft with AI".
2. **WhatsApp to send now**: click Open WhatsApp → send → Mark sent. Do this for each one.
3. Got a WhatsApp reply? Open the lead and paste it in. That stops its follow-ups and drafts your answer.
Emails and email follow-ups send by themselves.

## Platforms
Scout looks for sites still on these free builder addresses: base44.app, lovable.app, replit.app, emergent.host, bolt.host,
netlify.app, vercel.app (on by default), plus github.io, framer.website, webflow.io, web.app, onrender.com, carrd.co (switch on in
Settings → Platforms). Autopilot, the archive pool and the search presets rotate through every platform that's switched on.
Deleted or offline sites (common on Netlify/Vercel) are detected and never contacted. To add a platform, edit `platforms.py`.

## How Scout finds sites (free, no key needed)
Scout uses whichever of these is available, and falls back to the next one automatically:
1. **Your own search engine (SearXNG)**, included in the Linux server setup. It runs privately next to Scout and searches
   Google, Bing, DuckDuckGo and Brave for things like `site:base44.app cleaning London`. Free and permanent.
2. **Free archives:** Common Crawl, the Internet Archive and certificate logs list thousands of base44.app sites.
   Scout refreshes them once a day and checks each site's name first, dropping apps and tools before the full scan.
   (These archives are sometimes busy. Scout simply retries later.)
3. **Optional paid extras:** a Firecrawl or Serper key in Settings, used only if you add one.
Autopilot goes through niche × UK city searches (cleaning London, barber Manchester…), then uses the archive pool.
Change the niches and cities in Settings → Autopilot. Keep "max searches per day" modest (15–40) so the search engines don't rate-limit you.
On a **Windows** server, SearXNG isn't included: the archives still work, or install Docker Desktop, copy `searxng/settings.yml.example` to `searxng/settings.yml`, and run `docker compose up -d searxng`,
then enter `http://127.0.0.1:8080` as the SearXNG address in Settings (also add `ports: ["127.0.0.1:8080:8080"]` under searxng in docker-compose.yml).

## Companies tab (Europages): redesign leads
A separate workflow for established UK companies with dated websites. Your Base44 leads and Autopilot are not affected.
**Fastest way: the "Add to Scout" button.** In Companies, drag the green **＋ Add to Scout** button onto your bookmarks bar
(Ctrl+Shift+B shows the bar). Then on Europages:
- on a **company or product page**, click it to add that company with its **exact website** (from "Company's Website")
- on a **results page**, click it to add every company listed (Scout then finds their websites)
A small panel counts what you've collected. Click **Send to Scout** and they appear in Companies, ready to audit.
(The button only reads the page you're looking at. Scout never browses Europages by itself, which keeps you within their rules.)

Or copy and paste:
1. **Companies → pick a sector** (★ Metal fabrication is the recommended start). Europages opens, already filtered to the UK.
2. On the results page press **Ctrl+A, Ctrl+C**, then paste into Scout and click **Read companies**. Repeat for page 2, 3…
   (Europages blocks automated tools, so Scout never visits it. It only reads what you paste.)
3. Click **Find websites & audit**. Scout finds each company's own website (search engine if available, otherwise by
   matching the company name to its web address), checks it like a buyer would (phone-friendly? padlock? quote button?
   old copyright year? slow? dated build?) and pulls verified contacts from the company's own site.
4. **Show: Poor websites (score 3+)**, then click **Start email outreach**. Companies use their own sequence (free homepage mockup,
   then from £{price}). Edit it under Sequences → Companies. Set the price at the top of the companies list.
Companies whose website wasn't found are listed separately. Some have no website at all, which makes them a lead for a new one.

## Lead Quality (who's likely to convert)
Every lead gets a score from 0 to 100 (A 80+, B 65+, C 50+, D below) with the reasons listed on the lead.
- **Up:** UK mobile/WhatsApp · a business email · problems that lose them enquiries (broken call/WhatsApp button, bouncing email,
  no quote option, not mobile-friendly) · a real active business (address, hours, prices, reviews, Ltd, established, insured) ·
  a DIY site on a free builder address · a good niche (trades, cleaning, health, manufacturing…)
- **Down:** footer credits a web designer · club/charity/student/demo project · chain/franchise · not UK · web agency (competitor) · site offline
- **Learns from you:** once you've contacted 8+ leads, niches, platforms and problem types that reply more get pushed up,
  and ones that don't get pushed down. See *What's working for you* on the Dashboard.
Autopilot and Company Autopilot only start leads at or above your minimum (Settings → Autopilot, default 60), best first.
Leads scanned before this version: tick them in Leads → **Re-scan selected** so they get the new business signals.

## Finding UK companies with Firecrawl (Companies tab)
Add your Firecrawl key in Settings, choose a sector and towns, then click **Find companies**. Firecrawl searches UK results
(directories like Yell/Europages excluded); Scout checks each site (free with its own browser, or ~2 Firecrawl credits per site),
pulls contacts and lists what's wrong. Turn on **Company Autopilot** to do this every day, and optionally start emails for good leads.
Costs: search = 2 credits per 10 results; website check by Firecrawl = ~1 credit per page.

## The STOP button
The red **■ STOP** in the sidebar stops everything that runs by itself, straight away: email and WhatsApp sending,
Autopilot, Company Autopilot, searches and scans. It also switches all of them **OFF** and resets the search history,
so nothing starts again until you switch it back on. Approved WhatsApp replies that hadn't gone out yet return to
"Waiting for your OK". Your leads and settings are kept.

## How follow-ups work
| | Day 0 | Follow-ups | Ends |
|---|---|---|---|
| WhatsApp (automatic if turned on, or you send) | opener | day 2, day 5 | after 3 messages |
| Email (automatic) | day 1 if WhatsApp too, else day 0 | day 3, day 7 (all in one thread) | day 12 "last one" |
- A reply on **any** channel stops **all** follow-ups for that lead.
- "unsubscribe", "stop" or "not interested" marks the lead **Do not contact** forever (on every channel).
- Bounced emails stop email for that lead only.
- Edit every message on the **Sequences** page.

## Staying safe (please read)
- **Use a separate mailbox/domain for cold email** (e.g. you@getyoursite.co.uk), not your main one. Start at 20–30 emails/day.
- **WhatsApp auto-send** uses a linked device (like WhatsApp Web). Automated messages to people who haven't contacted you first are against WhatsApp's rules, so a number can be restricted. Use a **dedicated number**, keep the warm-up on, and stay at or below 100/day (Scout won't go higher). If you see blocks or reports going up, lower the daily number.
- **UK rules (PECR/GDPR):** cold email to **limited companies** is allowed if you identify yourself and offer an opt-out, which Scout does on every email. **Sole traders and partnerships** count as individuals, so unsolicited marketing email to them needs consent. Be careful with personal-looking addresses (e.g. a Gmail address that is clearly one person's). Always honour opt-outs, which Scout does automatically. This isn't legal advice; see ico.org.uk for details.

## Files
`app.py` server · `index.html` dashboard · `instagram.py` official Instagram Messaging API/webhook · `scout.py` scraper and verification · `outreach.py` email and follow-up engine ·
`autopilot.py` daily target · `discovery.py` + `searxng/` finding sites · `companies.py` Europages companies · `whatsapp_auto.py` + `wa-service/` WhatsApp sending · `dm_agent.py` message logic · `DM_PLAYBOOK.md` the AI's instructions ·
`scout_data.json` **all your data, so back this file up**
