# LinkedIn Daily Agent — Architecture & Design Notes

This is the deep-dive companion to [`README.md`](./README.md) (which covers setup and redeploy).
It documents **what the agent does, how it's built, why it's built that way, and the gotchas
learned the hard way**.

> No secrets live here. All tokens/IDs/passwords are in `config.env` (gitignored); see
> `config.example.env`. This doc refers to them by name only.

---

## 1. What it is

A self-hosted agent that writes LinkedIn posts in the owner's voice, designs a branded
image card (or watermarked video), and sends everything to **Telegram for approval** before
anything is published. It also lets the owner **browse** recent news and pick the story.
Everything runs on one small self-hosted Linux box; **nothing is posted without an explicit
tap**, and there are **no inbound ports** (approvals work over an outbound-only Telegram
long-poll).

**Stack:** n8n (2 workflows) + a localhost Python render service + Telegram Bot API +
LinkedIn's official API (n8n node for images, raw Posts API for video/drafts) + Anthropic
Claude (Haiku) for writing. Card images are Chromium screenshots of HTML.

**What it never does:** scrape LinkedIn. The only LinkedIn traffic is the OAuth-authenticated
official API, used to publish (or save as a draft) the owner's own approved posts. Pasted
LinkedIn links are not opened (see §7).

---

## 2. High-level architecture

```
                    ┌─────────────────────────── n8n host ───────────────────────────┐
  3×/day schedule ─▶│ WF-A  "LinkedIn Daily Agent"  (headlines digest, ~17 nodes)     │
                    │      all news feeds → balanced headlines page → Telegram         │
                    │                                                                  │
  Telegram  ◀──────▶│ WF-B  "LinkedIn Approvals"    (continuous listener, ~210 nodes) │──▶ localhost
  (long-poll,       │      claim-lock → getUpdates → route event → act → loop          │    render svc
   outbound only)   └──────────────────────────────────────────────────────────────────┘    127.0.0.1:8099
                                                                                             (Chromium + ffmpeg
   External: Anthropic (writing) · LinkedIn official API (publish) · publisher RSS feeds      + locks/state)
             · Wikipedia/Wikimedia Commons (photos) · YouTube via yt-dlp (optional clips)
```

- **`build_wf.py`** is the source of truth: a Python generator that emits `wf_a.json` and
  `wf_b.json` (do NOT hand-edit the JSON). Redeploy with `import_workflows.py`.
- **`render_service.py`** is a tiny localhost `http.server` that n8n calls over HTTP. It does
  everything n8n can't: Chromium screenshots, ffmpeg watermarking, and in-memory
  state/locks. Bound to `127.0.0.1` only.

### Why two workflows + a render service?

- **`executeCommand` isn't available** in this n8n build, so shelling out to Chromium/ffmpeg
  from n8n is impossible. The render service is how we run them — n8n calls it via plain
  `httpRequest`, keeping everything local and adding zero exposure.
- **Approvals can't use webhooks.** A webhook is an *inbound* endpoint, and opening a port
  was ruled out from day one. So WF-B **long-polls** Telegram `getUpdates` (outbound only).
  Works on cellular, nothing exposed.

---

## 3. WF-A — the scheduled headlines digest

