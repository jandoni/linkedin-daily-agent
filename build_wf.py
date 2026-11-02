import json, uuid, sys, os
# Two workflows. Render + lock + idempotency via a localhost service.
#   argv[1] = WF-A (scheduled headlines digest)
#   argv[2] = WF-B (continuous listener: poll & act + revise + link/topic/news + headlines)
# All secrets / instance IDs are loaded from config.env (see config.example.env). Nothing sensitive is hardcoded.
OUT_A = sys.argv[1] if len(sys.argv) > 1 else "wf_a.json"
OUT_B = sys.argv[2] if len(sys.argv) > 2 else "wf_b.json"

def _load_config():
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(here, "config.env")
    if os.path.exists(path):
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
_load_config()
def _cfg(key, default=""):
    return os.environ.get(key, default)

LINKEDIN_CRED = _cfg("LINKEDIN_CRED")     # n8n credential IDs — created in the n8n UI, differ per install
TELEGRAM_CRED = _cfg("TELEGRAM_CRED")
CLAUDE_CRED   = _cfg("CLAUDE_CRED")
CHAT_ID       = _cfg("CHAT_ID")           # your Telegram chat id
PERSON_URN    = _cfg("PERSON_URN")        # LinkedIn person URN (the 'sub' from GET /v2/userinfo)
LINKEDIN_HANDLE = _cfg("LINKEDIN_HANDLE", "your-handle")   # the slug in linkedin.com/in/<slug>/ — shown on the card + video watermark
DISPLAY_NAME  = _cfg("DISPLAY_NAME", "Your Name")         # full name shown on the social-post card style
VERIFIED_BADGE = _cfg("VERIFIED_BADGE", "1") not in ("0", "false", "no", "")   # blue check next to the name on the social post card
X_HANDLE      = _cfg("X_HANDLE", "").lstrip("@")           # optional: your real X handle for the post-screenshot card; blank = in/<LINKEDIN_HANDLE>
AVATAR_URL    = _cfg("AVATAR_URL", "")                    # optional profile photo URL for the social-post card; blank = monogram
AUTHOR_BIO    = _cfg("AUTHOR_BIO", "a technology professional who writes about cloud and AI")   # one line about you, used in the drafting prompt
TIMEZONE      = _cfg("TIMEZONE", "America/Chicago")       # IANA zone for the timestamp on social-post cards
_nw = [w for w in DISPLAY_NAME.split() if w]
INITIALS      = ((_nw[0][0] + _nw[-1][0]) if len(_nw) >= 2 else (_nw[0][:2] if _nw else "JB")).upper()
NSTYLES       = 2                                          # card templates: 0 red frame, 1 social post (big number + typographic removed 2026-11-01: plain colour + text read as AI)
AUTO_STYLES   = [0, 1]                                     # styles a new draft rotates through (the ones with a photo)
STYLE_NAMES   = ["🖼 Red frame", "📱 Social post"]
TG_TOKEN      = _cfg("TG_TOKEN")          # Telegram bot token
CONTACT_EMAIL = _cfg("CONTACT_EMAIL", "bot@example.com")   # polite UA contact for the Wikipedia image API
TG_API        = "https://api.telegram.org/bot" + TG_TOKEN
TG_FILE       = "https://api.telegram.org/file/bot" + TG_TOKEN + "/"

if __name__ == "__main__" and not TG_TOKEN:
    sys.exit("ERROR: config.env missing or TG_TOKEN empty. Copy config.example.env -> config.env and fill it in.")
RENDER_SVC    = "http://127.0.0.1:8099"
FILE_RESP     = {"response":{"response":{"responseFormat":"file","outputPropertyName":"data"}}}
TEXT_RESP     = {"response":{"response":{"responseFormat":"text"}},"timeout":20000}
REAL_UA       = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
WIKI_URL      = "=https://en.wikipedia.org/w/api.php?action=query&format=json&generator=search&gsrsearch={{ encodeURIComponent($('Extract draft').first().json.imageSubject) }}&gsrlimit=3&prop=pageimages&piprop=thumbnail&pithumbsize=1200"

def nid(): return str(uuid.uuid4())
def node(name, ntype, tv, pos, params, extra=None):
    n = {"parameters": params, "id": nid(), "name": name, "type": ntype, "typeVersion": tv, "position": pos}
    if extra: n.update(extra)
    return n
def sticky(name,content,pos,w,h,color):
    return node(name,"n8n-nodes-base.stickyNote",1,pos,{"content":content,"height":h,"width":w,"color":color})
def tg_post(label,endpoint,pos,json_expr):
    return node(label,"n8n-nodes-base.httpRequest",4.2,pos,
        {"method":"POST","url":TG_API+"/"+endpoint,"sendBody":True,"specifyBody":"json","jsonBody":json_expr,"options":{}},
        extra={"onError":"continueRegularOutput"})
# Swap the image of the draft's preview message in place (editMessageMedia) instead of posting a
# new photo plus the whole post text and buttons again: a media change does not touch the text.
# The preview is the message right above the buttons message the tap came from (the bot always
# sends the two back to back). Any failure (message too old, deleted) returns ok=false and the
# caller falls back to sending a new preview.
def tg_edit_media(label,pos,caption_expr,kind="photo"):
    return node(label,"n8n-nodes-base.httpRequest",4.2,pos,
        {"method":"POST","url":TG_API+"/editMessageMedia","sendBody":True,"contentType":"multipart-form-data",
         "bodyParameters":{"parameters":[
            {"parameterType":"formData","name":"chat_id","value":"={{ $('Process updates').first().json.chatId }}"},
            {"parameterType":"formData","name":"message_id","value":"={{ Number($('Process updates').first().json.orig || $('Process updates').first().json.messageId) - 1 }}"},
            {"parameterType":"formData","name":"media","value":"={{ JSON.stringify({ type: '"+kind+"', media: 'attach://file', caption: "+caption_expr+" }) }}"},
            {"parameterType":"formBinaryData","name":"file","inputDataFieldName":"data"}]},
         "options":{}},
        extra={"onError":"continueRegularOutput"})
def claude_node(label,pos,body):
    return node(label,"n8n-nodes-base.httpRequest",4.2,pos,
        {"method":"POST","url":"https://api.anthropic.com/v1/messages",
         "authentication":"genericCredentialType","genericAuthType":"httpHeaderAuth",
         "sendHeaders":True,"headerParameters":{"parameters":[{"name":"anthropic-version","value":"2023-06-01"}]},
         "sendBody":True,"specifyBody":"json","jsonBody":body,"options":{}},
        extra={"credentials":{"httpHeaderAuth":{"id":CLAUDE_CRED,"name":"Anthropic (Claude) x-api-key"}}})
def IF_EQ(field,val):
    return {"conditions":{"options":{"caseSensitive":True,"typeValidation":"strict","version":2},
      "conditions":[{"id":nid(),"leftValue":"={{ $json."+field+" }}","rightValue":val,"operator":{"type":"string","operation":"equals"}}],"combinator":"and"},"options":{}}
def IF_OR(field,v1,v2):
    return {"conditions":{"options":{"caseSensitive":True,"typeValidation":"strict","version":2},
      "conditions":[
        {"id":nid(),"leftValue":"={{ $json."+field+" }}","rightValue":v1,"operator":{"type":"string","operation":"equals"}},
        {"id":nid(),"leftValue":"={{ $json."+field+" }}","rightValue":v2,"operator":{"type":"string","operation":"equals"}}],
      "combinator":"or"},"options":{}}
def IF_BOOL(field):
    return {"conditions":{"options":{"caseSensitive":True,"typeValidation":"strict","version":2},
      "conditions":[{"id":nid(),"leftValue":"={{ $json."+field+" }}","rightValue":True,
        "operator":{"type":"boolean","operation":"true","singleValue":True}}],"combinator":"and"},"options":{}}
def IF_NOTEMPTY(field):
    return {"conditions":{"options":{"caseSensitive":True,"typeValidation":"loose","version":2},
      "conditions":[{"id":nid(),"leftValue":"={{ $json."+field+" }}","rightValue":"",
        "operator":{"type":"string","operation":"notEmpty","singleValue":True}}],"combinator":"and"},"options":{}}

# ================================================================ Claude voice prompt
SYSTEM = (
 "You are " + DISPLAY_NAME + ", writing first-person LinkedIn posts in your own voice. You are " + AUTHOR_BIO + ". You write for engineers, PMs and tech leaders in cloud, AI, data and infrastructure, plus a broader business audience.\\n\\n"
 "HOW YOU WRITE (match this closely):\\n"
 "1) INFORMATIVE FIRST. You explain an interesting or under-covered tech or business story clearly, using concrete facts, real numbers, dollar figures and named companies. You teach the reader something they did not know.\\n"
 "2) A LIGHT, SUBTLE TAKE. You are not preachy and you do not lecture. You mostly report the story, then add ONE soft personal read, usually near the end, written fresh for THIS story in your own words. Stay fairly balanced, not a hot take.\\n"
 "3) SHORT and SKIMMABLE. Usually 60 to 140 words. Never pad.\\n"
 "4) HUMAN, not AI. Plain, direct language.\\n\\n"
 "STRUCTURE (lay it out exactly like this, very airy and easy to skim, the way the sharpest tech writers on LinkedIn do):\\n"
 "- Open with the hook on its own line: one punchy statement, ideally leading with a concrete number. Optionally end it with ONE tasteful emoji, or none. Breaking news may start with JUST IN:.\\n"
 "- Put a BLANK LINE between every block. One idea per line.\\n"
 "- When the story has specifics, use a short list of 2 to 5 points. Start each item with a plain bullet character (a dot) and keep each item to ONE short line built around a hard number or fact. Put a blank line between the list items.\\n"
 "- DIG INTO THE SOURCE, do not just restate the headline number. Break it down with the SPECIFIC figures from the article: who spends or gets what, the split by company or category, percentages, dollar amounts. For example not just 420 billion on AI, but WHERE it goes (this much from one company, that much on memory chips, this share to another). The bullets carry those concrete sub-numbers. If the article gives a breakdown, your post must reflect it.\\n"
 "- Then your subtle take on one short line (what it really means).\\n"
 "- VARY THE ENDING. Most posts simply end on that take, or on a concrete fact, with no question at all. Use a closing question in at most one post out of four, and only when it is specific to this story and genuinely open. Never end with a generic invitation to comment.\\n"
 "- EMOJI: at most one or two in the entire post, tasteful, never emoji spam. Do not over-format.\\n"
 "- Never dump links, and never say things like comment X to get it. No lead magnets.\\n\\n"
 "Your real winning hooks sound like this (curiosity plus something specific): Everyone is talking about AI replacing jobs. IKEA proved that is a lazy strategy. / Every CAPTCHA you solved for the last 20 years was unpaid work for Google. / Microsoft did not release the best AI model. Microsoft released a smaller cheaper one. Keep that energy in the opening line of the post, then keep the rest grounded and informative.\\n\\n"
 "HARD RULES (breaking any of these makes it read as AI written, which you never want):\\n"
 "a) NEVER use the dash as punctuation. No em dash, no en dash, no spaced hyphen used as a break. Use separate short sentences, or a comma, or a colon instead.\\n"
 "b) Do not use empty AI-cliche filler lines like: This is the real story nobody is talking about. or And that changes everything. Short standalone lines are GOOD, but only when each one carries a real fact or a real opinion, never empty hype.\\n"
 "c) Only use numbers, dollar figures and facts that actually appear in the provided source material. Never invent a figure. ALWAYS write money with the $ symbol and percentages with the % symbol (for example $420 billion, $41M, $449, 154%, up 12%). Never spell them out as dollars, percent or percentage. The symbols are more eye catching, and this matters most in the hook that appears on the image.\\n"
 "d) No buzzword openers (in todays fast paced world), no cliches (game changer), no hype, no salesy tone.\\n"
 "f) NEVER reuse a recurring sign off formula. These are banned outright, including any close rewording: What do you think, What do you think about this, Thoughts, Curious what others think, Agree or disagree, Am I wrong, The way I see it, What stands out to me, This is what clicked for me, Here is the thing, Let that sink in, The real story here, Make of that what you will. Repeating the same closing shape post after post is the single clearest tell of an automated account, so each post must end differently from the last one.\\n"
 "g) Do not open two posts the same way either. Rotate how you start: a number, a named company doing something, a short factual statement, a contrast. Avoid starting with a question.\\n"
 "e) ALWAYS provide 5 to 8 hashtags in the separate hashtags field (NOT inside the post body). Choose them to fit THIS specific post: its exact topic and the companies, products and technologies actually named in it. Use real, widely used professional tags (for example #AI #DataCenters #CloudComputing #Azure #Nvidia #Semiconductors #MachineLearning). They must be genuinely relevant, never generic filler, and never repeat the same word. The hashtags field is REQUIRED and must never be empty.\\n\\n"
 "TOPICS YOU POST ABOUT: AI and its impact on jobs and society, big tech and cloud strategy (especially Microsoft, Azure, Nvidia, OpenAI), and under appreciated tech and infrastructure stories. If the source is a dry product announcement, lead with why it matters, do not list specs.\\n\\n"
 "OUTPUT FORMAT: Respond with ONLY a single JSON object, no preamble and no code fences. Keys: post, hook, highlight_phrase, image_subject, source_url, note, hashtags, quote, quote_speaker, quote_role.\\n"
 "post = the full LinkedIn post text, about 90 to 140 words and never more than 160, following every rule above. Do NOT put hashtags inside the post; they go in the separate hashtags field.\\n"
 "hook = ONE cohesive scroll stopping headline for the image card, written as ONE single statement (never two separate sentences that look like two different titles). IT MUST NAME THE MAIN SUBJECT of the story: the company, person or product it is about (for example Nvidia, Microsoft, Jensen Huang, OpenAI, Azure). A reader who sees ONLY the card must instantly know WHO or WHAT it is about. NEVER write a hook that is only numbers with no named subject. Bad, only numbers with no company named: $96.2B revenue up 106%. Good, subject named: Nvidia just posted $96.2B in revenue up 106%. When the story has a concrete number or dollar figure, LEAD WITH IT or put it right after the subject, and write it with the SYMBOL not words (for example: Nvidia just wired OpenAI $100 billion, or Azure margins hit 72%). Use $ for money and % for percentages, never the words dollars or percent. Specific and curiosity driving. Only use figures found in the source. At most about 70 characters. Plain text, no hashtags, no quotation marks, no dash, no hyphenated words.\\n"
 "highlight_phrase = the single most eye catching part OF THE HOOK to color in blue, for example the number or dollar figure, or a punchy two to four word kicker. It MUST be copied EXACTLY as it appears inside hook (an exact substring of hook), so it can be highlighted inside the title. Choose the words that grab the eye. Use an empty string only if nothing stands out.\\n"
 "image_subject = the subject for the card background photo. Pick whatever best fits THIS specific story and will return a strong real PHOTOGRAPH. Do NOT always default to a face. Depending on the story, choose the most visually relevant option: a well known person such as a CEO or founder (Jensen Huang, Satya Nadella, Sam Altman, Sundar Pichai, Andy Jassy, Dario Amodei, Lisa Su); a company campus or building (Googleplex, Apple Park, Microsoft campus); a data center; a specific product or chip (Nvidia H100, iPhone); or another iconic, visually relevant thing. Vary it based on the topic. HARD REQUIREMENTS: it must be a real thing that has a Wikipedia page with an actual photograph, and it must NOT be something whose only image is a plain logo. If the key company is obscure or known mainly by a logo (a hedge fund, a quant firm, a broker, a small startup), use its CEO, its campus, or a related data center or product instead. Give one concrete searchable name, one to four words (for example: Jensen Huang, or Googleplex, or data center, or Nvidia H100).\\n"
 "source_url = the exact source link of the specific item you wrote about, copied VERBATIM from the provided material (the link field of the news item you chose, or a SOURCE URL line in the article text). Use an empty string only if the material truly has no link. Never make up a URL.\\n"
 "note = normally an EMPTY string. But if the material or the request needs something you cannot provide or verify (for example a specific number, spec or fact that is simply not present in the source), do NOT invent it and do NOT silently ignore it. Instead put ONE short honest sentence here stating exactly what you could not find or verify. Only fill note when something is genuinely missing.\\n"
 "quote = a direct quotation that appears in the provided material inside quotation marks, said by a named person in the story (a CEO, founder, executive, analyst, researcher, official, expert or lawyer, preferring the most senior or most quotable one), copied WORD FOR WORD, at most about 30 words, the most telling line. Never paraphrase, shorten inside the quote, merge two quotes or invent one. Whenever the material contains a direct quote from a named person, include the best one. Only if it contains none, return an empty string. quote_speaker = that person full name exactly as written in the material. quote_role = their title and organisation in a few words, for example CEO of Nvidia or analyst at Wedbush. Both empty when quote is empty.\\n"
 "hashtags = REQUIRED, never empty. A single line of 5 to 8 relevant hashtags for THIS post, space separated, each starting with #, in natural CamelCase, chosen from the topic and the companies, products and technologies named in the post (for example: #AI #DataCenters #Nvidia #CloudComputing #Semiconductors #Azure). Genuinely relevant, no repeats, no generic filler."
)
assert "'" not in SYSTEM, "SYSTEM must contain NO single quotes/apostrophes (SYS_Q wraps it in single quotes for the n8n expression; an apostrophe breaks the Claude node body)."
SYS_Q = "'" + SYSTEM + "'"   # single-quote-safe: the assert above guarantees no apostrophes in SYSTEM

revise_body = ("={{ JSON.stringify({ model: 'claude-haiku-4-5', max_tokens: 1600, system: " + SYS_Q + ", "
    "messages: [ "
      "{ role: 'user', content: 'Here is a LinkedIn post you already drafted as Jon, with its image card fields.\\n\\nPOST:\\n' + $json.postText + "
      "'\\n\\nCARD HOOK: ' + $json.hook + '\\nHIGHLIGHT PHRASE: ' + $json.highlightPhrase + '\\nIMAGE SUBJECT: ' + $json.imageSubject + "
      "'\\n\\nJon just sent this feedback:\\n' + $('Process updates').first().json.feedback + "
      "'\\n\\nApply his feedback and return the revised JSON object with keys post, hook, highlight_phrase, image_subject in the same format and following all the rules. Keep his voice, keep the factual numbers unless he asked to change them, and change only what the feedback asks for.' }, "
      "{ role: 'assistant', content: '{' } ] }) }}")
# a REAL X post the owner pasted: the card shows that post itself (author, text, time, real likes), and the
# LinkedIn text is his take on it. Its words are its author's, so the writer must not add quotes of its own.
xpost_body = ("={{ JSON.stringify({ model: 'claude-haiku-4-5', max_tokens: 1600, system: " + SYS_Q + ", "
    "messages: [ "
      "{ role: 'user', content: 'Write ONE LinkedIn post in my voice reacting to this real post on X. Give my take: why it matters for a professional audience and what it signals. Use ONLY what the post says: never add statistics, user numbers, money figures, dates or events that are not in the post itself (from memory they come out wrong). Leave quote, quote_speaker and quote_role empty (the image already shows the post itself). The source_url to return is exactly: ' + $('Get X post').first().json.url + "
      "'\\n\\nTODAY: ' + $now.toFormat('yyyy-MM-dd') + ' (if the post is not from the last few days, write about it as a past moment and never call it new or say just)' + '\\nAUTHOR: ' + $('Get X post').first().json.name + ' (@' + $('Get X post').first().json.handle + ')\\nPOSTED: ' + $('Get X post').first().json.created + ' (that is ' + $('Get X post').first().json.ago + '; when you say when it was posted, use exactly those words and never add today)' + '\\nPOST: ' + $('Get X post').first().json.text + "
      "'\\n\\nThen return the JSON object described in the system prompt.' }, "
      "{ role: 'assistant', content: '{' } ] }) }}")