Fires at **07:00 / 12:00 / 18:00** (n8n's `GENERIC_TIMEZONE`). The schedule does not generate
a draft: auto-picked stories were rarely the ones worth posting about. Instead each run sends
the **same balanced headlines page as `/headlines`** (page 0): fetch all news feeds → filter
reviews → per-host cap → **max 3 stock/market items per page of 10** → send with the
**➡️ Show 10 more** button. The button and any pasted-back link are handled by WF-B's
listener like any other event — the owner picks the story, the bot writes the post.

### The two-pass news pipeline (used by WF-B's `/new` and 🔄 Different news)

1. **Fetch** all publisher RSS feeds (see §7).
2. **Filter reviews + rank** (Code node): drop product reviews / gadget commerce / formulaic
   finance filler, keep stock-site items only when they are about tech, sort newest-first,
   **cap each source** (≤6) and **cap the stock/market share** (≤6 of 24) so the picker
   mostly sees actual tech news.
3. **Bundle** the candidates and fetch the **recently-used** list (variety control).
4. **Drop used** (Code node): recently used stories are removed in code, links compared
   without `?source=` tracking params. Telling the picker to avoid them was not enough: it
   chose the same article three times in one afternoon.
5. **Pick story** (cheap Claude call): choose ONE fresh, substantive story (avoids reviews),
   returning `{index, url}`.
6. **Fetch the full article** at that URL and **clean** it to text (strip scripts/nav/tags,
   extract `og:` metadata + image).
7. **Write post** (Claude): a detailed post in the owner's voice using the article's real numbers.
8. **Extract draft → card → Telegram** (the shared "tail", §5).

Two-pass (pick, then read the full article) is what makes posts *specific* — early versions
that fed Claude only the RSS snippet produced generic "headline number only" posts.

---

## 4. WF-B — the continuous approval listener

A **single** long-running execution that loops:

```
15-second schedule → Claim/heartbeat (lock) → Claimed? → Load offset →
Get updates (long-poll getUpdates, 50s) → Process updates (classify 1 event) →
(Send status) → route to a branch → act → loop back to Claim/heartbeat
```

It self-caps at ~15 min, **releases the lock** (`/claim {release}`), and the next 15-second tick
starts a fresh listener. Without the release, taps went unanswered for up to ~2 min while the
65 s lock ran out. The **`/claim` lock** (see §6) guarantees only ONE listener runs at a time.

**Event routing** — `Process updates` turns each Telegram message/callback into one event,
then a chain of IF nodes dispatches it:

| Event | Trigger | Branch |
|-------|---------|--------|
| `help` | `/help`, `/start` | send the pinned guide pointer |
| `headlines` | `/headlines`, `/top`, `/news` | list ~10 recent headlines + "Show 10 more" |
| `more` | ➡️ button | next headlines page |
| `photo` | a photo (no caption) | rebuild the draft's card with the photo as **background** (headline box, badge, and style kept; photo stored via `/setphoto` so restyles keep it) |
| `photocard` | a photo **with a caption** | standalone title card: the photo + the caption as headline, in the card templates; sent back as an image with a 🎨 button |
| `video` | a video | watermark + attach as native LinkedIn video |
| `style` / `restyle` | 🖼 Red frame / 📱 Social post | re-render the TAPPED draft (`/draft?id=`) in that style, same draft id |
| `back` | ↩️ Back to the card | restore the card on the original preview and delete the suggestion |
| `cardstyle` | 🎨 on a custom title card | re-render that card in the next template (state via `/draft?id=`; card ids start with `card` and `/latest` skips them, so custom cards never hijack the draft flows) |
| `xpost` | an x.com / twitter.com status link | card shows that real post; the text is the owner's take (§8) |
| `lilink` | a linkedin.com / lnkd.in link | not opened: reply asking for the original article or the post text |
| `link` | any other URL | draft a post about that article |
| `topic` | `/new <topic>` | draft about a topic |
| `news`/`again` | `/new`, 🔄 Different news | run the news pipeline |
| `feedback` | plain text | revise the current draft with the notes |
| `like` | 👍 | ack + show Publish / Save draft / Discard |
| `pub` | ✅ Publish now | post to LinkedIn (image or video) |
| `draft` | 📝 Save as draft | create a LinkedIn draft (the owner adds @tags + schedules natively) |
| `del`/`skip` | 🗑 / 👎 | discard |
| `clip` | 🎬 Clip from video | find the story's video, cut the relevant seconds, watermark it (§8) |
| `shot` | 📸 Article shot | screenshot the top of the source page as the post image |
| `pic` / `gallery` | 📷 Photo / 🖼 Gallery | one real photo, or a 2–4 photo grid, from Wikipedia/Commons |

Rule learned the hard way: **a callback prefix must never equal a message event name.** The
📷 Photo button used to send `photo:<id>`, which the photo-upload check caught first; the
upload branch then threw on the missing file and ended the whole listener run. The button is
now `pic:<id>` (old `photo:` taps are mapped to `pic`), and a failed photo download reports
itself instead of throwing.

### Media changes edit the preview in place

A style change, 📷 Photo, 🖼 Gallery or 📸 Article shot does **not** post a new photo plus the
post text and the whole keyboard again. It calls `editMessageMedia` on the preview (the
message right above the tapped buttons; helper `tg_edit_media`), so one draft stays two
messages. If the edit fails (message too old, deleted), it falls back to sending a new
preview. Suggestions that need a decision (a clip, a photo, a page shot) offer
👍 Like it / ↩️ Back to the card. Back is `back:<draft>:<style>:<buttons msg>`, so it can restore
the card on the draft's ORIGINAL preview and delete the suggestion messages.

Gotcha hit here: an IF right after an HTTP node sees that node's response as `$json`. Read
the tap from `$('Process updates')`, not `$json`.

### Status messages

Every slow request gets a status line at the bottom of the chat ("⏳ Capturing the article
page (about 10 seconds)…"), because a toast alone left the owner unsure whether a tap had
worked (it had: the preview far above was edited in place). `Process updates` attaches a
`status` text per event; `Needs status?` → `Send status` posts it, and `Back to event` hands
the event back to the routing IFs (they read `$json`). Endings: in-place edits turn it into
"✅ … The preview above was updated ⬆️"; paths that post new messages delete it; failures
become `editMessageText` of the status ("⚠️ reason") instead of extra messages.

### Sending media back to Telegram (important)

**Telegram's Bot API refuses to fetch its own `api.telegram.org/file/bot…` URLs** — a
`sendPhoto` with such a URL as `photo` returns `400: failed to get HTTP URL content`.
Combined with `onError: continue` on the send node, this made the photo-swap preview fail
*silently* for weeks: the swap itself worked, but the owner never saw a confirmation, so the
feature looked dead. Rule: **every media message to Telegram must be a binary re-upload**
(read the file back from the render service → binary `telegram` node), never a Telegram file
URL. Those URLs are still fine for *server-side* downloads (the render service, Chromium
backgrounds) — just not for Telegram itself to fetch.

### The button-reliability pattern (important)

Every button callback must:
1. **`answerCallbackQuery` immediately** — stops the Telegram spinner so there's no "loading"
   lag. (The first tap can still wait up to ~50s for the current long-poll to return; that's
   inherent to long-polling and affects all buttons equally.)
2. **Pass through the `/posted` idempotency gate before any slow/side-effecting work** — so a
   re-delivered or re-processed tap can't act twice.

Fast, read-only buttons (Like, Skip, Show more) only need the instant ack. **Slow or
side-effecting** buttons (Publish, 🔄 Different news) need **both** — because a long operation
can outlive the 65s claim TTL, letting a *second* listener spawn and reprocess the same tap.
The `/posted` gate makes that a no-op.

---

## 5. The shared "tail" (draft → card → preview)

`make_tail()` builds the sequence reused by the news/topic/link/revise/X-post branches:

```
Extract draft → Post ok? → Record used → Has note? → (Send note) →
Resolve image (/findimage) → Resolve speaker (/personphoto) → Build card HTML →
Render card → Send preview photo → Send approval buttons → delete the status message
```

- **Extract draft** parses Claude's JSON output into `{postText, hook, highlightPhrase,
  imageSubject, sourceUrl, note, articleImage, hashtags, quote…}` and **appends the hashtags**
  to the post text (see §9). A revision carries over the revised draft's `sourceUrl`,
  `articleImage`, photo and style (the revise prompt does not return them), and the article
  image is read only from the node the current event ran: the listener is one long execution,
  so `$('Clean article')` can still hold an older story's image.
- **Post ok?** guards against empty posts (never sends a blank card).
- **Record used** stores the story so it won't repeat (variety). Its running count also
  rotates the card style per draft.
- **Has note? / Send note** surfaces an honesty note when Claude couldn't verify something.
- **Build card HTML** renders the card (see §8), **Render card** screenshots it via the
  service, then it's sent to Telegram with the approval buttons + a `📎 Source:` link.

---

## 6. The render service (`render_service.py`)

Localhost-only Python `http.server` on `127.0.0.1:8099`, run as a systemd unit
(`linkedin-render`). Endpoints the workflows use:

| Endpoint | Purpose |
|----------|---------|
| `POST /render` | Chromium screenshot of card HTML → PNG; persists `<id>.html/.png/.json` |
| `GET /card?id=` | the stored PNG (magic-byte content-type sniff for png/jpeg) |
| `GET /draft?id=` / `GET /latest` | the stored draft JSON |
| `POST /setphoto` / `GET /photo?id=` | store a Telegram photo locally as a card background (survives the ~1h Telegram file-URL expiry) |
| `POST /video` | download a Telegram video, ffmpeg-watermark it (`in/<handle>` blue badge) |
| `POST /uploadvideo` / `GET /vidfile` | presigned multipart PUT to LinkedIn / fetch the mp4 |
| `POST /claim` | **single-listener lock** (atomic, token + TTL, explicit release) |
| `POST /posted` | **idempotency gate** (`{first:true}` once, then `false`) |
| `GET/POST /used` | recently-used stories (news variety, 3-day rolling, persisted to disk) |
| `GET /findnews?title=` | when a pasted link is blocked: search Bing News, return the first readable version |
| `GET /findimage?article=&subject=` | a verified loadable card photo (article image if it loads, else Wikipedia/Commons) |
| `GET /personphoto?name=` | a quoted person's photo, only when Wikipedia is clearly about them |
| `GET /xpost?url=` | a real X post from X's public embed data |
| `POST /photos` | 📷 Photo / 🖼 Gallery: real photos composed into the card slot |
| `POST /shot` | 📸 Article shot: screenshot of the top of the source page |
| `POST /autoclip` | 🎬 Clip: pick the video, read its transcript, cut and watermark the moment |
| `GET /health` | health check |

Lower-level helpers (`/ytsearch`, `/transcript`, `/clip`, `/grabframe`, `/mediaprobe`,
`/automedia`, `/setcard`, `/avatar`, `/debug`) are there for testing and manual use.

It also **auto-prunes** `liagent_*` media older than 48h every hour. Buttons on a pruned draft
say the draft expired instead of failing with "no subject".

The fallback watermark handle is `LINKEDIN_HANDLE` from the environment (the systemd unit
sets it) or a `config.env` next to the script; n8n sends the configured handle with every
request anyway.

### Why the lock + idempotency gate exist

Early on, the agent **posted to LinkedIn twice**. Root cause: n8n's
`$getWorkflowStaticData` only persists at *execution end*, so overlapping poll executions
(reactivations + Telegram at-least-once delivery) both grabbed the same callback and both
posted. Fixes: **`/claim`** ensures only one listener ever runs; **`/posted`** ensures a
draft can post only once no matter what. Duplicate posting is now impossible.

---

## 7. News sources & filtering

**Tech feeds (9):** Ars Technica, The Verge, The Register, VentureBeat, MIT Technology
Review, TechCrunch, SiliconANGLE, The Next Web, Data Center Dynamics.
**Stock/market feeds (2):** CNBC Technology, Motley Fool.

Key decisions:
- **Tech vs market balance:** the stock feeds were drowning out actual tech news. Market
  items (by host: `fool.com`, `cnbc.com`) are capped at **3 per page of 10** in the headlines
  list and **6 of 24** in the picker pool, and in the pool they must be about tech (a biotech
  stock story got picked once).
- **TechCrunch + DCD article pages are sometimes Cloudflare-gated** — safe to include because
  of the graceful-degradation + `/findnews` fallbacks: a blocked fetch writes from the RSS
  summary, and a blocked pasted link is re-found on a readable outlet. All other feeds serve
  full articles to a plain HTTP fetch, verified from the actual fetch origin.
- **Google News RSS is unusable** — its links redirect to a homepage, not the article.
- **Reviews/commerce filtered out** (product reviews, hands-on, buying guides, deals, gift
  guides, roundups) by URL + title heuristics — the owner doesn't post about gadget reviews.
- **Formulaic finance filler filtered** (Motley Fool's SEC insider-filing posts and raw
  earnings-call transcripts) — Fool posts these constantly and they'd flood the feed.
- **Per-source cap** so a high-frequency feed can't crowd out the others.
- The article fetch uses a **real Chrome User-Agent** (an early custom UA had "Bot" in it,
  which some walls flag).

### Pasted links

General links **degrade gracefully**: if the body is thin (paywall/JS-gated) but there's a
usable title+summary (`og:` tags first), it writes from that and says so in a note. If a link
is fully **blocked** (e.g. a consent wall), the bot says so and the render service's
**`/findnews`** searches Bing News for the headline (from `og:title` / the URL slug), follows
the results to the **first readable publisher version** of the same story, and writes the
post from that. Only when even that fails does it return "Link unreadable".

**LinkedIn links are never fetched.** A `linkedin.com` or `lnkd.in` URL is classified as
`lilink` before the generic link rule, and the bot replies asking for the original article
link, or `/new` followed by the post text. The agent's only LinkedIn traffic is the official
API for the owner's own posts.

---

## 8. Card design & styles

Cards are 1080×1080 Chromium screenshots of HTML (Sora for Red frame, Inter for Social post).
A shared `renderCard(d, style)` renders one of two templates; the style buttons re-render the
*same* draft (same photo/text) in the chosen style. New drafts rotate between the two, and a
revision keeps the look of the draft it revises.

- **Style 0 — "red frame":** the article's own photo as the background, with a **white box +
  red border** holding the headline in **bold black** (no blue highlight — that read as
  AI-generated). The `in/<handle>` LinkedIn badge sits bottom-right.
- **Style 1 — "social post":** a real-looking **X post screenshot in dark mode** (an earlier
  white card read as fake: bold display font, a headline, no timestamp or actions).
  Inter at regular weight (installed on the render host with `fonts-inter`, plus
  `fonts-noto-color-emoji`), avatar + name + handle (`@X_HANDLE` if configured, else
  `in/<LINKEDIN_HANDLE>`), the hook as a sentence, the article photo with its real domain label,
  the time the card was made, a divider, and Lucide action icons. Honesty rule: only true
  details — **no invented like/repost/view counts** (a brand-new post shows icons without
  numbers). A blue verified badge sits next to the name (`VERIFIED_BADGE=0` turns it off).
  **Never render the card as posted by someone else** (a CEO, company or analyst from the
  story): a real-looking post with words they never wrote is impersonation; their real quotes
  can only appear as attributed quotes.

Two styles that were plain colour + text (a big number, a typographic card) were removed:
without a photo they looked empty and read as AI-made.

### Quote cards

When the story quotes someone, the social post card becomes **the owner's post quoting
them**: “exact words” — Name, role. Claude returns `quote`, `quote_speaker`, `quote_role`;
`Extract draft` keeps the quote ONLY if it appears in the article word for word (case,
punctuation and entities normalised) and the speaker's surname appears within 250 chars of
one of its occurrences (a pull quote in the summary line repeats it far from the
attribution). Topic drafts never get a quote; revisions keep it. The speaker's photo comes
from `/personphoto`, only when the Wikipedia page title contains every part of the name and
the image is a real JPEG; otherwise the article photo is used. The preview caption says the
quote was checked word for word.

### Real X posts

Paste an x.com / twitter.com status link: `/xpost` reads the post from X's public embed data
(the same source X's embed widget uses): author, handle, verified type, text (t.co links
expanded), original time, real like count, photo. The card is that post drawn in the X layout
with its REAL data, and Claude writes the owner's take on it, told today's date and the exact
"how long ago" wording, and to use ONLY what the post says (from memory it once added a wrong
statistic). Deleted or private posts fail with a clear message.

### Card image logic

`/findimage` makes sure the card **always** has a background: use the article's own
`og:image` **only if it actually loads** (many CDNs answer server-side fetches with an error,
which used to leave the card blank). If it doesn't load, fall back to a relevant photo from
**Wikipedia**, then **Wikimedia Commons**, searched by `imageSubject` (a person/campus/product
Claude picks), JPEG only so diagrams and charts don't slip in; final fallback is a dark
gradient. Name/handle/avatar come from config (`DISPLAY_NAME`, `LINKEDIN_HANDLE`, `AVATAR_URL`).

### Other media options

- **📷 Photo / 🖼 Gallery** (`/photos`): one real photo, or a grid of 2–4, from the article
  image, Wikipedia and Commons. Hard time budget (it runs inside the 65 s claim), near-black
  and near-duplicate tiles rejected.
- **📸 Article shot** (`/shot`): Chromium screenshot of the top of the source page.
  `--timeout=7000` instead of `--virtual-time-budget`, because ad-heavy pages never go idle.
  Refuses a draft with no real source link (an empty URL renders Chromium's start page).
- **🎬 Clip from video** (`/autoclip`): YouTube search via yt-dlp with progressively shorter
  queries (a long headline full of figures returns nothing), prefer the channel named in the
  story, find the transcript cue with the most story keywords, download only that window
  (`--download-sections`), burn in a credit line and the handle badge. The clip sets `isVideo`
  on the draft so Publish posts it as a video; a later photo or shot clears it.

---

## 9. Voice, hooks, and hashtags

### Voice (the Claude SYSTEM prompt)

Informative-first with a **light, subtle take** (not preachy); short (~90–140 words);
airy spacing with a short bullet list of hard numbers, one soft take, and a varied ending (a
closing question at most one post in four). Hard rules (each an "AI tell" to avoid): **never use
the dash as punctuation**, no empty cliché filler lines, no recurring sign-off formulas, only
real numbers from the source, and **always write money with `$` and percentages with `%`**
(symbols are more eye-catching).

- **Hooks must name the subject.** A hook like "$96.2B revenue, up 106%" is meaningless on a
  card — it must say *Nvidia* did it. The hook rule requires the company/person/product.
- **Hashtags are a dedicated required field**, not tacked onto the prose. The code appends 5–8
  contextual tags to every post — reliable even when the model would otherwise forget on a
  long post.

> ⚠️ **The SYSTEM prompt must contain NO apostrophes/single quotes.** It's wrapped in single
> quotes when embedded in the Claude node body, so one apostrophe breaks the request. A
> generator `assert` enforces this.

---

## 10. Publishing to LinkedIn

- **Images:** the n8n LinkedIn node (`shareMediaCategory=IMAGE`, binary from `/card`).
- **Video:** the raw Posts API — `/rest/videos` initialize/finalize + `/rest/posts`, headers
  `LinkedIn-Version: 202608` + `X-Restli-Protocol-Version: 2.0.0`. The n8n node is image-only.
  Telegram bot downloads cap at ~20 MB, so clips must be short.
- **Save as draft:** `POST /rest/posts` with `lifecycleState: DRAFT` — surfaces in the
  composer so the owner can add @tags and schedule natively (editing a *published* post hurts
  reach). Drafts are **text-only** (image re-attached from the Telegram preview).
- All raw LinkedIn calls use `httpRequest` with `predefinedCredentialType: linkedInOAuth2Api`
  (injects the OAuth Bearer). The raw token is **never** extracted.
- **@tagging isn't automated** — person/company mentions need URNs that can't be looked up by
  name; that's what Save-as-draft is for.

---

## 11. Deploy workflow (and its gotchas)

Regenerate → import → (restart the render service if its lock/state should clear):

```bash
python3 build_wf.py wf_a.json wf_b.json
python3 import_workflows.py
ssh <n8n-host> "systemctl restart linkedin-render"   # clears /claim so a fresh listener starts
```

Hard-won gotchas:
- **WF-B doesn't go live instantly.** The old listener runs its own execution for up to
  15 min. To force the new version: stop the running listener execution, then
  deactivate → patch → activate, then restart `linkedin-render`. `import_workflows.py` stops the
  running listener automatically.
- **Stop the listener BEFORE deactivating**, or the deactivate can hang.
- **Activation needs `{versionId}`** via `POST /rest/workflows/{id}/activate` (PATCH
  `{active:true}` silently no-ops). The versionId changes on every PATCH.
- **A `[ -n "$RUN" ] && …` bash chain will abort the whole deploy** when no listener is
  currently running (it self-capped). Make the stop step a proper `if`, don't `&&`-chain it.
- **`config.env` must exist** for `build_wf.py` to generate (it fails fast otherwise).
- **Escape regexes in generated JS.** Code nodes live inside Python strings: a `\b` that is not
  written as `\\b` becomes a backspace character, and the regex silently matches far less.
- **Testing through Telegram Web:** inline buttons ignore `element.click()`; drive them with
  real mouse clicks.

---

## 12. Cost & security

- **Claude:** Haiku 4.5, a few short calls per draft — negligible.
- **LinkedIn:** official API only, free for posting as yourself.
- **Security:** everything runs locally; **no inbound ports**; approvals are outbound-only.
  Keep `config.env` and the n8n encryption key private and backed up. The repo contains no
  secrets (`.gitignore` covers `config.env`, cookies and the generated workflow JSON, which
  embeds the bot token).

---

## 13. How it got here (short version)

1. **Core pipeline** — scheduled news draft → branded card → Telegram preview + approve.
2. **Render service** — replaced the unavailable `executeCommand` with a localhost Chromium
   service; later added ffmpeg, locks, and state endpoints.
3. **Voice v2** — informative-first + subtle take, no dashes, number-led hooks, airy formatting.
4. **Continuous listener + dup-post fix** — single `/claim` lock + `/posted` idempotency;
   near-instant buttons.
5. **Input types** — paste a link, `/new <topic>`, `/new` (news), send a photo, send a video,
   plain text (revise). Pinned guide + command menu.
6. **Storage janitor**, **Save as draft**, **source link + honesty note**.
7. **News v2** — two-pass (pick fresh story → read full article), variety tracking,
   🔄 Different news, detailed breakdowns.
8. **Reliability** — instant-ack + idempotency for slow buttons; the general button pattern.
9. **`/headlines`**, then the schedule switched from auto-drafting to the headlines digest.
10. **Blocked links find the story elsewhere** (`/findnews`) + verified card backgrounds
    (`/findimage`).
11. **Custom title cards**, **photo swap as background replacement**, binary re-upload rule.
12. **Media options** — clip from video, article shot, real photos and galleries; previews
    edited in place; ↩️ Back to the card.
13. **Social post card as an X post screenshot**, **verified quote cards**, **real X posts**.
14. **Status message for every slow request.**

---

*Generator: `build_wf.py`. Everything here is produced by it plus `render_service.py`. To
change behavior, edit those, regenerate, and redeploy — never hand-edit the workflow JSON.*