topic_body = ("={{ JSON.stringify({ model: 'claude-haiku-4-5', max_tokens: 1600, system: " + SYS_Q + ", "
    "messages: [ "
      "{ role: 'user', content: 'Write ONE LinkedIn post in my voice about this topic or angle I want to cover:\\n' + $('Process updates').first().json.payload + "
      "'\\n\\nThen return the JSON object described in the system prompt.' }, "
      "{ role: 'assistant', content: '{' } ] }) }}")
link_body = ("={{ JSON.stringify({ model: 'claude-haiku-4-5', max_tokens: 1600, system: " + SYS_Q + ", "
    "messages: [ "
      "{ role: 'user', content: 'Write ONE LinkedIn post in my voice about THIS specific article. The source_url to return is exactly: ' + $('Process updates').first().json.payload + '\\n\\nWhen the full body text is present, write from it and USE its specific numbers and breakdown (who spends or gets what, the split, percentages, amounts). If the body is thin (a paywall or JS-gated page) but a TITLE and SUMMARY are present, write a solid post from those instead, and add a short note in the note field saying the post is based on the article summary because the full text was not accessible. Do not invent facts, and NEVER return an empty post.\\n\\nARTICLE:\\n' + String($json.article || '').slice(0,7000) + "
      "'\\n\\nThen return the JSON object described in the system prompt.' }, "
      "{ role: 'assistant', content: '{' } ] }) }}")
# fallback: the pasted link was blocked; we found the SAME story on a readable outlet -> write from that
alt_link_body = ("={{ JSON.stringify({ model: 'claude-haiku-4-5', max_tokens: 1600, system: " + SYS_Q + ", "
    "messages: [ "
      "{ role: 'user', content: 'Write ONE LinkedIn post in my voice about this story. The link the user shared was blocked, so this is the SAME story from a different outlet. The source_url to return is exactly: ' + $json.url + '\\n\\nUSE the specific numbers and breakdown from the material below. Do not invent facts, and NEVER return an empty post.\\n\\nARTICLE:\\n' + String($json.article || '').slice(0,7000) + "
      "'\\n\\nThen return the JSON object described in the system prompt.' }, "
      "{ role: 'assistant', content: '{' } ] }) }}")
# build a search headline from the blocked page (prefer og:title, else the URL slug)
get_headline_js = """const payload = String($('Process updates').first().json.payload || '');
let ogt = '';
try { ogt = String($('Clean article').first().json.title || ''); } catch(e) {}
let slug = '';
try {
  slug = (payload.split('?')[0].split('#')[0].replace(/\\/$/,'').split('/').pop() || '');
  slug = slug.replace(/\\.(html?|php|aspx)$/i,'').replace(/[-_]\\d{5,}.*$/,'').replace(/[-_]+/g,' ').trim();
} catch(e) {}
const generic = /^(yahoo|google|bing|msn|breaking news|latest news|home|news)\\b/i;
let title = (ogt && ogt.length >= 15 && !generic.test(ogt)) ? ogt : slug;
if (!title || title.length < 8) title = slug || ogt;
return [{ json: { title: title } }];"""
# --- two-pass news: pick a fresh story from headlines, then write from its full article ---
pick_body = ("={{ JSON.stringify({ model: 'claude-haiku-4-5', max_tokens: 300, "
    "system: 'You select ONE news story for me to write a LinkedIn post about. The candidates are a JSON array; the FIRST item is index 0. Return ONLY a JSON object with keys index and url: index = the 0-based array position of your chosen candidate, url = that candidate link field copied verbatim. Choose the single most interesting and substantive RECENT story about AI, big tech, cloud, chips, or tech companies and their stock or earnings, preferring ones with concrete numbers, dollar figures and named companies. DO NOT choose product reviews, hands-on articles, buying guides, deals, gift guides, how-to pieces or gadget roundups (a review of a phone, laptop, TV, headphones, car, etc.) — pick real news: company strategy, funding, earnings, market moves, launches that matter, AI, cloud, chips, or policy. You MUST NOT choose any story whose link or title appears in the AVOID list.', "
    "messages: [ { role: 'user', content: 'CANDIDATES (JSON array, index 0 first):\\n' + JSON.stringify($('Drop used').first().json.stories) + '\\n\\nAVOID (recently used, do NOT pick any of these):\\n' + JSON.stringify($json.recent) + '\\n\\nReturn {index, url}.' }, "
    "{ role: 'assistant', content: '{' } ] }) }}")
extract_pick_js = """const raw = ($json && $json.content && $json.content[0]) ? $json.content[0].text : '';
const cand = '{' + raw;
let o; try { o = JSON.parse(cand.slice(cand.indexOf('{'), cand.lastIndexOf('}') + 1)); } catch(e) { o = {}; }
const stories = ($('Drop used').first().json.stories) || [];
const idx = parseInt(o.index, 10);
let m = (Number.isInteger(idx) && idx >= 0 && idx < stories.length) ? stories[idx] : null;
if (!m && o.url) m = stories.find(s => String(s.link || s.url || '') === String(o.url));
if (!m && stories.length) m = stories[0];
const url = m ? String(m.link || m.url || m.guid || o.url || '') : String(o.url || '');
const title = m ? String(m.title || '') : '';
const summary = m ? String(m.contentSnippet || m.content || m.description || '').replace(/<[^>]+>/g, ' ').slice(0, 400) : '';
return [{ json: { url: url, title: title, summary: summary } }];"""
write_body = ("={{ JSON.stringify({ model: 'claude-haiku-4-5', max_tokens: 1600, system: " + SYS_Q + ", "
    "messages: [ { role: 'user', content: 'Write ONE LinkedIn post in my voice about this story. The source_url to return is exactly: ' + $('Extract pick').first().json.url + "
    "'\\n\\nHEADLINE: ' + $('Extract pick').first().json.title + '\\nSUMMARY: ' + $('Extract pick').first().json.summary + "
    "'\\n\\nFULL ARTICLE (may be empty if it could not be fetched):\\n' + String($json.article || '').slice(0,7000) + "
    "'\\n\\nWhen the FULL ARTICLE has real content, write from it and USE its specific numbers and breakdown (who spends or gets what, the split, percentages, amounts), do not stay generic. If the full article is empty or unreadable, write a solid post from the headline and summary instead. NEVER return an empty post. Then return the JSON object described in the system prompt.' }, { role: 'assistant', content: '{' } ] }) }}")

extract_js = """const raw = ($json && $json.content && $json.content[0] && $json.content[0].text) ? $json.content[0].text : '';
const cand = '{' + raw;
const a = cand.indexOf('{');
const b = cand.lastIndexOf('}');
let obj;
try { obj = JSON.parse(cand.slice(a, b + 1)); }
catch (e) { try { obj = JSON.parse(raw); } catch (e2) { obj = { post: raw, hook: '', highlight_phrase: '', image_subject: '' }; } }
// The listener is one long execution, so $('Clean article') can still hold an article from an
// earlier event. Read the image only from the node this event ran. A revision (feedback) keeps
// the source link, article image and card style of the draft it revises: the revise prompt
// does not return them, so they used to come back empty (no source link, no Article shot).
let ev = '';
try { ev = String($('Process updates').first().json.event || ''); } catch (e) {}
let articleImage = '', prev = null;
if (ev === 'news' || ev === 'again' || ev === '') { try { articleImage = String($('Clean news article').first().json.articleImage || ''); } catch (e) {} }
if (!articleImage && (ev === 'link' || ev === '')) { try { articleImage = String($('Clean article').first().json.articleImage || ''); } catch (e) {} }
if (ev === 'feedback') { try { prev = $('Get latest draft').first().json || null; } catch (e) {} }
if (prev && !articleImage) articleImage = String(prev.articleImage || '');
// hashtags come from a dedicated field; append them to the post so they are ALWAYS present
let tagsRaw = obj.hashtags;
if (Array.isArray(tagsRaw)) tagsRaw = tagsRaw.join(' ');
let tags = String(tagsRaw || '').replace(/\\s+/g, ' ').trim();
if (tags) { tags = tags.split(' ').filter(Boolean).map(function (t) { t = t.replace(/^#+/, ''); return t ? '#' + t : ''; }).filter(Boolean).join(' '); }
let post = String(obj.post || '').trim();
// an older X post is not news: drop a stray 'ago today' the model likes to write anyway
if (ev === 'xpost') { let xa = ''; try { xa = String($('Get X post').first().json.ago || ''); } catch (e) {} if (xa && xa !== 'today') post = post.replace(/\\b(ago|back)\\s+today\\b/gi, '$1'); }
if (tags && post.indexOf('#') === -1) { post = post + '\\n\\n' + tags; }   // only if model did not already inline them
// A quote survives only if it is really in the article, word for word (ignoring case and
// punctuation), and the speaker is named there too: the card shows it in that person's name, so
// a paraphrase or an invented line must never get through. Topic drafts have no article, so they
// never get one; a revision keeps the quote of the draft it revises.
function norm(t){ return String(t || '').toLowerCase().replace(/&#?\\w+;/g, ' ').replace(/[^a-z0-9]+/g, ' ').trim(); }
let src = '';
if (ev === 'news' || ev === 'again') { try { src = String($('Clean news article').first().json.article || ''); } catch (e) {} }
if (ev === 'link') {
  try { src = String($('Clean article').first().json.article || ''); } catch (e) {}
  try { src += ' ' + String($('Find alt news').first().json.article || ''); } catch (e) {}
}
const quoteRaw = (String(obj.quote || '') + ' / ' + String(obj.quote_speaker || '')).slice(0, 300);   // what the writer returned, kept in the draft for diagnosis
let quote = String(obj.quote || '').trim().replace(/^["\\u201c\\u201d]+|["\\u201c\\u201d]+$/g, '').trim();
let speaker = String(obj.quote_speaker || '').trim(), role = String(obj.quote_role || '').trim();
const qn = norm(quote), sn = norm(src), last = norm(speaker).split(' ').pop();
// the speaker must be named next to the quote (any of its occurrences: a pull quote in the
// summary line repeats it far from the attribution), not just somewhere in the article
let attributed = false;
for (let qi = qn ? sn.indexOf(qn) : -1; qi !== -1 && !attributed; qi = sn.indexOf(qn, qi + 1)) {
  attributed = sn.slice(Math.max(0, qi - 250), qi + qn.length + 250).indexOf(last) !== -1;
}
if (!(qn.split(' ').length >= 5 && quote.length <= 260 && last && attributed)) { quote = ''; speaker = ''; role = ''; }
if (ev === 'feedback' && prev) { quote = String(prev.quote || ''); speaker = String(prev.quoteSpeaker || ''); role = String(prev.quoteRole || ''); }
return [{ json: {
  quote: (ev === 'xpost') ? '' : quote, quoteSpeaker: (ev === 'xpost') ? '' : speaker, quoteRole: (ev === 'xpost') ? '' : role, quoteRaw: quoteRaw,
  xpost: (function(){ if (ev === 'xpost') { try { return $('Get X post').first().json; } catch (e) {} } return (prev && prev.xpost) || null; })(),
  postText: post,
  hook: String(obj.hook || '').trim(),
  highlightPhrase: String(obj.highlight_phrase || '').trim(),
  imageSubject: String(obj.image_subject || '').trim(),
  sourceUrl: String(obj.source_url || (prev && prev.sourceUrl) || '').trim(),
  note: String(obj.note || '').trim(),
  articleImage: articleImage.trim(),
  prevStyle: (prev && prev.style !== undefined && prev.style !== null) ? String(prev.style) : '',
  prevPhoto: String((prev && prev.photoUrl) || '').trim(),
  prevSpeakerPhoto: String((prev && prev.speakerPhoto) || '').trim()
} }];"""

def _inject(js):
    return (js.replace("__NAME__", DISPLAY_NAME).replace("__HANDLE__", LINKEDIN_HANDLE)
              .replace("__INITIALS__", INITIALS).replace("__AVATAR__", AVATAR_URL)
              .replace("__POSTHANDLE__", ("@" + X_HANDLE) if X_HANDLE else ("in/" + LINKEDIN_HANDLE))
              .replace("__VERIFIED__", "true" if VERIFIED_BADGE else "false"))

# shared card renderer: renderCard(d, style) -> full HTML. d = { hook, articleImage, photoUrl }.
# style 0 = photo background + red-frame white headline box (default). style 1 = clean social-post card.
RENDER_CARD_JS = """function esc(s){return String(s==null?'':s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}
var LI_SVG='<svg viewBox="0 0 448 512"><path d="M100.28 448H7.4V148.9h92.88zM53.79 108.1C24.09 108.1 0 83.5 0 53.8a53.79 53.79 0 0 1 107.58 0c0 29.7-24.1 54.3-53.79 54.3zM447.9 448h-92.68V302.4c0-34.7-.7-79.2-48.29-79.2-48.29 0-55.69 37.7-55.69 76.7V448h-92.78V148.9h89.08v40.8h1.3c12.4-23.5 42.69-48.3 87.88-48.3 94 0 111.28 61.9 111.28 142.3V448z"/></svg>';
function renderCard(d, style){
  const hook = esc(d.hook || '');
  const hlen = String(d.hook || '').length;
  const bgUrl = String(d.photoUrl || d.articleImage || '').trim();
  const NAME='__NAME__', HANDLE='in/__HANDLE__', INITIALS='__INITIALS__', AV='__AVATAR__', POSTHANDLE='__POSTHANDLE__';
  if (style === 1) {
    // A real post screenshot (X, dark mode). The old white card read as fake: bold display font,
    // a headline instead of a sentence, no timestamp or actions. Only TRUE details are added:
    // the time the card is made, the article domain on the image, and the action icons with no
    // counts (how a brand-new post looks). No invented likes/reposts and no verified check.
    const BG='#000000', TXT='#e7e9ea', SUB='#71767b', LINE='#2f3336';
    // a verified quote (checked word for word in Extract draft) is posted as a quote with its speaker
    // a REAL X post (from a pasted x.com link, read by /xpost) is drawn as itself: its author, avatar,
    // verified colour, text, original time and real like count. Nothing about it is made up.
    const xp = (d.xpost && d.xpost.ok) ? d.xpost : null;
    const hasQ = !xp && !!String(d.quote || '').trim();
    let text = xp ? String(xp.text || '') : hasQ ? ('\\u201c' + String(d.quote).trim() + '\\u201d') : String(d.hook || '').trim();
    if (!xp && !hasQ && text && !/[.!?:]$/.test(text) && text.slice(-1) !== '\\u2026') text += '.';
    const attrib = hasQ ? ('\\u2014 ' + String(d.quoteSpeaker || '') + (d.quoteRole ? ', ' + d.quoteRole : '')) : '';
    const face = hasQ && !!d.speakerPhoto;
    const mUrl = xp ? String(xp.photo || '') : face ? String(d.speakerPhoto) : bgUrl;
    const host = xp ? '' : String(d.sourceUrl || '').replace(/^https?:\\/\\//,'').split('/')[0].replace(/^www\\./,'');
    const n = text.length + attrib.length;
    const fs = mUrl ? (n > 150 ? 42 : n > 110 ? 46 : n > 70 ? 50 : 54) : (n > 150 ? 54 : n > 100 ? 60 : 66);
    const now = xp && xp.created ? new Date(xp.created) : new Date();
    const when = now.toLocaleTimeString('en-US', { timeZone:'__TIMEZONE__', hour:'numeric', minute:'2-digit' })
      + ' \\u00b7 ' + now.toLocaleDateString('en-US', { timeZone:'__TIMEZONE__', month:'short', day:'numeric', year:'numeric' });
    // Lucide icons (ISC licence): message-circle, repeat-2, heart, bookmark, share
    const IC = ['<path d="M7.9 20A9 9 0 1 0 4 16.1L2 22Z"/>',
      '<path d="m2 9 3-3 3 3"/><path d="M13 18H7a2 2 0 0 1-2-2V6"/><path d="m22 15-3 3-3-3"/><path d="M11 6h6a2 2 0 0 1 2 2v10"/>',
      '<path d="M19 14c1.49-1.46 3-3.21 3-5.5A5.5 5.5 0 0 0 16.5 3c-1.76 0-3 .5-4.5 2-1.5-1.5-2.74-2-4.5-2A5.5 5.5 0 0 0 2 8.5c0 2.3 1.5 4.05 3 5.5l7 7Z"/>',
      '<path d="m19 21-7-4-7 4V5a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2v16z"/>',
      '<path d="M4 12v8a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-8"/><path d="M16 6l-4-4-4 4"/><path d="M12 2v13"/>'];
    const acts = IC.map(function(p){ return '<svg viewBox="0 0 24 24">' + p + '</svg>'; }).join('');
    // verified badge next to the name (owner asked for it 2026-11-01; VERIFIED_BADGE=0 in config.env turns it off)
    const VERIFIED = __VERIFIED__;
    const BADGE = '<svg class="vb" viewBox="0 0 24 24"><path fill="#1d9bf0" d="M3.85 8.62a4 4 0 0 1 4.78-4.77 4 4 0 0 1 6.74 0 4 4 0 0 1 4.78 4.78 4 4 0 0 1 0 6.74 4 4 0 0 1-4.77 4.78 4 4 0 0 1-6.75 0 4 4 0 0 1-4.78-4.77 4 4 0 0 1 0-6.76Z"/>'
      + '<path d="m9 12 2 2 4-4" fill="none" stroke="#fff" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/></svg>';
    const BCOL = { blue:'#1d9bf0', gold:'#e2b719', grey:'#829aab' };
    const badgeCol = xp ? (BCOL[xp.badge] || '') : (VERIFIED ? BCOL.blue : '');
    const badgeHtml = (badgeCol ? BADGE.replace('#1d9bf0', badgeCol) : '')
      + (xp && xp.affiliate ? `<img class="aff" src="${xp.affiliate}" alt="${esc(xp.affiliateName || '')}">` : '');   // X affiliation logo, as on the real post
    const who = xp ? esc(xp.name) : NAME, whoHandle = xp ? '@' + esc(xp.handle) : POSTHANDLE;
    const avSrc = xp ? xp.avatar : AV;
    const avHtml = avSrc ? `<img class="av" src="${avSrc}">` : `<div class="av mono">${INITIALS}</div>`;
    // X style number: 1,234 / 12.3K / 1.8M (truncated, like X)
    function xnum(v){ v = Number(v) || 0; if (v < 10000) return v.toLocaleString('en-US'); if (v < 1e6) return (Math.floor(v / 100) / 10).toString().replace(/\\.0$/, '') + 'K'; return (Math.floor(v / 1e5) / 10).toString().replace(/\\.0$/, '') + 'M'; }
    const stats = xp && xp.likes ? `<div class="rule"></div><div class="stats"><b>${xnum(xp.likes)}</b> Likes</div>` : '';
    // hashtags and @mentions in X blue, as X shows them
    const textHtml = esc(text).replace(/(^|\\s)([#@][A-Za-z0-9_]+)/g, '$1<span class="tag">$2</span>')
      .replace(/(^|\\s)((?:https?:\\/\\/)?(?:[a-z0-9-]+\\.)+[a-z]{2,}(?:\\/[^\\s<]*)?)/gi, '$1<span class="tag">$2</span>');   // links in X blue
    // a portrait is shown at its own shape, left-aligned, the way X shows a single tall photo;
    // cropped into the wide frame it was a band across the eyes
    const dom = host ? `<span class="dom">${esc(host)}</span>` : '';
    const media = !mUrl ? '' : face
      ? `<div class="mwrap"><div class="mbox"><img src="${mUrl}">${dom}</div></div>`
      : `<div class="media" style="background-image:url('${mUrl}')">${dom}</div>`;
    return `<!doctype html><html><head><meta charset="utf-8"><style>
*{margin:0;padding:0;box-sizing:border-box}html,body{background:${BG}}
.card{width:1080px;height:1080px;background:${BG};color:${TXT};font-family:'Inter','Noto Color Emoji',sans-serif;padding:46px 52px 30px;display:flex;flex-direction:column;${mUrl ? '' : 'justify-content:center;'}}
.head{display:flex;align-items:center;gap:24px}
.av{width:104px;height:104px;border-radius:50%;object-fit:cover;flex:0 0 auto}
.mono{background:#1d9bf0;color:#fff;font-weight:700;font-size:42px;display:flex;align-items:center;justify-content:center}
.who{display:flex;flex-direction:column;gap:4px;flex:1}
.nm{font-weight:700;font-size:38px;letter-spacing:-0.01em;display:flex;align-items:center;gap:8px}
.vb{width:40px;height:40px;flex:0 0 auto}
.aff{width:34px;height:34px;border-radius:7px;border:1px solid ${LINE};flex:0 0 auto}
.hn{color:${SUB};font-size:34px}
.dots{color:${SUB};font-size:40px;letter-spacing:2px;align-self:flex-start;margin-top:6px}
.txt{margin-top:30px;font-size:${fs}px;line-height:1.32;letter-spacing:-0.005em;white-space:pre-line}
.media{position:relative;margin-top:30px;flex:1 1 auto;min-height:200px;border-radius:34px;border:2px solid ${LINE};background-size:cover;background-position:center}
.mwrap{margin-top:30px;flex:1 1 auto;min-height:200px;display:flex}
.mbox{position:relative;height:100%}
.mbox img{height:100%;width:auto;max-width:976px;object-fit:cover;border-radius:34px;border:2px solid ${LINE};display:block}
.att{margin-top:16px}
.tag{color:#1d9bf0}
.stats{color:${SUB};font-size:32px}
.stats b{color:${TXT};font-weight:700}
.dom{position:absolute;left:22px;bottom:20px;background:rgba(0,0,0,.72);color:#fff;font-size:28px;padding:6px 16px;border-radius:12px}
.meta{margin-top:30px;color:${SUB};font-size:32px}
.rule{height:2px;background:${LINE};margin:28px 0 22px}
.acts{display:flex;justify-content:space-between;padding:0 18px}
.acts svg{width:46px;height:46px;fill:none;stroke:${SUB};stroke-width:1.75;stroke-linecap:round;stroke-linejoin:round}
</style></head><body><div class="card">
<div class="head">${avHtml}<div class="who"><div class="nm">${who}${badgeHtml}</div><div class="hn">${whoHandle}</div></div><div class="dots">\\u00b7\\u00b7\\u00b7</div></div>
<div class="txt">${textHtml}${attrib ? `<div class="att">${esc(attrib)}</div>` : ''}</div>
${media}
<div class="meta">${when}</div>${stats}
<div class="rule"></div>
<div class="acts">${acts}</div>
</div></body></html>`;
  }
  const qs0 = hlen>90?52 : hlen>72?58 : hlen>52?64 : hlen>36?70 : 76;
  const photoCss = bgUrl ? `background-image:url('${bgUrl}');background-size:cover;background-position:center;` : `background:radial-gradient(120% 100% at 72% 8%,#18263f 0%,#0b0f17 62%);`;
  return `<!doctype html><html><head><meta charset="utf-8"><style>
*{margin:0;padding:0;box-sizing:border-box}html,body{background:#0b0f17}
.card{position:relative;width:1080px;height:1080px;overflow:hidden;font-family:'Sora',-apple-system,system-ui,sans-serif;background:#0b0f17}
.photo{position:absolute;inset:0;${photoCss}filter:saturate(0.98) contrast(1.02)}
.overlay{position:absolute;inset:0;background:linear-gradient(180deg,rgba(8,11,18,0.22) 0%,rgba(8,11,18,0) 24%,rgba(8,11,18,0) 60%,rgba(8,11,18,0.55) 100%)}
.headline{position:absolute;left:52px;right:52px;bottom:150px;background:#fff;border:7px solid #e8402c;border-radius:6px;padding:38px 42px 42px;box-shadow:0 26px 64px rgba(0,0,0,0.5)}
.htext{color:#0a0a0a;font-weight:800;font-size:${qs0}px;line-height:1.09;letter-spacing:-0.015em}
.badge{position:absolute;right:56px;bottom:52px;display:flex;align-items:center;gap:14px}
.li{width:46px;height:46px;border-radius:11px;background:#0a66c2;display:flex;align-items:center;justify-content:center;flex:0 0 auto;box-shadow:0 6px 20px rgba(0,0,0,0.5)}
.li svg{width:25px;height:25px;fill:#fff}
.handle{color:#fff;font-weight:700;font-size:30px;letter-spacing:.01em;text-shadow:0 2px 16px rgba(0,0,0,0.75)}
</style></head><body><div class="card">
<div class="photo"></div><div class="overlay"></div>
<div class="headline"><div class="htext">${hook}</div></div>
<div class="badge"><span class="li">${LI_SVG}</span><span class="handle">${HANDLE}</span></div>
</div></body></html>`;
}"""
RENDER_CARD_JS = RENDER_CARD_JS.replace("__TIMEZONE__", TIMEZONE)

build_js = _inject(RENDER_CARD_JS + """
const d = $('Extract draft').first().json;
const photoUrl = String(d.prevPhoto || (($('Resolve image').first() || {}).json || {}).url || '').trim();   // revision: keep the draft's photo; else /findimage: a VERIFIED loadable image (article img if it loads, else a relevant Wikipedia/Commons photo)
// quote card: the quoted person's photo, only when /personphoto is sure it is them (else the article photo)
const speakerPhoto = d.quote ? String(d.prevSpeakerPhoto || (($('Resolve speaker').first() || {}).json || {}).url || '').trim() : '';
// Rotate the card template per draft. Every fresh draft used style 0, so the only way to
// see the other one was to press a button, and all the news looked identical. Record used
// runs just before this and returns a count that ticks once per draft and survives restarts.
// Only the photo styles (0 red frame, 1 social post) rotate: the auto-picked big-number and
// typographic cards had no photo, and half of all drafts came out looking empty. Those two
// stay one tap away. A revision keeps the look of the draft it revises.
const _n = Number((($('Record used').first() || {}).json || {}).count);
const AUTO = """ + str(AUTO_STYLES) + """;
let style = AUTO[(Number.isFinite(_n) ? _n : Math.floor(Date.now()/60000)) % AUTO.length];
if (d.quote || (d.xpost && d.xpost.ok)) style = 1;   // a verified quote / a real X post opens as the post card
if (d.prevStyle !== '' && Number.isFinite(Number(d.prevStyle))) style = Number(d.prevStyle) % """ + str(NSTYLES) + """;
const Q = { quote:d.quote||'', quoteSpeaker:d.quoteSpeaker||'', quoteRole:d.quoteRole||'', speakerPhoto:speakerPhoto, xpost:d.xpost||null };
const html = renderCard(Object.assign({ hook:d.hook, articleImage:'', photoUrl:photoUrl, sourceUrl:d.sourceUrl||'' }, Q), style);
const draftId = 'd' + Date.now();
const draftJson = JSON.stringify(Object.assign({ postText:d.postText, hook:d.hook, highlightPhrase:d.highlightPhrase, imageSubject:d.imageSubject, sourceUrl:d.sourceUrl||'', note:d.note||'', articleImage:d.articleImage||'', photoUrl:photoUrl, style:style, quoteRaw:d.quoteRaw||'' }, Q));
return [{ json: { html:html, draftJson:draftJson, draftId:draftId, quoteSpeaker:Q.quoteSpeaker, xpostBy:(d.xpost && d.xpost.ok) ? '@' + d.xpost.handle : '', photoUrl:photoUrl, postText:d.postText, hook:d.hook, highlightPhrase:d.highlightPhrase, imageSubject:d.imageSubject, sourceUrl:d.sourceUrl||'' } }];""")

# restyle builder: rebuild the SAME draft (from /latest) with the NEXT style template
restyle_build_js = _inject(RENDER_CARD_JS + """
const d = $json;
const forced = String(($('Process updates').first().json || {}).arg || '');
const style = forced !== '' ? (Number(forced) % """ + str(NSTYLES) + """) : (((Number(d.style)||0) + 1) % """ + str(NSTYLES) + """);
const photoUrl = String(d.photoUrl || '').trim();
const Q = { quote:d.quote||'', quoteSpeaker:d.quoteSpeaker||'', quoteRole:d.quoteRole||'', speakerPhoto:d.speakerPhoto||'', xpost:d.xpost||null };
const html = renderCard(Object.assign({ hook:d.hook, articleImage:'', photoUrl:photoUrl, sourceUrl:d.sourceUrl||'' }, Q), style);
// Same draft, new look: re-render into the tapped draft's id so its buttons keep working and
// Publish posts what the preview shows (a new id per restyle left the old buttons stale).
const draftId = String(($('Process updates').first().json || {}).draftId || '') || ('d' + Date.now());
const styleName = """ + json.dumps(STYLE_NAMES) + """[style] || 'New style';
const draftJson = JSON.stringify(Object.assign({ postText:d.postText, hook:d.hook, highlightPhrase:d.highlightPhrase, imageSubject:d.imageSubject, sourceUrl:d.sourceUrl||'', note:d.note||'', articleImage:d.articleImage||'', photoUrl:photoUrl, style:style }, Q));
return [{ json: { html:html, draftJson:draftJson, draftId:draftId, styleName:styleName, photoUrl:photoUrl, postText:d.postText, hook:d.hook, highlightPhrase:d.highlightPhrase, imageSubject:d.imageSubject, sourceUrl:d.sourceUrl||'' } }];""")

buttons_expr = ("={{ JSON.stringify({ chat_id: '" + CHAT_ID + "', "
    "text: $('Extract draft').first().json.postText + ($('Extract draft').first().json.sourceUrl ? ('\\n\\n📎 Source: ' + $('Extract draft').first().json.sourceUrl) : ''), "
    "reply_markup: { inline_keyboard: [ "
    "[ { text: '👍 Like it', callback_data: 'like:' + $('Build card HTML').first().json.draftId }, "
    "{ text: '👎 Skip', callback_data: 'skip:' + $('Build card HTML').first().json.draftId } ], "
    "[ { text: '🔄 Different news', callback_data: 'again:' + $('Build card HTML').first().json.draftId } ], "
    "[ { text: '🖼 Red frame', callback_data: 'style:' + $('Build card HTML').first().json.draftId + ':0' }, "
    "{ text: '📱 Social post', callback_data: 'style:' + $('Build card HTML').first().json.draftId + ':1' } ], "
    "[ { text: '🎬 Clip from video', callback_data: 'clip:' + $('Build card HTML').first().json.draftId }, "
    "{ text: '📸 Article shot', callback_data: 'shot:' + $('Build card HTML').first().json.draftId } ], "
    "[ { text: '📷 Photo', callback_data: 'pic:' + $('Build card HTML').first().json.draftId }, "
    "{ text: '🖼 Gallery', callback_data: 'gallery:' + $('Build card HTML').first().json.draftId } ] "
    "] } }) }}")

# restyle preview buttons — same keyboard, reading the restyled draft's id/text
restyle_buttons_expr = ("={{ JSON.stringify({ chat_id: '" + CHAT_ID + "', "
    "text: $('Build restyle card').first().json.postText + ($('Build restyle card').first().json.sourceUrl ? ('\\n\\n📎 Source: ' + $('Build restyle card').first().json.sourceUrl) : ''), "
    "reply_markup: { inline_keyboard: [ "
    "[ { text: '👍 Like it', callback_data: 'like:' + $('Build restyle card').first().json.draftId }, "
    "{ text: '👎 Skip', callback_data: 'skip:' + $('Build restyle card').first().json.draftId } ], "
    "[ { text: '🔄 Different news', callback_data: 'again:' + $('Build restyle card').first().json.draftId } ], "
    "[ { text: '🖼 Red frame', callback_data: 'style:' + $('Build restyle card').first().json.draftId + ':0' }, "
    "{ text: '📱 Social post', callback_data: 'style:' + $('Build restyle card').first().json.draftId + ':1' } ], "
    "[ { text: '🎬 Clip from video', callback_data: 'clip:' + $('Build restyle card').first().json.draftId }, "
    "{ text: '📸 Article shot', callback_data: 'shot:' + $('Build restyle card').first().json.draftId } ], "
    "[ { text: '📷 Photo', callback_data: 'pic:' + $('Build restyle card').first().json.draftId }, "
    "{ text: '🖼 Gallery', callback_data: 'gallery:' + $('Build restyle card').first().json.draftId } ] "
    "] } }) }}")

# ---- custom TITLE-CARD mode: Jon sends a PHOTO + CAPTION -> render HIS photo with HIS caption
# as the headline, in the same card templates the bot uses, and send the finished image back.
# Standalone: draft ids start with 'card' so render_service /latest SKIPS them — the normal
# draft flows (plain-text revise, 👍/publish) keep targeting the real latest draft.
# The 🎨 button cycles styles via the cardstyle:<id> callback (state read back from /draft?id=).
custom_card_js = _inject(RENDER_CARD_JS + """
const hook = String($('Process updates').first().json.caption || '').trim().slice(0,180);
const sp = $('Set photo (card)').first().json;
const photoUrl = String(sp.url || '');
const style = 0;
const html = renderCard({ hook:hook, articleImage:'', photoUrl:photoUrl }, style);
const draftId = String(sp.id || ('card' + Date.now()));
const draftJson = JSON.stringify({ postText:'', hook:hook, photoUrl:photoUrl, style:style, custom:true });
return [{ json: { html:html, draftJson:draftJson, draftId:draftId } }];""")

# photo swap rebuild: Jon's photo becomes the BACKGROUND of the draft's card — the headline
# box / badge / current style are re-rendered on top (NOT a raw image replacement; Jon was
# losing the title). Same draft id, so the existing 👍/✅ flow and /latest are untouched;
# photoUrl persists as the local /photo URL so later 🎨 restyles keep his photo.
swap_build_js = _inject(RENDER_CARD_JS + """
const d = $('Get latest (photo)').first().json;
const photoUrl = String($('Set photo').first().json.url || '');
const style = Number(d.style) || 0;
const Q = { quote:d.quote||'', quoteSpeaker:d.quoteSpeaker||'', quoteRole:d.quoteRole||'', speakerPhoto:'', xpost:null };   // his photo wins over the speaker photo / real post
const html = renderCard(Object.assign({ hook:d.hook||'', articleImage:'', photoUrl:photoUrl, sourceUrl:d.sourceUrl||'' }, Q), style);
const draftId = String(d.id);
const draftJson = JSON.stringify(Object.assign({ postText:d.postText||'', hook:d.hook||'', highlightPhrase:d.highlightPhrase||'', imageSubject:d.imageSubject||'', sourceUrl:d.sourceUrl||'', note:d.note||'', articleImage:'', photoUrl:photoUrl, style:style }, Q));
return [{ json: { html:html, draftJson:draftJson, draftId:draftId } }];""")

cardstyle_build_js = _inject(RENDER_CARD_JS + """
const d = $json;
const forced = String(($('Process updates').first().json || {}).arg || '');
const style = forced !== '' ? (Number(forced) % """ + str(NSTYLES) + """) : (((Number(d.style)||0) + 1) % """ + str(NSTYLES) + """);
const hook = String(d.hook || '');
const photoUrl = String(d.photoUrl || '').trim();
const html = renderCard({ hook:hook, articleImage:'', photoUrl:photoUrl }, style);
const draftId = 'card' + Date.now();
const draftJson = JSON.stringify({ postText:'', hook:hook, photoUrl:photoUrl, style:style, custom:true });
return [{ json: { html:html, draftJson:draftJson, draftId:draftId } }];""")

card_button_expr = ("={{ JSON.stringify({ chat_id: '" + CHAT_ID + "', "
    "text: '🖼 Your card is ready. Long-press the image to save or share it.', "
    "reply_markup: { inline_keyboard: [ [ { text: '🎨 Other style', callback_data: 'cardstyle:' + $('Build custom card').first().json.draftId } ] ] } }) }}")
cardstyle_button_expr = ("={{ JSON.stringify({ chat_id: '" + CHAT_ID + "', "
    "text: '🖼 Your card is ready. Long-press the image to save or share it.', "
    "reply_markup: { inline_keyboard: [ [ { text: '🎨 Other style', callback_data: 'cardstyle:' + $('Build card restyle').first().json.draftId } ] ] } }) }}")

def make_tail(x0, y):
    return [
      node("Extract draft","n8n-nodes-base.code",2,[x0,y],{"mode":"runOnceForAllItems","jsCode":extract_js}),
      node("Post ok?","n8n-nodes-base.if",2.2,[x0+150,y+30],IF_NOTEMPTY("postText")),
      tg_post("Draft failed","sendMessage",[x0+150,y+320],
        "={{ JSON.stringify({ chat_id: '"+CHAT_ID+"', text: '⚠️ I could not build a post this time (the source could not be read). Try /new, a topic, or a different link.' }) }}"),
      node("Record used","n8n-nodes-base.httpRequest",4.2,[x0+330,y+150],
        {"method":"POST","url":RENDER_SVC+"/used","sendBody":True,"specifyBody":"json",
         "jsonBody":"={{ JSON.stringify({ url: $('Extract draft').first().json.sourceUrl, title: $('Extract draft').first().json.hook }) }}","options":{}},
        extra={"onError":"continueRegularOutput"}),
      node("Has note?","n8n-nodes-base.if",2.2,[x0+180,y-140],IF_NOTEMPTY("note")),
      tg_post("Send note","sendMessage",[x0+180,y-300],
        "={{ JSON.stringify({ chat_id: '"+CHAT_ID+"', text: '⚠️ Heads up: ' + $('Extract draft').first().json.note }) }}"),
      node("Resolve image","n8n-nodes-base.httpRequest",4.2,[x0+220,y],
        {"method":"GET","url":"="+RENDER_SVC+"/findimage?article={{ encodeURIComponent($('Extract draft').first().json.articleImage || '') }}&subject={{ encodeURIComponent($('Extract draft').first().json.imageSubject || '') }}","options":{"timeout":40000}},
        extra={"onError":"continueRegularOutput"}),
      node("Resolve speaker","n8n-nodes-base.httpRequest",4.2,[x0+330,y+110],
        {"method":"GET","url":"="+RENDER_SVC+"/personphoto?name={{ encodeURIComponent($('Extract draft').first().json.quoteSpeaker || '') }}","options":{"timeout":20000}},
        extra={"onError":"continueRegularOutput"}),
      node("Build card HTML","n8n-nodes-base.code",2,[x0+440,y],{"mode":"runOnceForAllItems","jsCode":build_js}),
      node("Render card","n8n-nodes-base.httpRequest",4.2,[x0+660,y],
        {"method":"POST","url":RENDER_SVC+"/render","sendBody":True,"specifyBody":"json",
         "jsonBody":"={{ JSON.stringify({ draft_id: $json.draftId, html: $json.html, draft_json: $json.draftJson }) }}","options":FILE_RESP}),
      node("Send preview photo","n8n-nodes-base.telegram",1.2,[x0+880,y],
        {"operation":"sendPhoto","chatId":CHAT_ID,"binaryData":True,"binaryPropertyName":"data",
         "additionalFields":{"caption":"={{ '🖼️ New LinkedIn draft — preview' + ($('Build card HTML').first().json.quoteSpeaker ? ' · 💬 quoting ' + $('Build card HTML').first().json.quoteSpeaker + ' (checked word for word against the article)' : '') + ($('Build card HTML').first().json.xpostBy ? ' · 🐦 the real post by ' + $('Build card HTML').first().json.xpostBy + ' (its real text, time and likes)' : '') }}"}},
        extra={"credentials":{"telegramApi":{"id":TELEGRAM_CRED,"name":"Telegram (Agent bot)"}}}),
      tg_post("Send approval buttons","sendMessage",[x0+1100,y],buttons_expr),
    ]
TAIL_CONNS = {
 "Extract draft":{"main":[[{"node":"Post ok?","type":"main","index":0}]]},
 "Post ok?":{"main":[[{"node":"Record used","type":"main","index":0}],[{"node":"Draft failed","type":"main","index":0}]]},
 "Record used":{"main":[[{"node":"Has note?","type":"main","index":0}]]},
 "Has note?":{"main":[[{"node":"Send note","type":"main","index":0}],[{"node":"Resolve image","type":"main","index":0}]]},
 "Send note":{"main":[[{"node":"Resolve image","type":"main","index":0}]]},
 "Resolve image":{"main":[[{"node":"Resolve speaker","type":"main","index":0}]]},
 "Resolve speaker":{"main":[[{"node":"Build card HTML","type":"main","index":0}]]},
 "Build card HTML":{"main":[[{"node":"Render card","type":"main","index":0}]]},
 "Render card":{"main":[[{"node":"Send preview photo","type":"main","index":0}]]},
 "Send preview photo":{"main":[[{"node":"Send approval buttons","type":"main","index":0}]]},
}

clean_article_js = """const html = $json.data || $json.body || '';
let t = String(html)
  .replace(/<script[\\s\\S]*?<\\/script>/gi, ' ')
  .replace(/<style[\\s\\S]*?<\\/style>/gi, ' ')
  .replace(/<[^>]+>/g, ' ')
  .replace(/&nbsp;/g, ' ').replace(/&amp;/g, '&').replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&#\\d+;/g, ' ')
  .replace(/\\s+/g, ' ').trim();
const ogt = String(html).match(/<meta[^>]+property=[\\"']og:title[\\"'][^>]*content=[\\"']([^\\"']+)[\\"']/i)
        || String(html).match(/<meta[^>]+content=[\\"']([^\\"']+)[\\"'][^>]*property=[\\"']og:title[\\"']/i);
const tm = String(html).match(/<title[^>]*>([^<]+)<\\/title>/i);
const title = (ogt ? ogt[1] : (tm ? tm[1] : '')).trim();          // og:title is richer than <title>
const ogd = String(html).match(/<meta[^>]+property=[\\"']og:description[\\"'][^>]*content=[\\"']([^\\"']+)[\\"']/i)
        || String(html).match(/<meta[^>]+content=[\\"']([^\\"']+)[\\"'][^>]*property=[\\"']og:description[\\"']/i);
const dm = String(html).match(/<meta[^>]+name=[\\"']description[\\"'][^>]*content=[\\"']([^\\"']+)[\\"']/i);
const desc = (ogd ? ogd[1] : (dm ? dm[1] : '')).trim();
let im = String(html).match(/<meta[^>]+property=[\\"']og:image(?::url)?[\\"'][^>]*content=[\\"']([^\\"']+)[\\"']/i)
      || String(html).match(/<meta[^>]+content=[\\"']([^\\"']+)[\\"'][^>]*property=[\\"']og:image(?::url)?[\\"']/i)
      || String(html).match(/<meta[^>]+name=[\\"']twitter:image[\\"'][^>]*content=[\\"']([^\\"']+)[\\"']/i);
let articleImage = im ? im[1].trim() : '';
if (articleImage.indexOf('//') === 0) articleImage = 'https:' + articleImage;
if (articleImage && articleImage.slice(0, 4) !== 'http') articleImage = '';
const low = String(html).toLowerCase();
const article = (title ? 'TITLE: ' + title + '\\n' : '') + (desc ? 'SUMMARY: ' + desc + '\\n\\n' : '') + t;
const chalMarks = ['just a moment','enable javascript and cookies','cf-challenge','attention required','verify you are human','are you a robot','checking your browser'];
const challenged = chalMarks.some(function(c){ return low.indexOf(c) !== -1; }) && t.length < 800;
const hasSummary = (title.length + desc.length) >= 80;   // enough to write from headline+summary (incl. og:)
const ok = hasSummary || (t.length >= 250 && !challenged);
return [{ json: { article: article.slice(0, 7000), articleImage: articleImage, title: title, summary: desc, ok: ok } }];"""

# DIRECT publisher feeds (their <link> is the real article URL, unlike Google News which redirects to a homepage)
# Verified from the render host's own network. TechCrunch + DCD article pages are SOMETIMES
# Cloudflare-gated, which is now safe: a thin/blocked fetch degrades to the RSS summary in the news
# pipeline, and a blocked pasted link triggers the /findnews find-it-elsewhere fallback.
# The last two feeds are STOCK/MARKET sources — Jon likes them but they must not flood the list
# (see MARKET_BALANCE_JS; balance is enforced in filter_rank_js + build_headlines_js).
NEWS_FEEDS = [
    ("News: Ars Technica",   "https://feeds.arstechnica.com/arstechnica/index"),
    ("News: The Verge",      "https://www.theverge.com/rss/index.xml"),
    ("News: The Register",   "https://www.theregister.com/headlines.atom"),
    ("News: VentureBeat",    "https://venturebeat.com/feed/"),
    ("News: MIT Tech Review","https://www.technologyreview.com/feed/"),
    ("News: TechCrunch",     "https://techcrunch.com/feed/"),                           # startup / AI / big-tech news
    ("News: SiliconANGLE",   "https://siliconangle.com/feed/"),                         # enterprise AI + cloud news
    ("News: The Next Web",   "https://thenextweb.com/feed"),                            # tech / AI news
    ("News: DCD",            "https://www.datacenterdynamics.com/rss/"),                # data center industry news
    ("News: CNBC Tech",      "https://www.cnbc.com/id/19854910/device/rss/rss.html"),   # MARKET: tech + market-moving news
    ("News: Motley Fool",    "https://www.fool.com/feeds/index.aspx"),                  # MARKET: tech stocks / earnings
]

# reusable JS: drop LOW-VALUE items (product reviews / gadget commerce, and formulaic
# finance filler like SEC insider-filing posts and raw earnings-call transcripts). Keep real news.
REVIEW_FILTER_JS = """function isReview(s){
  var link = String((s && (s.link || s.url || s.guid)) || '').toLowerCase();
  var title = String((s && s.title) || '').toLowerCase();
  var urlMarks = ['/review','buying-guide','/deals','/best-','hands-on','how-to','/roundup','gift-guide','/vs-','/deal/','/coverage/filings/','/call-transcripts/'];
  for (var i=0;i<urlMarks.length;i++){ if(link.indexOf(urlMarks[i])!==-1) return true; }
  var titleMarks = ['hands-on','hands on','unboxing','buying guide','gift guide','first look','we tested','we tried',' review:','earnings call transcript','director sells','director buys','insider sells','insider buys'];
  for (var j=0;j<titleMarks.length;j++){ if(title.indexOf(titleMarks[j])!==-1) return true; }
  if (/\\sreview$/.test(title)) return true;
  if (/\\breview\\b/.test(title) && /(laptop|phone|headphone|earbud|tablet|\\btv\\b|monitor|camera|smartwatch|watch|speaker|router|keyboard|mouse|drone|\\bssd\\b|\\bgpu\\b|console|charger|soundbar|webcam|dock|handheld|\\bev\\b|\\bcar\\b)/.test(title)) return true;
  return false;
}"""

# reusable JS: classify stock/market items by host so tech news vs market news can be balanced.
# Jon LIKES the stock coverage, there was just too much of it drowning out the actual tech news.
MARKET_BALANCE_JS = """var MARKET_HOSTS = ['fool.com','cnbc.com'];
function hostOf(s){ return String((s && (s.link || s.url || s.guid)) || '').replace(/^https?:\\/\\//,'').split('/')[0].replace(/^www\\./,''); }
function isMarket(s){ return MARKET_HOSTS.indexOf(hostOf(s)) !== -1; }
"""

# pipeline pre-step: drop low-value items, sort newest-first, then cap per source so a
# high-frequency feed (e.g. Motley Fool) cannot flood out the others, and cap the stock/market
# share of the pool so the picker mostly sees actual tech news; keep top 24 for the picker.
filter_rank_js = REVIEW_FILTER_JS + MARKET_BALANCE_JS + """
const items = $input.all().map(function(x){ return x.json; });
// stock sites (fool/cnbc) only count when the item is about tech: the picker chose a Motley Fool
// biotech piece (Gilead) and two other stock stories in a row on 2026-11-01
const TECH_T = /\\b(ai|artificial intelligence|nvidia|microsoft|azure|openai|anthropic|google|alphabet|amazon|aws|apple|meta|tesla|spacex|starlink|chips?|semiconductors?|gpus?|cloud|data ?cent(?:er|re)s?|software|cyber\\w*|robot\\w*|quantum|intel|amd|tsmc|broadcom|oracle|palantir|coreweave|arm|qualcomm|salesforce|tech\\w*)\\b/i;
const kept = items.filter(function(s){ return !isReview(s) && (!isMarket(s) || TECH_T.test(String(s.title||'') + ' ' + String(s.contentSnippet||''))); });
kept.sort(function(a,b){ return new Date(b.isoDate||b.pubDate||0) - new Date(a.isoDate||a.pubDate||0); });
var perHost = {}; var out = []; var market = 0;
for (var k=0; k<kept.length; k++){
  var s = kept[k];
  if (isMarket(s) && market >= 6) continue;
  var host = hostOf(s);
  perHost[host] = (perHost[host]||0) + 1;
  if (perHost[host] <= 6) { out.push(s); if (isMarket(s)) market++; }
  if (out.length >= 24) break;
}
return out.map(function(s){ return { json: s }; });"""

# Recently used stories are removed from the candidates in code: the picker was told to avoid
# them but chose the same Barclays article three times on 2026-11-01. Links are compared without
# their query string (?source=... tracking). If everything was used, keep the full list.
drop_used_js = """const all = ($('Bundle stories').first().json.stories) || [];
const recent = ($json.recent) || [];
function key(u){ return String(u || '').split('?')[0].split('#')[0].replace(/\\/+$/, '').toLowerCase(); }
const used = {}; recent.forEach(function(r){ if (r.url) used[key(r.url)] = 1; });
const fresh = all.filter(function(s){ return !used[key(s.link || s.url || s.guid)]; });
return [{ json: { stories: fresh.length ? fresh : all, recent: recent, dropped: all.length - fresh.length } }];"""

# two-pass NEWS pipeline: sources -> pick a fresh story (avoid recently used) -> fetch its full article -> write detailed post
def news_pipeline(x0, y):
    ns = []
    for i,(nm,url) in enumerate(NEWS_FEEDS):
        ns.append(node(nm,"n8n-nodes-base.rssFeedRead",1.1,[x0,y-200+i*110],{"url":url,"options":{}},extra={"onError":"continueRegularOutput"}))
    ns += [
      node("Merge sources","n8n-nodes-base.merge",3,[x0+200,y],{"mode":"append","numberInputs":len(NEWS_FEEDS)}),
      node("Filter reviews + rank","n8n-nodes-base.code",2,[x0+380,y],{"mode":"runOnceForAllItems","jsCode":filter_rank_js}),
      node("Bundle stories","n8n-nodes-base.aggregate",1,[x0+560,y],
        {"aggregate":"aggregateAllItemData","destinationFieldName":"stories","options":{}}),
      node("Get used","n8n-nodes-base.httpRequest",4.2,[x0+740,y],
        {"method":"GET","url":RENDER_SVC+"/used","options":{}},extra={"onError":"continueRegularOutput"}),
      node("Drop used","n8n-nodes-base.code",2,[x0+830,y+120],{"mode":"runOnceForAllItems","jsCode":drop_used_js}),
      claude_node("Pick story",[x0+920,y],pick_body),
      node("Extract pick","n8n-nodes-base.code",2,[x0+1100,y],{"mode":"runOnceForAllItems","jsCode":extract_pick_js}),
      node("Fetch article","n8n-nodes-base.httpRequest",4.2,[x0+1280,y],
        {"method":"GET","url":"={{ $('Extract pick').first().json.url }}","sendHeaders":True,
         "headerParameters":{"parameters":[{"name":"User-Agent","value":REAL_UA}]},"options":TEXT_RESP},
        extra={"onError":"continueRegularOutput"}),
      node("Clean news article","n8n-nodes-base.code",2,[x0+1460,y],{"mode":"runOnceForAllItems","jsCode":clean_article_js}),
      claude_node("Write post",[x0+1640,y],write_body),
    ]
    conns = {}
    for i,(nm,url) in enumerate(NEWS_FEEDS):
        conns[nm] = {"main":[[{"node":"Merge sources","type":"main","index":i}]]}
    conns.update({
      "Merge sources":{"main":[[{"node":"Filter reviews + rank","type":"main","index":0}]]},
      "Filter reviews + rank":{"main":[[{"node":"Bundle stories","type":"main","index":0}]]},
      "Bundle stories":{"main":[[{"node":"Get used","type":"main","index":0}]]},
      "Get used":{"main":[[{"node":"Drop used","type":"main","index":0}]]},
      "Drop used":{"main":[[{"node":"Pick story","type":"main","index":0}]]},
      "Pick story":{"main":[[{"node":"Extract pick","type":"main","index":0}]]},
      "Extract pick":{"main":[[{"node":"Fetch article","type":"main","index":0}]]},
      "Fetch article":{"main":[[{"node":"Clean news article","type":"main","index":0}]]},
      "Clean news article":{"main":[[{"node":"Write post","type":"main","index":0}]]},
      "Write post":{"main":[[{"node":"Extract draft","type":"main","index":0}]]},
    })
    entry = [{"node":nm,"type":"main","index":0} for nm,_ in NEWS_FEEDS]
    return ns, conns, entry

# ================================================================ WF-A : SCHEDULED HEADLINES DIGEST
# WF-A is now built AFTER build_headlines_js (which it shares with WF-B's /headlines branch) —
# search for "WF-A construction" below. The old scheduled auto-draft pipeline was removed
# 2026-09-05: Jon rarely liked the scheduled drafts, so the schedule now sends the headlines
# list instead and he pastes back the link he actually wants a post about.

# ================================================================ WF-B : CONTINUOUS LISTENER
loadoffset_js = """const s = $getWorkflowStaticData('global');
const now = Date.now();
if (!s.listenerStart) s.listenerStart = now;
// 15 min cap -> release the lock and end; the next 15 s tick starts a fresh listener (it used
// to just end, so taps went unanswered for up to ~2 min until the 65 s lock expired)
if ((now - s.listenerStart) > 900000) { s.listenerStart = 0; return [{ json: { release: true } }]; }
return [{ json: { offset: (s.lastUpdateId || 0) + 1, release: false } }];"""
process_js = """const s = $getWorkflowStaticData('global');
const last = s.lastUpdateId || 0;
const updates = ($json.result || []).filter(u => u.update_id > last).sort((a,b)=>a.update_id-b.update_id);
function classify() {
for (const u of updates) {
  if (u.callback_query && u.callback_query.data) {
    const cq = u.callback_query; const parts = String(cq.data).split(':');
    s.lastUpdateId = u.update_id;
    // the 📷 Photo button was 'photo:' until 2026-11-01, which the photo-swap branch caught
    // (it expects an uploaded file) and crashed the listener. A tap is never an upload.
    if (parts[0] === 'photo') parts[0] = 'pic';
    return [{ json: { event: parts[0], draftId: parts[1] || '', arg: parts[2] || '', orig: parts[3] || '', callbackQueryId: cq.id,
      chatId: (cq.message && cq.message.chat && cq.message.chat.id) || '',
      messageId: (cq.message && cq.message.message_id) || '' } }];
  } else if (u.message && ((u.message.photo && u.message.photo.length) || (u.message.document && /^image\\/(jpe?g|png)/i.test(String(u.message.document.mime_type || ''))))) {
    const fileId = (u.message.photo && u.message.photo.length) ? u.message.photo[u.message.photo.length - 1].file_id : u.message.document.file_id;
    s.lastUpdateId = u.update_id;
    const cap = String(u.message.caption || '').trim();
    if (cap) return [{ json: { event:'photocard', fileId: fileId, caption: cap, chatId: (u.message.chat && u.message.chat.id) || '' } }];
    return [{ json: { event:'photo', fileId: fileId, chatId: (u.message.chat && u.message.chat.id) || '' } }];
  } else if (u.message && (u.message.video || (u.message.document && /video/i.test((u.message.document.mime_type) || '')))) {
    const vf = u.message.video || u.message.document;
    s.lastUpdateId = u.update_id;
    return [{ json: { event:'video', fileId: vf.file_id, chatId: (u.message.chat && u.message.chat.id) || '' } }];
  } else if (u.message && u.message.text) {
    const t = String(u.message.text).trim();
    const chatId = (u.message.chat && u.message.chat.id) || '';
    s.lastUpdateId = u.update_id;
    if (/^\\/(help|start)/i.test(t)) return [{ json: { event:'help', chatId } }];
    if (/^\\/(headlines|top10|top|news)\\b/i.test(t)) return [{ json: { event:'headlines', chatId } }];
    const xm = t.match(/https?:\\/\\/(?:www\\.|mobile\\.)?(?:x|twitter)\\.com\\/[^\\/\\s]+\\/status(?:es)?\\/\\d+/i);
    if (xm) return [{ json: { event:'xpost', payload: xm[0], chatId } }];   // a real X post -> card shows that post
    let m = t.match(/https?:\\/\\/\\S+/i) || t.match(/\\bwww\\.\\S+/i);
    if (m) { let lu = m[0]; if (!/^https?:/i.test(lu)) lu = 'https://' + lu;
      // LinkedIn pages are never fetched, not even one pasted post: ask for the article or the text instead
      if (/^https?:\\/\\/([^\\/?#]*\\.)?(linkedin\\.com|lnkd\\.in)([\\/?#:]|$)/i.test(lu)) return [{ json: { event:'lilink', chatId } }];
      return [{ json: { event:'link', payload: lu, chatId } }]; }
    if (/^\\/new\\b/i.test(t)) { const rest = t.replace(/^\\/new\\b/i,'').trim(); return [{ json: rest ? { event:'topic', payload: rest, chatId } : { event:'news', chatId } }]; }
    if (t.charAt(0) === '/') return [{ json: { event:'none' } }];
    return [{ json: { event:'feedback', feedback: t, chatId } }];
  } else { s.lastUpdateId = u.update_id; }
}
return [{ json: { event: 'none' } }];
}
// every slow request gets a status message in the chat (owner could not tell if a tap was working)
const STATUS = { style:'\u23f3 Changing the look\u2026', restyle:'\u23f3 Changing the look\u2026', back:'\u23f3 Going back to the card\u2026',
  pic:'\u23f3 Finding a photo\u2026', gallery:'\u23f3 Building a photo gallery\u2026',
  shot:'\u23f3 Capturing the article page (about 10 seconds)\u2026', clip:'\u23f3 Looking for a video and cutting a clip (up to 30 seconds)\u2026',
  again:'\u23f3 Finding a different story (about 15 seconds)\u2026', news:'\u23f3 Picking a story from the news (about 15 seconds)\u2026',
  link:'\u23f3 Reading the article and writing the post\u2026', topic:'\u23f3 Writing a post on that topic\u2026',
  feedback:'\u270d\ufe0f Reworking it with your notes\u2026', xpost:'\ud83d\udc26 Reading that post on X\u2026' };
const r = classify();
r[0].json.status = STATUS[r[0].json.event] || '';
return r;"""

clean_article_js = """const html = $json.data || $json.body || '';
let t = String(html)
  .replace(/<script[\\s\\S]*?<\\/script>/gi, ' ')
  .replace(/<style[\\s\\S]*?<\\/style>/gi, ' ')
  .replace(/<[^>]+>/g, ' ')
  .replace(/&nbsp;/g, ' ').replace(/&amp;/g, '&').replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&#\\d+;/g, ' ')
  .replace(/\\s+/g, ' ').trim();
const ogt = String(html).match(/<meta[^>]+property=[\\"']og:title[\\"'][^>]*content=[\\"']([^\\"']+)[\\"']/i)
        || String(html).match(/<meta[^>]+content=[\\"']([^\\"']+)[\\"'][^>]*property=[\\"']og:title[\\"']/i);
const tm = String(html).match(/<title[^>]*>([^<]+)<\\/title>/i);
const title = (ogt ? ogt[1] : (tm ? tm[1] : '')).trim();          // og:title is richer than <title>
const ogd = String(html).match(/<meta[^>]+property=[\\"']og:description[\\"'][^>]*content=[\\"']([^\\"']+)[\\"']/i)
        || String(html).match(/<meta[^>]+content=[\\"']([^\\"']+)[\\"'][^>]*property=[\\"']og:description[\\"']/i);
const dm = String(html).match(/<meta[^>]+name=[\\"']description[\\"'][^>]*content=[\\"']([^\\"']+)[\\"']/i);
const desc = (ogd ? ogd[1] : (dm ? dm[1] : '')).trim();
let im = String(html).match(/<meta[^>]+property=[\\"']og:image(?::url)?[\\"'][^>]*content=[\\"']([^\\"']+)[\\"']/i)
      || String(html).match(/<meta[^>]+content=[\\"']([^\\"']+)[\\"'][^>]*property=[\\"']og:image(?::url)?[\\"']/i)
      || String(html).match(/<meta[^>]+name=[\\"']twitter:image[\\"'][^>]*content=[\\"']([^\\"']+)[\\"']/i);
let articleImage = im ? im[1].trim() : '';
if (articleImage.indexOf('//') === 0) articleImage = 'https:' + articleImage;
if (articleImage && articleImage.slice(0, 4) !== 'http') articleImage = '';
const low = String(html).toLowerCase();
const article = (title ? 'TITLE: ' + title + '\\n' : '') + (desc ? 'SUMMARY: ' + desc + '\\n\\n' : '') + t;
const chalMarks = ['just a moment','enable javascript and cookies','cf-challenge','attention required','verify you are human','are you a robot','checking your browser'];
const challenged = chalMarks.some(function(c){ return low.indexOf(c) !== -1; }) && t.length < 800;
const hasSummary = (title.length + desc.length) >= 80;   // enough to write from headline+summary (incl. og:)
const ok = hasSummary || (t.length >= 250 && !challenged);
return [{ json: { article: article.slice(0, 7000), articleImage: articleImage, title: title, summary: desc, ok: ok } }];"""

# /headlines browser: format one page (10) of recent headlines + a "Show 10 more" button.
# offset comes from the callback (more:<N>); the raw command starts at 0 (and so does WF-A's
# scheduled digest, where $('Process updates') does not exist -> the try/catch keeps offset 0).
# Stateless (re-fetches feeds each page). BALANCED: per-host cap 8 so no feed floods, then each
# page of 10 carries at most 3 stock/market items (fool/cnbc), the rest actual tech news.
build_headlines_js = (REVIEW_FILTER_JS + MARKET_BALANCE_JS + """
const stories = (($('Bundle headlines').first().json.stories) || []).filter(function(s){ return !isReview(s); });
stories.sort((a,b)=> new Date(b.isoDate||b.pubDate||0) - new Date(a.isoDate||a.pubDate||0));
var perHost = {}; var pool = [];
for (var k=0; k<stories.length; k++){
  var h = hostOf(stories[k]);
  perHost[h] = (perHost[h]||0) + 1;
  if (perHost[h] <= 8) pool.push(stories[k]);
}
var techQ = pool.filter(function(s){ return !isMarket(s); });
var marketQ = pool.filter(isMarket);
var capped = [];
while (capped.length < 40 && (techQ.length || marketQ.length)){
  var page = techQ.splice(0,7).concat(marketQ.splice(0,3));
  while (page.length < 10 && (techQ.length || marketQ.length)) page.push(techQ.length ? techQ.shift() : marketQ.shift());
  page.sort(function(a,b){ return new Date(b.isoDate||b.pubDate||0) - new Date(a.isoDate||a.pubDate||0); });
  capped = capped.concat(page);
}
capped = capped.slice(0, 40);
let offset = 0;
try { offset = parseInt($('Process updates').first().json.draftId, 10) || 0; } catch(e) {}
if (!(offset >= 0)) offset = 0;
const PAGE = 10;
const total = capped.length;
const slice = capped.slice(offset, offset + PAGE);
const dec = s => String(s||'').replace(/&#(\\d+);/g,(m,n)=>String.fromCharCode(+n)).replace(/&#x([0-9a-f]+);/gi,(m,n)=>String.fromCharCode(parseInt(n,16))).replace(/&amp;/g,'&').replace(/&quot;/g,'"').replace(/&apos;/g,"'").replace(/&lt;/g,'<').replace(/&gt;/g,'>');
let body;
if (!slice.length) {
  body = offset > 0 ? 'That is the end of the list. Send /headlines to start over.' : 'I could not fetch headlines right now, try again in a minute.';
} else {
  const lines = slice.map((s,i)=> (offset+i+1) + '. ' + dec(s.title).trim() + (s.link ? '\\n' + s.link : '')).join('\\n\\n');
  const shown = Math.min(offset + PAGE, total);
  body = '\\ud83d\\udcf0 Recent tech & AI headlines (' + (offset+1) + '-' + shown + ' of ' + total + ')\\n\\nTap a link to read, or paste one back to me and I will draft a post about it.\\n\\n' + lines;
}
const nextOffset = offset + PAGE;
const hasMore = nextOffset < total;
const payload = { chat_id: '__CHAT__', text: body, disable_web_page_preview: true };
if (hasMore) payload.reply_markup = { inline_keyboard: [[ { text: '\\u27a1\\ufe0f Show 10 more', callback_data: 'more:' + nextOffset } ]] };
return [{ json: { tg: JSON.stringify(payload) } }];""").replace("__CHAT__", CHAT_ID)

# ---------------- WF-A construction: 3x-daily schedule -> the same balanced headlines page as
# /headlines (page 0; $('Process updates') does not exist here so the try/catch keeps offset 0).
# The ➡️ Show-10-more button on the sent message is a callback, handled by WF-B's listener.
A = [node("3x daily · 7/12/18 CT","n8n-nodes-base.scheduleTrigger",1.2,[-360,300],
    {"rule":{"interval":[
        {"field":"days","daysInterval":1,"triggerAtHour":7},
        {"field":"days","daysInterval":1,"triggerAtHour":12},
        {"field":"days","daysInterval":1,"triggerAtHour":18}]}})]
hlA_entry = []
for i,(nm,url) in enumerate(NEWS_FEEDS):
    fn = "HL " + nm.split(": ")[-1]
    hlA_entry.append({"node":fn,"type":"main","index":0})
    A.append(node(fn,"n8n-nodes-base.rssFeedRead",1.1,[-120,-160+i*90],{"url":url,"options":{}},
        extra={"onError":"continueRegularOutput"}))
A += [
  node("Merge headlines","n8n-nodes-base.merge",3,[80,300],{"mode":"append","numberInputs":len(NEWS_FEEDS)}),
  node("Bundle headlines","n8n-nodes-base.aggregate",1,[280,300],
      {"aggregate":"aggregateAllItemData","destinationFieldName":"stories","options":{}}),
  node("Build headlines page","n8n-nodes-base.code",2,[480,300],{"mode":"runOnceForAllItems","jsCode":build_headlines_js}),
  tg_post("Send headlines","sendMessage",[680,300],"={{ $json.tg }}"),
]
A.append(sticky("Note A","## Scheduled headlines digest\n3x daily (7/12/18 CT): fetch all news feeds → one balanced page of 10 recent headlines (max 3 stock/market items per page, per-host cap) + ➡️ Show-10-more button (handled by WF-B's listener). Jon pastes a link back to get a draft. The old scheduled auto-draft pipeline was removed 2026-09-05.",[-360,-160],360,260,4))
connsA = {"3x daily · 7/12/18 CT":{"main":[hlA_entry]}}
for i,(nm,url) in enumerate(NEWS_FEEDS):
    connsA["HL " + nm.split(": ")[-1]] = {"main":[[{"node":"Merge headlines","type":"main","index":i}]]}
connsA.update({
 "Merge headlines":{"main":[[{"node":"Bundle headlines","type":"main","index":0}]]},
 "Bundle headlines":{"main":[[{"node":"Build headlines page","type":"main","index":0}]]},
 "Build headlines page":{"main":[[{"node":"Send headlines","type":"main","index":0}]]},
})
wfA = {"name":"LinkedIn Daily Agent","nodes":A,"connections":connsA,"settings":{"executionOrder":"v1"}}
json.dump(wfA, open(OUT_A,"w"))

LOOP = "Claim / heartbeat"
B = []
B.append(node("Every minute","n8n-nodes-base.scheduleTrigger",1.2,[-820,300],{"rule":{"interval":[{"field":"seconds","secondsInterval":15}]}}))   # name kept for continuity; ticks every 15 s
B.append(node("Claim / heartbeat","n8n-nodes-base.httpRequest",4.2,[-620,300],
    {"method":"POST","url":RENDER_SVC+"/claim","sendBody":True,"specifyBody":"json",
     "jsonBody":"={{ JSON.stringify({ token: $execution.id, ttl: 65 }) }}","options":{}},
    extra={"onError":"continueRegularOutput"}))
B.append(node("Claimed?","n8n-nodes-base.if",2.2,[-420,300],IF_BOOL("ok")))
B.append(node("Load offset","n8n-nodes-base.code",2,[-220,300],{"mode":"runOnceForAllItems","jsCode":loadoffset_js}))
B.append(node("Cap reached?","n8n-nodes-base.if",2.2,[-120,420],IF_BOOL("release")))
B.append(node("Release claim","n8n-nodes-base.httpRequest",4.2,[0,520],
    {"method":"POST","url":RENDER_SVC+"/claim","sendBody":True,"specifyBody":"json",
     "jsonBody":"={{ JSON.stringify({ token: $execution.id, release: true }) }}","options":{}},
    extra={"onError":"continueRegularOutput"}))
B.append(node("Get updates","n8n-nodes-base.httpRequest",4.2,[-20,300],
    {"method":"GET","url":"="+TG_API+"/getUpdates?timeout=50&offset={{ $json.offset }}","options":{"timeout":60000}},
    extra={"onError":"continueRegularOutput"}))
B.append(node("Process updates","n8n-nodes-base.code",2,[180,300],{"mode":"runOnceForAllItems","jsCode":process_js}))
# routing chain
B.append(node("Help?","n8n-nodes-base.if",2.2,[380,300],IF_EQ("event","help")))
B.append(node("Headlines?","n8n-nodes-base.if",2.2,[440,-360],IF_EQ("event","headlines")))
B.append(node("More?","n8n-nodes-base.if",2.2,[600,-360],IF_EQ("event","more")))
B.append(node("Photocard?","n8n-nodes-base.if",2.2,[560,240],IF_EQ("event","photocard")))
B.append(node("Photo?","n8n-nodes-base.if",2.2,[560,160],IF_EQ("event","photo")))
B.append(node("Video?","n8n-nodes-base.if",2.2,[560,80],IF_EQ("event","video")))
B.append(node("Restyle?","n8n-nodes-base.if",2.2,[560,-80],IF_EQ("event","restyle")))
B.append(node("Style?","n8n-nodes-base.if",2.2,[560,-20],IF_OR("event","style","back")))
B.append(node("Cardstyle?","n8n-nodes-base.if",2.2,[560,-160],IF_EQ("event","cardstyle")))
B.append(node("X post?","n8n-nodes-base.if",2.2,[460,400],IF_EQ("event","xpost")))
B.append(node("Get X post","n8n-nodes-base.httpRequest",4.2,[660,520],
    {"method":"GET","url":"="+RENDER_SVC+"/xpost?url={{ encodeURIComponent($('Process updates').first().json.payload) }}","options":{"timeout":20000}},
    extra={"onError":"continueRegularOutput"}))
B.append(node("X post ok?","n8n-nodes-base.if",2.2,[760,520],IF_BOOL("ok")))
B.append(claude_node("Draft from X post",[860,460],xpost_body))
B.append(tg_post("X post failed","sendMessage",[860,600],"={{ JSON.stringify({ chat_id: $('Process updates').first().json.chatId, text: '🐦 ' + ($('Get X post').first().json.reason || 'I could not read that post') + '. Paste the article link instead and I will write from that.' }) }}"))
B.append(node("LinkedIn link?","n8n-nodes-base.if",2.2,[500,360],IF_EQ("event","lilink")))
B.append(tg_post("LinkedIn link msg","sendMessage",[500,480],
    "={{ JSON.stringify({ chat_id: $('Process updates').first().json.chatId, text: 'I do not open LinkedIn pages (this agent only uses the official LinkedIn API to publish your own posts). Paste the link to the original article instead, or send /new followed by the post text and I will write about that.' }) }}"))
B.append(node("Link?","n8n-nodes-base.if",2.2,[560,340],IF_EQ("event","link")))
B.append(node("Topic?","n8n-nodes-base.if",2.2,[740,380],IF_EQ("event","topic")))
B.append(node("News or again?","n8n-nodes-base.if",2.2,[920,420],IF_OR("event","news","again")))
B.append(node("Feedback?","n8n-nodes-base.if",2.2,[1100,460],IF_EQ("event","feedback")))
B.append(node("Like?","n8n-nodes-base.if",2.2,[1280,520],IF_EQ("event","like")))
B.append(node("Publish?","n8n-nodes-base.if",2.2,[1460,560],IF_EQ("event","pub")))
B.append(node("Draft?","n8n-nodes-base.if",2.2,[1560,640],IF_EQ("event","draft")))
B.append(node("Discard?","n8n-nodes-base.if",2.2,[1660,700],IF_OR("event","del","skip")))
# help branch
B.append(tg_post("Send guide","sendMessage",[380,120],
    "={{ JSON.stringify({ chat_id: $('Process updates').first().json.chatId, text: '📌 The full guide is pinned at the top of this chat, scroll up or tap the pinned message.\\n\\nQuick version: paste a link or type /new <topic> for a new post, send a photo to swap the image, tap 👍 then ✅ to publish, or just type notes to revise the current draft.' }) }}"))
# photo swap branch (send a photo -> replace the current draft image with it)
B.append(node("Get latest (photo)","n8n-nodes-base.httpRequest",4.2,[560,-40],
    {"method":"GET","url":RENDER_SVC+"/latest","options":{}},extra={"onError":"continueRegularOutput"}))
B.append(node("Get file","n8n-nodes-base.httpRequest",4.2,[560,-220],
    {"method":"GET","url":"="+TG_API+"/getFile?file_id={{ $('Process updates').first().json.fileId }}","options":{}},extra={"onError":"continueRegularOutput"}))
# store the photo on the render svc (local URL: Chromium renders from localhost + survives the
# ~1h Telegram file-URL expiry so later restyles keep the photo)
B.append(node("Set photo","n8n-nodes-base.httpRequest",4.2,[760,-220],
    {"method":"POST","url":RENDER_SVC+"/setphoto","sendBody":True,"specifyBody":"json",
     "jsonBody":"={{ JSON.stringify({ draft_id: $('Get latest (photo)').first().json.id, file_url: '"+TG_FILE+"' + ((($('Get file').first().json || {}).result || {}).file_path || '') }) }}","options":{}},
    extra={"onError":"continueRegularOutput"}))
# a failed store used to throw, which ended the whole listener run (every later tap waited
# for the next one); now it says so and loops
B.append(node("Photo stored?","n8n-nodes-base.if",2.2,[860,-220],IF_BOOL("ok")))
B.append(tg_post("Photo store failed","sendMessage",[960,-340],
    "={{ JSON.stringify({ chat_id: $('Process updates').first().json.chatId, text: '🖼 I could not download that photo from Telegram. Try sending it again (as a photo, under 20 MB).' }) }}"))
# guard: /latest can 404 (no draft in 48h) — say so instead of silently doing nothing
B.append(node("Have draft?","n8n-nodes-base.if",2.2,[660,-130],IF_NOTEMPTY("id")))
B.append(tg_post("No draft msg","sendMessage",[860,-40],
    "={{ JSON.stringify({ chat_id: $('Process updates').first().json.chatId, text: '🖼 I have no recent draft to attach that photo to. Get one with /new or by pasting a link, then send the photo again. Tip: send the photo WITH a caption and I will turn it into a title card instead.' }) }}"))
# rebuild the card with Jon's photo as the BACKGROUND (headline box + badge + style kept),
# overwriting the same draft id. /render returns the PNG, which must then be re-UPLOADED as
# binary — GOTCHA (verified live 2026-09-07): Telegram sendPhoto REFUSES its own
# api.telegram.org file URLs ("Bad Request: failed to get HTTP URL content").
B.append(node("Build swap card","n8n-nodes-base.code",2,[960,-220],{"mode":"runOnceForAllItems","jsCode":swap_build_js}))
B.append(node("Render swap card","n8n-nodes-base.httpRequest",4.2,[1060,-160],
    {"method":"POST","url":RENDER_SVC+"/render","sendBody":True,"specifyBody":"json",
     "jsonBody":"={{ JSON.stringify({ draft_id: $json.draftId, html: $json.html, draft_json: $json.draftJson }) }}","options":FILE_RESP}))
B.append(node("Send photo preview","n8n-nodes-base.telegram",1.2,[1160,-220],
    {"operation":"sendPhoto","chatId":CHAT_ID,"binaryData":True,"binaryPropertyName":"data",
     "additionalFields":{"caption":"🖼 Card rebuilt with your photo as the background — preview"}},
    extra={"credentials":{"telegramApi":{"id":TELEGRAM_CRED,"name":"Telegram (Agent bot)"}}}))
photo_btn_expr = ("={{ JSON.stringify({ chat_id: $('Process updates').first().json.chatId, "
    "text: String($('Get latest (photo)').first().json.postText || '').slice(0,1000), "
    "reply_markup: { inline_keyboard: [[ "
    "{ text: '👍 Like it', callback_data: 'like:' + $('Get latest (photo)').first().json.id }, "
    "{ text: '👎 Skip', callback_data: 'skip:' + $('Get latest (photo)').first().json.id } "
    "]] } }) }}")
B.append(tg_post("Send swap buttons","sendMessage",[1360,-220],photo_btn_expr))
# video swap branch (send a video -> watermark it + attach to current draft)
B.append(node("Get latest (video)","n8n-nodes-base.httpRequest",4.2,[740,-40],
    {"method":"GET","url":RENDER_SVC+"/latest","options":{}},extra={"onError":"continueRegularOutput"}))
B.append(node("Get file (v)","n8n-nodes-base.httpRequest",4.2,[740,-260],
    {"method":"GET","url":"="+TG_API+"/getFile?file_id={{ $('Process updates').first().json.fileId }}","options":{}},extra={"onError":"continueRegularOutput"}))
B.append(node("Set video","n8n-nodes-base.httpRequest",4.2,[940,-260],
    {"method":"POST","url":RENDER_SVC+"/video","sendBody":True,"specifyBody":"json",
     "jsonBody":"={{ JSON.stringify({ draft_id: $('Get latest (video)').first().json.id, file_url: '"+TG_FILE+"' + $('Get file (v)').first().json.result.file_path, handle: 'in/"+LINKEDIN_HANDLE+"' }) }}",
     "options":{"timeout":240000}},extra={"onError":"continueRegularOutput"}))
B.append(node("Download watermarked","n8n-nodes-base.httpRequest",4.2,[1140,-260],
    {"method":"GET","url":"="+RENDER_SVC+"/vidfile?id={{ $('Get latest (video)').first().json.id }}","options":FILE_RESP},extra={"onError":"continueRegularOutput"}))
B.append(node("Send video preview","n8n-nodes-base.telegram",1.2,[1340,-260],
    {"operation":"sendVideo","chatId":CHAT_ID,"binaryData":True,"binaryPropertyName":"data",
     "additionalFields":{"caption":"=🎬 Video attached with your watermark. Post text:\n\n{{ String($('Get latest (video)').first().json.postText || '').slice(0,900) }}"}},
    extra={"credentials":{"telegramApi":{"id":TELEGRAM_CRED,"name":"Telegram (Agent bot)"}}}))
vbtn_expr = ("={{ JSON.stringify({ chat_id: $('Process updates').first().json.chatId, "
    "text: 'Publish this as a video post?', "
    "reply_markup: { inline_keyboard: [[ "
    "{ text: '👍 Like it', callback_data: 'like:' + $('Get latest (video)').first().json.id }, "
    "{ text: '👎 Skip', callback_data: 'skip:' + $('Get latest (video)').first().json.id } "
    "]] } }) }}")
B.append(tg_post("Send video buttons","sendMessage",[1540,-260],vbtn_expr))
# restyle branch (🎨 Other style -> rebuild the SAME draft with the next style template). Instant ack, no gate:
# re-render is fast (<65s) and idempotent-ish (cycles styles), so no /posted lock needed.
B.append(tg_post("Ack restyle","answerCallbackQuery",[760,-560],
    "={{ JSON.stringify({ callback_query_id: $('Process updates').first().json.callbackQueryId, text: '🎨 New style…' }) }}"))
# the TAPPED draft, not /latest: a style button on an older draft used to restyle the newest one
B.append(node("Get latest (restyle)","n8n-nodes-base.httpRequest",4.2,[960,-560],
    {"method":"GET","url":"="+RENDER_SVC+"/draft?id={{ $('Process updates').first().json.draftId }}","options":{}},
    extra={"onError":"continueRegularOutput"}))
B.append(node("Restyle draft?","n8n-nodes-base.if",2.2,[1060,-620],IF_NOTEMPTY("hook")))
B.append(tg_post("Restyle expired","sendMessage",[1160,-700],
    "={{ JSON.stringify({ chat_id: $('Process updates').first().json.chatId, text: '🎨 That draft is more than 2 days old and its files were cleaned up, so I cannot restyle it. Send /new or paste the link again.' }) }}"))
B.append(node("Build restyle card","n8n-nodes-base.code",2,[1160,-560],{"mode":"runOnceForAllItems","jsCode":restyle_build_js}))
B.append(node("Render restyle","n8n-nodes-base.httpRequest",4.2,[1360,-560],
    {"method":"POST","url":RENDER_SVC+"/render","sendBody":True,"specifyBody":"json",
     "jsonBody":"={{ JSON.stringify({ draft_id: $json.draftId, html: $json.html, draft_json: $json.draftJson }) }}","options":FILE_RESP}))
B.append(tg_edit_media("Edit restyle preview",[1460,-640],"$('Build restyle card').first().json.styleName + ' — preview'"))
B.append(node("Restyle edited?","n8n-nodes-base.if",2.2,[1560,-700],IF_BOOL("ok")))
# ↩️ Back to the card: the card is back on the original preview, so the suggestion (the clip
# or photo message right above the tapped question, and the question itself) goes away
# reads the tap from Process updates: $json here is Telegram's reply to the edit, not the event
B.append(node("Back tap?","n8n-nodes-base.if",2.2,[1660,-760],IF_EQ("event","back")))
B[-1]["parameters"]["conditions"]["conditions"][0]["leftValue"] = "={{ $('Process updates').first().json.event }}"
B.append(tg_post("Delete suggestion","deleteMessages",[1760,-820],
    "={{ JSON.stringify({ chat_id: $('Process updates').first().json.chatId, message_ids: [Number($('Process updates').first().json.messageId) - 1, Number($('Process updates').first().json.messageId)] }) }}"))
B.append(node("Card for resend","n8n-nodes-base.httpRequest",4.2,[1460,-480],
    {"method":"GET","url":"="+RENDER_SVC+"/card?id={{ $('Build restyle card').first().json.draftId }}","options":FILE_RESP},
    extra={"onError":"continueRegularOutput"}))
B.append(node("Send restyle preview","n8n-nodes-base.telegram",1.2,[1560,-560],
    {"operation":"sendPhoto","chatId":CHAT_ID,"binaryData":True,"binaryPropertyName":"data",
     "additionalFields":{"caption":"🎨 New style — preview"}},
    extra={"credentials":{"telegramApi":{"id":TELEGRAM_CRED,"name":"Telegram (Agent bot)"}}}))
B.append(tg_post("Send restyle buttons","sendMessage",[1760,-560],restyle_buttons_expr))
# custom title-card branch (photo + caption -> branded card sent back; 🎨 cycles the templates)
B.append(node("Get file (card)","n8n-nodes-base.httpRequest",4.2,[760,-820],
    {"method":"GET","url":"="+TG_API+"/getFile?file_id={{ $('Process updates').first().json.fileId }}","options":{}},extra={"onError":"continueRegularOutput"}))
B.append(node("Set photo (card)","n8n-nodes-base.httpRequest",4.2,[860,-760],
    {"method":"POST","url":RENDER_SVC+"/setphoto","sendBody":True,"specifyBody":"json",
     "jsonBody":"={{ JSON.stringify({ draft_id: 'card' + Date.now(), file_url: '"+TG_FILE+"' + $('Get file (card)').first().json.result.file_path }) }}","options":{}}))
B.append(node("Build custom card","n8n-nodes-base.code",2,[960,-820],{"mode":"runOnceForAllItems","jsCode":custom_card_js}))
B.append(node("Render custom card","n8n-nodes-base.httpRequest",4.2,[1160,-820],
    {"method":"POST","url":RENDER_SVC+"/render","sendBody":True,"specifyBody":"json",
     "jsonBody":"={{ JSON.stringify({ draft_id: $json.draftId, html: $json.html, draft_json: $json.draftJson }) }}","options":FILE_RESP}))
B.append(node("Send custom card","n8n-nodes-base.telegram",1.2,[1360,-820],
    {"operation":"sendPhoto","chatId":CHAT_ID,"binaryData":True,"binaryPropertyName":"data",
     "additionalFields":{"caption":"🖼 Your card"}},
    extra={"credentials":{"telegramApi":{"id":TELEGRAM_CRED,"name":"Telegram (Agent bot)"}}}))
B.append(tg_post("Send card button","sendMessage",[1560,-820],card_button_expr))
# 🎨 on a custom card: instant ack, rebuild from /draft?id= with the next style (no /posted gate —
# fast + idempotent-ish cycling, same reasoning as the draft restyle button)
B.append(tg_post("Ack cardstyle","answerCallbackQuery",[760,-920],
    "={{ JSON.stringify({ callback_query_id: $('Process updates').first().json.callbackQueryId, text: '🎨 New style…' }) }}"))
B.append(node("Get card draft","n8n-nodes-base.httpRequest",4.2,[960,-920],
    {"method":"GET","url":"="+RENDER_SVC+"/draft?id={{ $('Process updates').first().json.draftId }}","options":{}},extra={"onError":"continueRegularOutput"}))
B.append(node("Build card restyle","n8n-nodes-base.code",2,[1160,-920],{"mode":"runOnceForAllItems","jsCode":cardstyle_build_js}))
B.append(node("Render card restyle","n8n-nodes-base.httpRequest",4.2,[1360,-920],
    {"method":"POST","url":RENDER_SVC+"/render","sendBody":True,"specifyBody":"json",
     "jsonBody":"={{ JSON.stringify({ draft_id: $json.draftId, html: $json.html, draft_json: $json.draftJson }) }}","options":FILE_RESP}))
B.append(node("Send card restyle","n8n-nodes-base.telegram",1.2,[1560,-920],
    {"operation":"sendPhoto","chatId":CHAT_ID,"binaryData":True,"binaryPropertyName":"data",
     "additionalFields":{"caption":"🎨 Other style"}},
    extra={"credentials":{"telegramApi":{"id":TELEGRAM_CRED,"name":"Telegram (Agent bot)"}}}))
B.append(tg_post("Send cardstyle button","sendMessage",[1760,-920],cardstyle_button_expr))
# link branch
B.append(node("Fetch link","n8n-nodes-base.httpRequest",4.2,[560,140],
    {"method":"GET","url":"={{ $('Process updates').first().json.payload }}","sendHeaders":True,
     "headerParameters":{"parameters":[{"name":"User-Agent","value":REAL_UA}]},"options":TEXT_RESP},
    extra={"onError":"continueRegularOutput"}))
B.append(node("Clean article","n8n-nodes-base.code",2,[560,40],{"mode":"runOnceForAllItems","jsCode":clean_article_js}))
B.append(node("Article ok?","n8n-nodes-base.if",2.2,[560,-40],IF_BOOL("ok")))
B.append(tg_post("Link unreadable","sendMessage",[560,300],
    "={{ JSON.stringify({ chat_id: $('Process updates').first().json.chatId, text: '⚠️ That source blocks bots and I could not find the same story on a readable outlet either. Paste the article text after /new, or try a different link.' }) }}"))
B.append(claude_node("Draft from link",[760,-140],link_body))
# blocked-link fallback: tell Jon, search for the same story elsewhere, write from a readable version
B.append(tg_post("Link blocked msg","sendMessage",[560,140],
    "={{ JSON.stringify({ chat_id: $('Process updates').first().json.chatId, text: '⚠️ That source is blocking bots. Searching for the same story on another outlet…' }) }}"))
B.append(node("Get headline","n8n-nodes-base.code",2,[740,140],{"mode":"runOnceForAllItems","jsCode":get_headline_js}))
B.append(node("Find alt news","n8n-nodes-base.httpRequest",4.2,[920,140],
    {"method":"GET","url":"="+RENDER_SVC+"/findnews?title={{ encodeURIComponent($json.title) }}","options":{"timeout":120000}},
    extra={"onError":"continueRegularOutput"}))
B.append(node("Alt found?","n8n-nodes-base.if",2.2,[1100,140],IF_BOOL("ok")))
B.append(claude_node("Draft from alt",[1280,60],alt_link_body))
# topic branch
B.append(claude_node("Draft from topic",[740,180],topic_body))
# news / different-news branch (shared two-pass pipeline; entered by 'news' and 'again')
npB_nodes, npB_conns, npB_entry = news_pipeline(1120, -160)
B += npB_nodes
# 🔄 Different-news BUTTON protection = same fix as Like/Publish: instant ack + idempotency gate.
# The news pipeline is long (>65s claim TTL) so a 2nd listener can spawn and reprocess the tap;
# the /posted gate (keyed by callback id) makes a re-processed tap a no-op -> never two drafts.
B.append(node("Again tap?","n8n-nodes-base.if",2.2,[940,-260],IF_EQ("event","again")))
B.append(node("Fresh news once?","n8n-nodes-base.httpRequest",4.2,[940,-160],
    {"method":"POST","url":RENDER_SVC+"/posted","sendBody":True,"specifyBody":"json",
     "jsonBody":"={{ JSON.stringify({ draft_id: 'again:' + $('Process updates').first().json.callbackQueryId }) }}","options":{}},
    extra={"onError":"continueRegularOutput"}))
B.append(node("First again?","n8n-nodes-base.if",2.2,[940,-60],IF_BOOL("first")))
B.append(tg_post("Ack again","answerCallbackQuery",[940,40],
    "={{ JSON.stringify({ callback_query_id: $('Process updates').first().json.callbackQueryId, text: '🔄 Finding a different story…' }) }}"))
# /headlines browser branch (event 'headlines' = page 0; button 'more:<N>' = next page). Read-only list,
# so it only needs the instant ack (no /posted gate — showing a list twice is harmless, and it finishes < 65s).
B.append(tg_post("Ack more","answerCallbackQuery",[600,-200],
    "={{ JSON.stringify({ callback_query_id: $('Process updates').first().json.callbackQueryId, text: 'Loading…' }) }}"))
hl_entry = []
for i,(nm,url) in enumerate(NEWS_FEEDS):
    fn = "HL " + nm.split(": ")[-1]
    hl_entry.append({"node":fn,"type":"main","index":0})
    B.append(node(fn,"n8n-nodes-base.rssFeedRead",1.1,[820,-480+i*90],{"url":url,"options":{}},
        extra={"onError":"continueRegularOutput"}))
B.append(node("Merge headlines","n8n-nodes-base.merge",3,[1020,-360],{"mode":"append","numberInputs":len(NEWS_FEEDS)}))
B.append(node("Bundle headlines","n8n-nodes-base.aggregate",1,[1220,-360],
    {"aggregate":"aggregateAllItemData","destinationFieldName":"stories","options":{}}))
B.append(node("Build headlines page","n8n-nodes-base.code",2,[1420,-360],{"mode":"runOnceForAllItems","jsCode":build_headlines_js}))
B.append(tg_post("Send headlines","sendMessage",[1620,-360],"={{ $json.tg }}"))
# feedback branch
B.append(node("Get latest draft","n8n-nodes-base.httpRequest",4.2,[1100,80],{"method":"GET","url":RENDER_SVC+"/latest","options":{}},extra={"onError":"continueRegularOutput"}))
B.append(claude_node("Revise with Claude",[1100,-100],revise_body))
# shared tail (Extract draft ... Send approval buttons)
B += make_tail(1360,-100)
# like branch
B.append(tg_post("Ack like","answerCallbackQuery",[1280,700],"={{ JSON.stringify({ callback_query_id: $json.callbackQueryId, text: '👍 Got it' }) }}"))
ask_pub_expr = ("={{ JSON.stringify({ chat_id: $('Process updates').first().json.chatId, "
    "text: '✅ Publish now, or save it as a LinkedIn draft so you can add tags / schedule it yourself?', "
    "reply_markup: { inline_keyboard: [ "
    "[ { text: '✅ Publish now', callback_data: 'pub:' + $('Process updates').first().json.draftId } ], "
    "[ { text: '📝 Save as draft (add tags)', callback_data: 'draft:' + $('Process updates').first().json.draftId }, "
    "{ text: '🗑 Discard', callback_data: 'del:' + $('Process updates').first().json.draftId } ] "
    "] } }) }}")
B.append(tg_post("Ask publish","sendMessage",[1480,700],ask_pub_expr))
# publish branch (idempotent)
B.append(tg_post("Ack publish","answerCallbackQuery",[1460,760],"={{ JSON.stringify({ callback_query_id: $json.callbackQueryId, text: 'Publishing…' }) }}"))
B.append(node("Mark posted","n8n-nodes-base.httpRequest",4.2,[1640,760],
    {"method":"POST","url":RENDER_SVC+"/posted","sendBody":True,"specifyBody":"json",
     "jsonBody":"={{ JSON.stringify({ draft_id: $('Process updates').first().json.draftId }) }}","options":{}},
    extra={"onError":"continueRegularOutput"}))
B.append(node("First time?","n8n-nodes-base.if",2.2,[1820,760],IF_BOOL("first")))
B.append(node("Get draft","n8n-nodes-base.httpRequest",4.2,[2020,680],
    {"method":"GET","url":"="+RENDER_SVC+"/draft?id={{ $('Process updates').first().json.draftId }}","options":{}},
    extra={"onError":"continueRegularOutput"}))
B.append(node("Get card","n8n-nodes-base.httpRequest",4.2,[2220,680],
    {"method":"GET","url":"="+RENDER_SVC+"/card?id={{ $('Process updates').first().json.draftId }}","options":FILE_RESP}))
B.append(node("Post to LinkedIn","n8n-nodes-base.linkedIn",1,[2420,680],
    {"person":PERSON_URN,"postAs":"person","text":"={{ $('Get draft').first().json.postText }}",
     "shareMediaCategory":"IMAGE","binaryPropertyName":"data","additionalFields":{}},
    extra={"credentials":{"linkedInOAuth2Api":{"id":LINKEDIN_CRED,"name":"LinkedIn (Jon)"}}}))
B.append(tg_post("Confirm posted","sendMessage",[2620,680],
    "={{ JSON.stringify({ chat_id: $('Process updates').first().json.chatId, text: '✅ Posted to your LinkedIn.' }) }}"))
B.append(tg_post("Already posted","sendMessage",[2020,860],
    "={{ JSON.stringify({ chat_id: $('Process updates').first().json.chatId, text: '⚠️ That draft was already posted, skipping.' }) }}"))
# --- publish fork: image (default) vs video ---
B.append(node("Is video?","n8n-nodes-base.if",2.2,[2220,760],IF_BOOL("isVideo")))
LIHDR = {"parameters":[{"name":"LinkedIn-Version","value":"202608"},{"name":"X-Restli-Protocol-Version","value":"2.0.0"}]}
def li_node(label,pos,url,body):
    return node(label,"n8n-nodes-base.httpRequest",4.2,pos,
        {"method":"POST","url":url,"authentication":"predefinedCredentialType","nodeCredentialType":"linkedInOAuth2Api",
         "sendHeaders":True,"headerParameters":LIHDR,"sendBody":True,"specifyBody":"json","jsonBody":body,"options":{}},
        extra={"onError":"continueRegularOutput","credentials":{"linkedInOAuth2Api":{"id":LINKEDIN_CRED,"name":"LinkedIn (Jon)"}}})
init_body = "={{ JSON.stringify({ initializeUploadRequest: { owner: 'urn:li:person:"+PERSON_URN+"', fileSizeBytes: $('Get draft').first().json.videoBytes, uploadCaptions: false, uploadThumbnail: false } }) }}"
B.append(li_node("Init video upload",[2420,900],"https://api.linkedin.com/rest/videos?action=initializeUpload",init_body))
B.append(node("Upload video parts","n8n-nodes-base.httpRequest",4.2,[2620,900],
    {"method":"POST","url":RENDER_SVC+"/uploadvideo","sendBody":True,"specifyBody":"json",
     "jsonBody":"={{ JSON.stringify({ id: $('Process updates').first().json.draftId, instructions: $json.value.uploadInstructions }) }}","options":{"timeout":240000}},
    extra={"onError":"continueRegularOutput"}))
prep_video_js = """const iu = $('Init video upload').first().json;
const val = (iu && iu.value) ? iu.value : {};
const up = $('Upload video parts').first().json || {};
const post = String($('Get draft').first().json.postText || '');
const BS = String.fromCharCode(92);
const SP = "|{}[]()<>*_~" + BS;
let commentary = "";
for (let i = 0; i < post.length; i++) { const c = post[i]; if (SP.indexOf(c) >= 0) commentary += BS; commentary += c; }
return [{ json: { videoURN: val.video || '', uploadToken: val.uploadToken || '', etags: up.etags || [], commentary: commentary } }];"""
B.append(node("Prep video post","n8n-nodes-base.code",2,[2820,900],{"mode":"runOnceForAllItems","jsCode":prep_video_js}))
fin_body = "={{ JSON.stringify({ finalizeUploadRequest: { video: $('Prep video post').first().json.videoURN, uploadToken: $('Prep video post').first().json.uploadToken, uploadedPartIds: $('Prep video post').first().json.etags } }) }}"
B.append(li_node("Finalize video",[3020,900],"https://api.linkedin.com/rest/videos?action=finalizeUpload",fin_body))
postv_body = "={{ JSON.stringify({ author: 'urn:li:person:"+PERSON_URN+"', commentary: $('Prep video post').first().json.commentary, visibility: 'PUBLIC', distribution: { feedDistribution: 'MAIN_FEED', targetEntities: [], thirdPartyDistributionChannels: [] }, content: { media: { title: 'video', id: $('Prep video post').first().json.videoURN } }, lifecycleState: 'PUBLISHED', isReshareDisabledByAuthor: false }) }}"
B.append(li_node("Post video to LinkedIn",[3220,900],"https://api.linkedin.com/rest/posts",postv_body))
# save-as-draft branch (unpublished LinkedIn draft -> Jon adds tags + schedules/publishes natively)
B.append(tg_post("Ack draft","answerCallbackQuery",[1560,820],"={{ JSON.stringify({ callback_query_id: $json.callbackQueryId, text: 'Saving draft…' }) }}"))
B.append(node("Get draft text","n8n-nodes-base.httpRequest",4.2,[1760,820],
    {"method":"GET","url":"="+RENDER_SVC+"/draft?id={{ $('Process updates').first().json.draftId }}","options":{}},extra={"onError":"continueRegularOutput"}))
prep_draft_js = """const post = String($('Get draft text').first().json.postText || '');
const BS = String.fromCharCode(92);
const SP = "|{}[]()<>*_~" + BS;
let commentary = "";
for (let i = 0; i < post.length; i++) { const c = post[i]; if (SP.indexOf(c) >= 0) commentary += BS; commentary += c; }
return [{ json: { commentary: commentary } }];"""
B.append(node("Prep draft","n8n-nodes-base.code",2,[1960,820],{"mode":"runOnceForAllItems","jsCode":prep_draft_js}))
draft_body = "={{ JSON.stringify({ author:'urn:li:person:"+PERSON_URN+"', commentary: $('Prep draft').first().json.commentary, visibility:'PUBLIC', distribution:{ feedDistribution:'MAIN_FEED', targetEntities:[], thirdPartyDistributionChannels:[] }, lifecycleState:'DRAFT', isReshareDisabledByAuthor:false }) }}"
B.append(li_node("Create LinkedIn draft",[2160,820],"https://api.linkedin.com/rest/posts",draft_body))
B.append(tg_post("Confirm draft","sendMessage",[2360,820],"={{ JSON.stringify({ chat_id: $('Process updates').first().json.chatId, text: '📝 Saved to your LinkedIn drafts. In LinkedIn tap Start a post and the text will be there. Add your @tags and attach the image (the preview above), then schedule or publish it yourself. Nothing is public until you do.' }) }}"))
# discard branch
# ---- 📷 Photo / 🖼 Gallery: real photos instead of a generated card. One branch, the mode
# comes from which button was tapped. Both land in the card slot so publishing is unchanged.
B.append(node("Photos?","n8n-nodes-base.if",2.2,[1860,1040],IF_OR("event","pic","gallery")))
B.append(tg_post("Ack photos","answerCallbackQuery",[2060,1000],
    "={{ JSON.stringify({ callback_query_id: $('Process updates').first().json.callbackQueryId, text: '📷 Finding photos…' }) }}"))
B.append(node("Get draft (photos)","n8n-nodes-base.httpRequest",4.2,[2260,1000],
    {"method":"GET","url":"="+RENDER_SVC+"/draft?id={{ $('Process updates').first().json.draftId }}","options":{}},
    extra={"onError":"continueRegularOutput"}))
B.append(node("Make photos","n8n-nodes-base.httpRequest",4.2,[2460,1000],
    {"method":"POST","url":RENDER_SVC+"/photos","sendBody":True,"specifyBody":"json",
     "jsonBody":("={{ JSON.stringify({ draft_id: $('Process updates').first().json.draftId, "
                 "subject: $('Get draft (photos)').first().json.imageSubject || '', "
                 "article: $('Get draft (photos)').first().json.articleImage || '', "
                 "mode: $('Process updates').first().json.event, want: 4 }) }}"),
     "options":{"timeout":40000}},extra={"onError":"continueRegularOutput"}))
B.append(node("Photos ok?","n8n-nodes-base.if",2.2,[2660,1000],IF_BOOL("ok")))
B.append(node("Download photos","n8n-nodes-base.httpRequest",4.2,[2860,960],
    {"method":"GET","url":"="+RENDER_SVC+"/card?id={{ $('Process updates').first().json.draftId }}","options":FILE_RESP},
    extra={"onError":"continueRegularOutput"}))
PHOTOS_CAPTION = ("'📷 ' + $('Make photos').first().json.used + ' photo(s) of ' + String($('Get draft (photos)').first().json.imageSubject || 'the subject') "
                  "+ ' (Wikimedia). Tap a style to go back to the card.'")
B.append(tg_edit_media("Edit photos preview",[2960,900],PHOTOS_CAPTION))
B.append(node("Photos edited?","n8n-nodes-base.if",2.2,[3060,860],IF_BOOL("ok")))
B.append(node("Photos for resend","n8n-nodes-base.httpRequest",4.2,[3060,1020],
    {"method":"GET","url":"="+RENDER_SVC+"/card?id={{ $('Process updates').first().json.draftId }}","options":FILE_RESP},
    extra={"onError":"continueRegularOutput"}))
B.append(node("Send photos","n8n-nodes-base.telegram",1.2,[3160,960],
    {"operation":"sendPhoto","chatId":CHAT_ID,"binaryData":True,"binaryPropertyName":"data",
     "additionalFields":{"caption":"={{ " + PHOTOS_CAPTION + " }}"}},
    extra={"credentials":{"telegramApi":{"id":TELEGRAM_CRED,"name":"Telegram (Agent bot)"}}}))
# 👎 Skip here used to discard the whole DRAFT (skip:<id>) when the owner only meant "not this image"
# back:<draft>:<style>:<the draft's buttons message> — so the tap can restore the card on the
# draft's ORIGINAL preview and delete the suggestion (clip + question). Before, it turned the
# clip message into the card and left "Use this clip for the post?" under it.
BACK_BTN = ("{ text: '↩️ Back to the card', callback_data: 'back:' + $('Process updates').first().json.draftId + ':' + "
            "(Number(($('Get draft (%s)').first().json || {}).style) || 0) + ':' + $('Process updates').first().json.messageId }")
B.append(tg_post("Send photos buttons","sendMessage",[3260,960],
    ("={{ JSON.stringify({ chat_id: $('Process updates').first().json.chatId, "
     "text: 'Use this as the post image?', "
     "reply_markup: { inline_keyboard: [[ "
     "{ text: '👍 Like it', callback_data: 'like:' + $('Process updates').first().json.draftId }, "
     + BACK_BTN % "photos" + " ]] } }) }}")))
B.append(tg_post("Photos failed","sendMessage",[2860,1100],
    "={{ JSON.stringify({ chat_id: $('Process updates').first().json.chatId, "
    "text: '📷 ' + ($('Make photos').first().json.reason || 'no usable photo found') + '. The card is still there.' }) }}"))
# ---- 📸 Article shot: capture the top of the story page (masthead, headline, lead image)
B.append(node("Shot?","n8n-nodes-base.if",2.2,[1860,900],IF_EQ("event","shot")))
B.append(tg_post("Ack shot","answerCallbackQuery",[2060,860],
    "={{ JSON.stringify({ callback_query_id: $('Process updates').first().json.callbackQueryId, text: '📸 Capturing the page…' }) }}"))
B.append(node("Get draft (shot)","n8n-nodes-base.httpRequest",4.2,[2260,860],
    {"method":"GET","url":"="+RENDER_SVC+"/draft?id={{ $('Process updates').first().json.draftId }}","options":{}},
    extra={"onError":"continueRegularOutput"}))
B.append(node("Make shot","n8n-nodes-base.httpRequest",4.2,[2460,860],
    {"method":"POST","url":RENDER_SVC+"/shot","sendBody":True,"specifyBody":"json",
     "jsonBody":("={{ JSON.stringify({ draft_id: $('Process updates').first().json.draftId, "
                 "url: $('Get draft (shot)').first().json.sourceUrl || '' }) }}"),
     "options":{"timeout":30000}},extra={"onError":"continueRegularOutput"}))
B.append(node("Shot ok?","n8n-nodes-base.if",2.2,[2660,860],IF_BOOL("ok")))
B.append(node("Download shot","n8n-nodes-base.httpRequest",4.2,[2860,820],
    {"method":"GET","url":"="+RENDER_SVC+"/card?id={{ $('Process updates').first().json.draftId }}","options":FILE_RESP},
    extra={"onError":"continueRegularOutput"}))
SHOT_CAPTION = ("'📸 Clipping of ' + String($('Get draft (shot)').first().json.sourceUrl || '').replace(/^https?:\\/\\//,'').split('/')[0] "
                "+ '. Tap a style to go back to the card.'")
B.append(tg_edit_media("Edit shot preview",[2960,760],SHOT_CAPTION))
B.append(node("Shot edited?","n8n-nodes-base.if",2.2,[3060,720],IF_BOOL("ok")))
B.append(node("Shot for resend","n8n-nodes-base.httpRequest",4.2,[3060,880],
    {"method":"GET","url":"="+RENDER_SVC+"/card?id={{ $('Process updates').first().json.draftId }}","options":FILE_RESP},
    extra={"onError":"continueRegularOutput"}))
B.append(node("Send shot","n8n-nodes-base.telegram",1.2,[3160,820],
    {"operation":"sendPhoto","chatId":CHAT_ID,"binaryData":True,"binaryPropertyName":"data",
     "additionalFields":{"caption":"={{ " + SHOT_CAPTION + " }}"}},
    extra={"credentials":{"telegramApi":{"id":TELEGRAM_CRED,"name":"Telegram (Agent bot)"}}}))
B.append(tg_post("Send shot buttons","sendMessage",[3260,820],
    ("={{ JSON.stringify({ chat_id: $('Process updates').first().json.chatId, "
     "text: 'Use this clipping as the post image?', "
     "reply_markup: { inline_keyboard: [[ "
     "{ text: '👍 Like it', callback_data: 'like:' + $('Process updates').first().json.draftId }, "
     + BACK_BTN % "shot" + " ]] } }) }}")))
B.append(tg_post("Shot failed","sendMessage",[2860,960],
    "={{ JSON.stringify({ chat_id: $('Process updates').first().json.chatId, "
    "text: '📸 ' + ($('Make shot').first().json.reason || 'could not capture that page') }) }}"))
# ---- 🎬 Clip from the source video: /autoclip picks the video, reads its transcript and
# cuts the moment the story is actually about. ~18s, which stays inside the 65s claim window.
B.append(node("Clip?","n8n-nodes-base.if",2.2,[1860,760],IF_EQ("event","clip")))
B.append(tg_post("Ack clip","answerCallbackQuery",[2060,700],
    "={{ JSON.stringify({ callback_query_id: $('Process updates').first().json.callbackQueryId, text: '🎬 Finding the moment…' }) }}"))
B.append(node("Get draft (clip)","n8n-nodes-base.httpRequest",4.2,[2260,700],
    {"method":"GET","url":"="+RENDER_SVC+"/draft?id={{ $('Process updates').first().json.draftId }}","options":{}},
    extra={"onError":"continueRegularOutput"}))
clip_body = ("={{ JSON.stringify({ draft_id: $('Process updates').first().json.draftId, "
    "query: [$('Get draft (clip)').first().json.imageSubject, $('Get draft (clip)').first().json.hook]"
    ".filter(Boolean).join(' ').slice(0,120), "
    "keywords: [$('Get draft (clip)').first().json.imageSubject, "
    "String($('Get draft (clip)').first().json.postText || '').split('\\n')[0]]"
    ".filter(Boolean).join(' ').slice(0,160), dur: 15, handle: 'in/" + LINKEDIN_HANDLE + "' }) }}")
B.append(node("Auto clip","n8n-nodes-base.httpRequest",4.2,[2460,700],
    {"method":"POST","url":RENDER_SVC+"/autoclip","sendBody":True,"specifyBody":"json",
     "jsonBody":clip_body,"options":{"timeout":420000}},extra={"onError":"continueRegularOutput"}))
B.append(node("Clip ok?","n8n-nodes-base.if",2.2,[2660,700],IF_BOOL("ok")))
B.append(node("Download clip","n8n-nodes-base.httpRequest",4.2,[2860,640],
    {"method":"GET","url":"="+RENDER_SVC+"/vidfile?id={{ $('Process updates').first().json.draftId }}","options":FILE_RESP},
    extra={"onError":"continueRegularOutput"}))
B.append(node("Send clip","n8n-nodes-base.telegram",1.2,[3060,640],
    {"operation":"sendVideo","chatId":CHAT_ID,"binaryData":True,"binaryPropertyName":"data",
     "additionalFields":{"caption":"=🎬 {{ String(($('Auto clip').first().json.video || {}).channel || '') }} · {{ String(($('Auto clip').first().json.video || {}).title || '').slice(0,70) }}\n⏱ {{ $('Auto clip').first().json.startLabel }}{{ $('Auto clip').first().json.fallbackMoment ? ' (no keyword match — rough guess)' : '' }}\n🗣 {{ String($('Auto clip').first().json.matched || '').slice(0,120) }}"}},
    extra={"credentials":{"telegramApi":{"id":TELEGRAM_CRED,"name":"Telegram (Agent bot)"}}}))
clip_btn_expr = ("={{ JSON.stringify({ chat_id: $('Process updates').first().json.chatId, "
    "text: 'Use this clip for the post? 👍 then ✅ publishes it as a video.', "
    "reply_markup: { inline_keyboard: [[ "
    "{ text: '👍 Like it', callback_data: 'like:' + $('Process updates').first().json.draftId }, "
    + BACK_BTN % "clip" + " ]] } }) }}")
B.append(tg_post("Send clip buttons","sendMessage",[3260,640],clip_btn_expr))
B.append(tg_post("Clip failed","sendMessage",[2860,800],
    "={{ JSON.stringify({ chat_id: $('Process updates').first().json.chatId, "
    "text: '🎬 ' + ((($('Auto clip').first().json || {}).reason || 'no video found') === 'no video found' "
    "? 'No usable video found for this story — keeping the card.' : $('Auto clip').first().json.reason + '.') }) }}"))
B.append(tg_post("Ack skip","answerCallbackQuery",[1820,600],"={{ JSON.stringify({ callback_query_id: $json.callbackQueryId, text: 'Skipped' }) }}"))
B.append(tg_post("Confirm skip","sendMessage",[2020,600],"={{ JSON.stringify({ chat_id: $('Process updates').first().json.chatId, text: '🗑 Discarded.' }) }}"))
B.append(sticky("Note P","## Continuous listener (outbound only)\nONE held connection long-polls Telegram and loops back after each event, so everything is near-instant. Render-service /claim = single listener (no dupes), /posted = publish once only.\n\nInputs it understands:\n• 👍 Like → ✅ Publish (posts with image)\n• 👎 Skip / 🗑 Discard\n• plain text → revise latest draft\n• a link → post about that article\n• /new <topic> → post about a topic\n• /new → post from today's news\n• a photo → swap the draft image\n• a video → watermark + attach it (posts as native video)\n• /help → guide",[-820,-40],470,380,5))

connsB = {
 "Every minute":{"main":[[{"node":"Claim / heartbeat","type":"main","index":0}]]},
 "Claim / heartbeat":{"main":[[{"node":"Claimed?","type":"main","index":0}]]},
 "Claimed?":{"main":[[{"node":"Load offset","type":"main","index":0}],[]]},
 "Load offset":{"main":[[{"node":"Cap reached?","type":"main","index":0}]]},
 "Cap reached?":{"main":[[{"node":"Release claim","type":"main","index":0}],[{"node":"Get updates","type":"main","index":0}]]},
 "Get updates":{"main":[[{"node":"Process updates","type":"main","index":0}]]},
 "Process updates":{"main":[[{"node":"Needs status?","type":"main","index":0}]]},
 "Needs status?":{"main":[[{"node":"Send status","type":"main","index":0}],[{"node":"Help?","type":"main","index":0}]]},
 "Send status":{"main":[[{"node":"Back to event","type":"main","index":0}]]},
 "Back to event":{"main":[[{"node":"Help?","type":"main","index":0}]]},
 "Help?":{"main":[[{"node":"Send guide","type":"main","index":0}],[{"node":"Headlines?","type":"main","index":0}]]},
 "Headlines?":{"main":[hl_entry,[{"node":"More?","type":"main","index":0}]]},
 "More?":{"main":[[{"node":"Ack more","type":"main","index":0}],[{"node":"Photocard?","type":"main","index":0}]]},
 "Photocard?":{"main":[[{"node":"Get file (card)","type":"main","index":0}],[{"node":"Photo?","type":"main","index":0}]]},
 "Get file (card)":{"main":[[{"node":"Set photo (card)","type":"main","index":0}]]},
 "Set photo (card)":{"main":[[{"node":"Build custom card","type":"main","index":0}]]},
 "Build custom card":{"main":[[{"node":"Render custom card","type":"main","index":0}]]},
 "Render custom card":{"main":[[{"node":"Send custom card","type":"main","index":0}]]},
 "Send custom card":{"main":[[{"node":"Send card button","type":"main","index":0}]]},
 "Send card button":{"main":[[{"node":LOOP,"type":"main","index":0}]]},
 "Cardstyle?":{"main":[[{"node":"Ack cardstyle","type":"main","index":0}],[{"node":"X post?","type":"main","index":0}]]},
 "X post?":{"main":[[{"node":"Get X post","type":"main","index":0}],[{"node":"LinkedIn link?","type":"main","index":0}]]},
 "LinkedIn link?":{"main":[[{"node":"LinkedIn link msg","type":"main","index":0}],[{"node":"Link?","type":"main","index":0}]]},
 "LinkedIn link msg":{"main":[[{"node":LOOP,"type":"main","index":0}]]},
 "Get X post":{"main":[[{"node":"X post ok?","type":"main","index":0}]]},
 "X post ok?":{"main":[[{"node":"Draft from X post","type":"main","index":0}],[{"node":"X post failed","type":"main","index":0}]]},
 "Draft from X post":{"main":[[{"node":"Extract draft","type":"main","index":0}]]},
 "X post failed":{"main":[[{"node":LOOP,"type":"main","index":0}]]},
 "Ack cardstyle":{"main":[[{"node":"Get card draft","type":"main","index":0}]]},
 "Get card draft":{"main":[[{"node":"Build card restyle","type":"main","index":0}]]},
 "Build card restyle":{"main":[[{"node":"Render card restyle","type":"main","index":0}]]},
 "Render card restyle":{"main":[[{"node":"Send card restyle","type":"main","index":0}]]},
 "Send card restyle":{"main":[[{"node":"Send cardstyle button","type":"main","index":0}]]},
 "Send cardstyle button":{"main":[[{"node":LOOP,"type":"main","index":0}]]},
 "Ack more":{"main":[hl_entry]},
 "Merge headlines":{"main":[[{"node":"Bundle headlines","type":"main","index":0}]]},
 "Bundle headlines":{"main":[[{"node":"Build headlines page","type":"main","index":0}]]},
 "Build headlines page":{"main":[[{"node":"Send headlines","type":"main","index":0}]]},
 "Send headlines":{"main":[[{"node":LOOP,"type":"main","index":0}]]},
 "Photo?":{"main":[[{"node":"Get latest (photo)","type":"main","index":0}],[{"node":"Video?","type":"main","index":0}]]},
 "Video?":{"main":[[{"node":"Get latest (video)","type":"main","index":0}],[{"node":"Restyle?","type":"main","index":0}]]},
 "Restyle?":{"main":[[{"node":"Ack restyle","type":"main","index":0}],[{"node":"Style?","type":"main","index":0}]]},
 "Style?":{"main":[[{"node":"Ack restyle","type":"main","index":0}],[{"node":"Cardstyle?","type":"main","index":0}]]},
 "Link?":{"main":[[{"node":"Fetch link","type":"main","index":0}],[{"node":"Topic?","type":"main","index":0}]]},
 "Topic?":{"main":[[{"node":"Draft from topic","type":"main","index":0}],[{"node":"News or again?","type":"main","index":0}]]},
 "News or again?":{"main":[[{"node":"Again tap?","type":"main","index":0}],[{"node":"Feedback?","type":"main","index":0}]]},
 "Again tap?":{"main":[[{"node":"Fresh news once?","type":"main","index":0}],npB_entry]},
 "Fresh news once?":{"main":[[{"node":"First again?","type":"main","index":0}]]},
 "First again?":{"main":[[{"node":"Ack again","type":"main","index":0}],[{"node":LOOP,"type":"main","index":0}]]},
 "Ack again":{"main":[npB_entry]},
 "Feedback?":{"main":[[{"node":"Get latest draft","type":"main","index":0}],[{"node":"Like?","type":"main","index":0}]]},
 "Like?":{"main":[[{"node":"Ack like","type":"main","index":0}],[{"node":"Publish?","type":"main","index":0}]]},
 "Publish?":{"main":[[{"node":"Ack publish","type":"main","index":0}],[{"node":"Draft?","type":"main","index":0}]]},
 "Draft?":{"main":[[{"node":"Ack draft","type":"main","index":0}],[{"node":"Discard?","type":"main","index":0}]]},
 "Ack draft":{"main":[[{"node":"Get draft text","type":"main","index":0}]]},
 "Get draft text":{"main":[[{"node":"Prep draft","type":"main","index":0}]]},
 "Prep draft":{"main":[[{"node":"Create LinkedIn draft","type":"main","index":0}]]},
 "Create LinkedIn draft":{"main":[[{"node":"Confirm draft","type":"main","index":0}]]},
 "Confirm draft":{"main":[[{"node":LOOP,"type":"main","index":0}]]},
 "Discard?":{"main":[[{"node":"Ack skip","type":"main","index":0}],[{"node":"Clip?","type":"main","index":0}]]},
 "Clip?":{"main":[[{"node":"Ack clip","type":"main","index":0}],[{"node":"Shot?","type":"main","index":0}]]},
 "Shot?":{"main":[[{"node":"Ack shot","type":"main","index":0}],[{"node":"Photos?","type":"main","index":0}]]},
 "Photos?":{"main":[[{"node":"Ack photos","type":"main","index":0}],[{"node":LOOP,"type":"main","index":0}]]},
 "Ack photos":{"main":[[{"node":"Get draft (photos)","type":"main","index":0}]]},
 "Get draft (photos)":{"main":[[{"node":"Make photos","type":"main","index":0}]]},
 "Make photos":{"main":[[{"node":"Photos ok?","type":"main","index":0}]]},
 "Photos ok?":{"main":[[{"node":"Download photos","type":"main","index":0}],[{"node":"Photos failed","type":"main","index":0}]]},
 "Download photos":{"main":[[{"node":"Edit photos preview","type":"main","index":0}]]},
 "Edit photos preview":{"main":[[{"node":"Photos edited?","type":"main","index":0}]]},
 "Photos edited?":{"main":[[{"node":"Photos status","type":"main","index":0}],[{"node":"Photos for resend","type":"main","index":0}]]},
 "Photos status":{"main":[[{"node":LOOP,"type":"main","index":0}]]},
 "Photos for resend":{"main":[[{"node":"Send photos","type":"main","index":0}]]},
 "Send photos":{"main":[[{"node":"Send photos buttons","type":"main","index":0}]]},
 "Send photos buttons":{"main":[[{"node":"Photos status gone","type":"main","index":0}]]},
 "Photos status gone":{"main":[[{"node":LOOP,"type":"main","index":0}]]},
 "Photos failed":{"main":[[{"node":LOOP,"type":"main","index":0}]]},
 "Ack shot":{"main":[[{"node":"Get draft (shot)","type":"main","index":0}]]},
 "Get draft (shot)":{"main":[[{"node":"Make shot","type":"main","index":0}]]},
 "Make shot":{"main":[[{"node":"Shot ok?","type":"main","index":0}]]},
 "Shot ok?":{"main":[[{"node":"Download shot","type":"main","index":0}],[{"node":"Shot failed","type":"main","index":0}]]},
 "Download shot":{"main":[[{"node":"Edit shot preview","type":"main","index":0}]]},
 "Edit shot preview":{"main":[[{"node":"Shot edited?","type":"main","index":0}]]},
 "Shot edited?":{"main":[[{"node":"Shot status","type":"main","index":0}],[{"node":"Shot for resend","type":"main","index":0}]]},
 "Shot status":{"main":[[{"node":LOOP,"type":"main","index":0}]]},
 "Shot for resend":{"main":[[{"node":"Send shot","type":"main","index":0}]]},
 "Send shot":{"main":[[{"node":"Send shot buttons","type":"main","index":0}]]},
 "Send shot buttons":{"main":[[{"node":"Shot status gone","type":"main","index":0}]]},
 "Shot status gone":{"main":[[{"node":LOOP,"type":"main","index":0}]]},
 "Shot failed":{"main":[[{"node":LOOP,"type":"main","index":0}]]},
 "Ack clip":{"main":[[{"node":"Get draft (clip)","type":"main","index":0}]]},
 "Get draft (clip)":{"main":[[{"node":"Auto clip","type":"main","index":0}]]},
 "Auto clip":{"main":[[{"node":"Clip ok?","type":"main","index":0}]]},
 "Clip ok?":{"main":[[{"node":"Download clip","type":"main","index":0}],[{"node":"Clip failed","type":"main","index":0}]]},
 "Download clip":{"main":[[{"node":"Send clip","type":"main","index":0}]]},
 "Send clip":{"main":[[{"node":"Send clip buttons","type":"main","index":0}]]},
 "Send clip buttons":{"main":[[{"node":"Clip status gone","type":"main","index":0}]]},
 "Clip status gone":{"main":[[{"node":LOOP,"type":"main","index":0}]]},
 "Clip failed":{"main":[[{"node":LOOP,"type":"main","index":0}]]},
 # help
 "Send guide":{"main":[[{"node":LOOP,"type":"main","index":0}]]},
 # photo swap
 "Get latest (photo)":{"main":[[{"node":"Have draft?","type":"main","index":0}]]},
 "Have draft?":{"main":[[{"node":"Get file","type":"main","index":0}],[{"node":"No draft msg","type":"main","index":0}]]},
 "No draft msg":{"main":[[{"node":LOOP,"type":"main","index":0}]]},
 "Get file":{"main":[[{"node":"Set photo","type":"main","index":0}]]},
 "Set photo":{"main":[[{"node":"Photo stored?","type":"main","index":0}]]},
 "Photo stored?":{"main":[[{"node":"Build swap card","type":"main","index":0}],[{"node":"Photo store failed","type":"main","index":0}]]},
 "Photo store failed":{"main":[[{"node":LOOP,"type":"main","index":0}]]},
 "Build swap card":{"main":[[{"node":"Render swap card","type":"main","index":0}]]},
 "Render swap card":{"main":[[{"node":"Send photo preview","type":"main","index":0}]]},
 "Send photo preview":{"main":[[{"node":"Send swap buttons","type":"main","index":0}]]},
 "Send swap buttons":{"main":[[{"node":LOOP,"type":"main","index":0}]]},
 # video swap
 "Get latest (video)":{"main":[[{"node":"Get file (v)","type":"main","index":0}]]},
 "Get file (v)":{"main":[[{"node":"Set video","type":"main","index":0}]]},
 "Set video":{"main":[[{"node":"Download watermarked","type":"main","index":0}]]},
 "Download watermarked":{"main":[[{"node":"Send video preview","type":"main","index":0}]]},
 "Send video preview":{"main":[[{"node":"Send video buttons","type":"main","index":0}]]},
 "Send video buttons":{"main":[[{"node":LOOP,"type":"main","index":0}]]},
 # restyle
 "Ack restyle":{"main":[[{"node":"Get latest (restyle)","type":"main","index":0}]]},
 "Get latest (restyle)":{"main":[[{"node":"Restyle draft?","type":"main","index":0}]]},
 "Restyle draft?":{"main":[[{"node":"Build restyle card","type":"main","index":0}],[{"node":"Restyle expired","type":"main","index":0}]]},
 "Restyle expired":{"main":[[{"node":LOOP,"type":"main","index":0}]]},
 "Build restyle card":{"main":[[{"node":"Render restyle","type":"main","index":0}]]},
 "Render restyle":{"main":[[{"node":"Edit restyle preview","type":"main","index":0}]]},
 "Edit restyle preview":{"main":[[{"node":"Restyle edited?","type":"main","index":0}]]},
 "Restyle edited?":{"main":[[{"node":"Back tap?","type":"main","index":0}],[{"node":"Card for resend","type":"main","index":0}]]},
 "Back tap?":{"main":[[{"node":"Delete suggestion","type":"main","index":0}],[{"node":"Restyle status","type":"main","index":0}]]},
 "Delete suggestion":{"main":[[{"node":"Restyle status","type":"main","index":0}]]},
 "Restyle status":{"main":[[{"node":LOOP,"type":"main","index":0}]]},
 "Card for resend":{"main":[[{"node":"Send restyle preview","type":"main","index":0}]]},
 "Send restyle preview":{"main":[[{"node":"Send restyle buttons","type":"main","index":0}]]},
 "Send restyle buttons":{"main":[[{"node":"Restyle status gone","type":"main","index":0}]]},
 "Restyle status gone":{"main":[[{"node":LOOP,"type":"main","index":0}]]},
 # link
 "Fetch link":{"main":[[{"node":"Clean article","type":"main","index":0}]]},
 "Clean article":{"main":[[{"node":"Article ok?","type":"main","index":0}]]},
 "Article ok?":{"main":[[{"node":"Draft from link","type":"main","index":0}],[{"node":"Link blocked msg","type":"main","index":0}]]},
 "Link blocked msg":{"main":[[{"node":"Get headline","type":"main","index":0}]]},
 "Get headline":{"main":[[{"node":"Find alt news","type":"main","index":0}]]},
 "Find alt news":{"main":[[{"node":"Alt found?","type":"main","index":0}]]},
 "Alt found?":{"main":[[{"node":"Draft from alt","type":"main","index":0}],[{"node":"Link unreadable","type":"main","index":0}]]},
 "Draft from alt":{"main":[[{"node":"Extract draft","type":"main","index":0}]]},
 "Link unreadable":{"main":[[{"node":LOOP,"type":"main","index":0}]]},
 "Draft from link":{"main":[[{"node":"Extract draft","type":"main","index":0}]]},
 # topic
 "Draft from topic":{"main":[[{"node":"Extract draft","type":"main","index":0}]]},
 # news / different-news pipeline conns are merged in via npB_conns below
 # feedback
 "Get latest draft":{"main":[[{"node":"Revise with Claude","type":"main","index":0}]]},
 "Revise with Claude":{"main":[[{"node":"Extract draft","type":"main","index":0}]]},
 # like
 "Ack like":{"main":[[{"node":"Ask publish","type":"main","index":0}]]},
 "Ask publish":{"main":[[{"node":LOOP,"type":"main","index":0}]]},
 # publish
 "Ack publish":{"main":[[{"node":"Mark posted","type":"main","index":0}]]},
 "Mark posted":{"main":[[{"node":"First time?","type":"main","index":0}]]},
 "First time?":{"main":[[{"node":"Get draft","type":"main","index":0}],[{"node":"Already posted","type":"main","index":0}]]},
 "Get draft":{"main":[[{"node":"Is video?","type":"main","index":0}]]},
 "Is video?":{"main":[[{"node":"Init video upload","type":"main","index":0}],[{"node":"Get card","type":"main","index":0}]]},
 "Get card":{"main":[[{"node":"Post to LinkedIn","type":"main","index":0}]]},
 "Post to LinkedIn":{"main":[[{"node":"Confirm posted","type":"main","index":0}]]},
 "Init video upload":{"main":[[{"node":"Upload video parts","type":"main","index":0}]]},
 "Upload video parts":{"main":[[{"node":"Prep video post","type":"main","index":0}]]},
 "Prep video post":{"main":[[{"node":"Finalize video","type":"main","index":0}]]},
 "Finalize video":{"main":[[{"node":"Post video to LinkedIn","type":"main","index":0}]]},
 "Post video to LinkedIn":{"main":[[{"node":"Confirm posted","type":"main","index":0}]]},
 "Confirm posted":{"main":[[{"node":LOOP,"type":"main","index":0}]]},
 "Already posted":{"main":[[{"node":LOOP,"type":"main","index":0}]]},
 # discard
 "Ack skip":{"main":[[{"node":"Confirm skip","type":"main","index":0}]]},
 "Confirm skip":{"main":[[{"node":LOOP,"type":"main","index":0}]]},
}
for i,(nm,url) in enumerate(NEWS_FEEDS):
    connsB["HL " + nm.split(": ")[-1]] = {"main":[[{"node":"Merge headlines","type":"main","index":i}]]}
connsB.update(npB_conns)
connsB.update(TAIL_CONNS)
connsB["Send approval buttons"] = {"main":[[{"node":"Draft status gone","type":"main","index":0}]]}
connsB["Draft status gone"] = {"main":[[{"node":LOOP,"type":"main","index":0}]]}
connsB["Draft failed"] = {"main":[[{"node":LOOP,"type":"main","index":0}]]}
# ---- status message: "⏳ working on it" at the bottom of the chat for every slow request; when done
# it becomes "✅ … ⬆️" (the preview above was changed in place), disappears (the result appears below),
# or becomes "⚠️ reason". The owner tapped Article shot and could not tell it had worked.
STATUS_ID = "$('Send status').first().json.result.message_id"
def tg_status(label, pos, text_expr):
    return tg_post(label, "editMessageText", pos, "={{ JSON.stringify({ chat_id: $('Process updates').first().json.chatId, message_id: " + STATUS_ID + ", text: " + text_expr + " }) }}")
def tg_status_gone(label, pos):
    return tg_post(label, "deleteMessage", pos, "={{ JSON.stringify({ chat_id: $('Process updates').first().json.chatId, message_id: " + STATUS_ID + " }) }}")
B.append(node("Needs status?","n8n-nodes-base.if",2.2,[260,300],IF_NOTEMPTY("status")))
B.append(tg_post("Send status","sendMessage",[340,220],"={{ JSON.stringify({ chat_id: $json.chatId, text: $json.status }) }}"))
B.append(node("Back to event","n8n-nodes-base.code",2,[420,220],{"mode":"runOnceForAllItems","jsCode":"return [{ json: $('Process updates').first().json }];   // the routing IFs read the event from $json"}))
UP = " + ' The preview above was updated \u2b06\ufe0f'"
B.append(tg_status("Restyle status",[1860,-760],"($('Process updates').first().json.event === 'back' ? '\u2705 Back to the card.' : '\u2705 ' + $('Build restyle card').first().json.styleName + ' applied.')" + UP))
B.append(tg_status_gone("Restyle status gone",[1860,-560]))
B.append(tg_status("Photos status",[3160,860],"'\u2705 ' + $('Make photos').first().json.used + ' photo(s) of ' + String($('Get draft (photos)').first().json.imageSubject || 'the subject') + ' applied.'" + UP))
B.append(tg_status_gone("Photos status gone",[3360,960]))
B.append(tg_status("Shot status",[3160,720],"'\u2705 Article shot applied.'" + UP))
B.append(tg_status_gone("Shot status gone",[3360,820]))
B.append(tg_status_gone("Clip status gone",[3360,640]))
B.append(tg_status_gone("Draft status gone",[2600,-100]))
for _n in B:
    if _n["name"] in ("Restyle expired", "Photos failed", "Shot failed", "Clip failed", "X post failed", "Link unreadable", "Draft failed"):
        _p = _n["parameters"]
        assert _p["url"].endswith("/sendMessage") and "JSON.stringify({ chat_id: " in _p["jsonBody"], _n["name"]
        _p["url"] = _p["url"][:-len("sendMessage")] + "editMessageText"
        _p["jsonBody"] = _p["jsonBody"].replace("JSON.stringify({ chat_id: ", "JSON.stringify({ message_id: " + STATUS_ID + ", chat_id: ", 1)
        _p["jsonBody"] = _p["jsonBody"].replace("text: '", "text: '\u26a0\ufe0f ", 1) if "\u26a0" not in _p["jsonBody"] else _p["jsonBody"]
wfB = {"name":"LinkedIn Approvals (poll)","nodes":B,"connections":connsB,
       "settings":{"executionOrder":"v1","saveDataSuccessExecution":"none","saveExecutionProgress":False,"saveDataErrorExecution":"all"}}
json.dump(wfB, open(OUT_B,"w"))

print("WF-A nodes:", len(A), "| WF-B nodes:", len(B))
