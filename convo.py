"""
Conversation brain: what did they just say, where are we in the conversation, and what should we say next.
Follows DM_PLAYBOOK.md: value first -> understand them -> earned offer -> price only when allowed.
Used for AI hints (always) and as the fallback when no AI key works.
"""
import re

INTENTS = {   # checked in this order (first match wins)
    "opt_out":      r"\b(stop|unsubscribe|not interested|leave me alone|remove me|don'?t (contact|message|text|email) me)\b",
    "who":          r"who (is|are) (this|you)|is this a scam|\bscam\b|how did you get (my|this)|where did you get",
    "price":        r"how much|\bprices?\b|\bcosts?\b|\bcharge|\bquote\b|£|\bfees?\b|\brates?\b",
    "expensive":    r"(too|bit|quite) (expensive|much|pricey)|can'?t afford|no budget|out of (my )?budget",
    "diy":          r"(i|we)('ll| will| can| could)? (do|sort|fix|change|handle) (it|that|this|them)|myself|ourselves|i('ll| will) have a look",
    "has_someone":  r"(my|our) (son|daughter|nephew|niece|brother|sister|friend|guy|developer|designer|web ?(guy|person|designer)|it (guy|person))|someone (does|already|who)|already (have|got) (someone|a )",
    "more_info":    r"more (info|information|details)|send (me )?(some )?(details|info)|email me|what do you (do|offer)|how does (it|this) work",
    "yes":          r"^\s*(yes|yeah|yep|yh|ya|sure|ok(ay)?|go on|please( do)?|send( it| it over)?|what is it|show me|sounds good)\b",
    "interested":   r"\binterested\b|let'?s do|go ahead|can you (do|fix|help|sort)|when can you|how (long|soon)|book (a call|you in|it in)|call me|let'?s talk",
    "no_worries":   r"don'?t worry|no worries|all good|it'?s fine|we'?re (fine|ok|good)|thanks? (for|but)|thank you|appreciate it|cheers",
    "busy":         r"\bbusy\b|not (right )?now|maybe later|next (week|month)|another time|get back to you|after (christmas|the holidays|easter)",
    "word_of_mouth":r"word of mouth|\breferrals?\b|don'?t need (a )?(website|site)|get (enough )?work (from|through)",
}

def classify(text):
    t = (text or "").lower().strip()
    for name, pat in INTENTS.items():
        if re.search(pat, t): return name
    return "other"

def stage(lead):
    """Where are we? Returns dict with flags the AI and templates use."""
    mine = [h for h in lead.get("history", []) if h["from"] == "me"]
    theirs = [h for h in lead.get("history", []) if h["from"] == "them"]
    my_text = " ".join(h["text"].lower() for h in mine)
    their_text = " ".join(h["text"].lower() for h in theirs)
    value_sent = lead.get("value_sent") or bool(re.search(r"here you go|here'?s (the|what)|screenshot attached|easy fix|quick fix", my_text) and len(mine) >= 2)
    price_asked = bool(re.search(INTENTS["price"], their_text))
    wants_it = bool(re.search(INTENTS["interested"], their_text)) or lead.get("status") == "won"
    price_given = "£" in my_text and len(mine) >= 2
    if lead.get("status") == "won": name = "client"
    elif price_given: name = "price_given"
    elif wants_it or price_asked: name = "offer_allowed"
    elif value_sent: name = "understand"
    elif theirs: name = "deliver_value"
    else: name = "opener"
    return {"name": name, "value_sent": bool(value_sent), "price_asked": price_asked, "wants_it": wants_it,
            "price_allowed": price_asked or wants_it, "messages_from_them": len(theirs), "messages_from_me": len(mine)}

STAGE_HINT = {
    "deliver_value": "They replied to the opener. Deliver the VALUE now: describe what their customers experience, give one free "
                     "fix they can do themselves, end with ONE curious question about their business. NO price, NO offer, NO 'I can do it for'.",
    "understand": "Value has been sent. Build rapport: reflect what they said, link the problem to their goal, ask at most one question. "
                  "Only move to an offer if they've shown they want it fixed. NO price unless they asked.",
    "offer_allowed": "They've asked about price or shown they want it. Offer options and a free next step (mockup or 10-minute call). "
                     "If they asked the price directly, give it with what's included, timeline and split payment. Then stop.",
    "price_given": "Price has been given. Answer their question, make the next step simple (scope, timeline, how to give access). No pressure.",
    "client": "They're a client. Be helpful and warm, and confirm next steps.",
    "opener": "No reply yet. Write a gentle follow-up with no price.",
}

INTENT_HINT = {
    "opt_out": "They want no more contact. One polite line, start with [DNC].",
    "who": "Be transparent: name, you're a web developer, their number/email is on their website which is how you spotted the issue; portfolio link. No ask.",
    "price": "They asked the price. Answer honestly with context, plus the free first step (mockup) as an option.",
    "expensive": "Shrink the scope (start with the most important fix) or split payment. Never just drop the price.",
    "diy": "Agree warmly, give the exact steps to fix it themselves, offer help if they get stuck. No selling.",
    "has_someone": "Make them look good: offer the list of issues for their person. Leave the door open.",
    "no_worries": "Thank them, leave one useful tip, close kindly. No selling, no question.",
    "busy": "Respect it. Ask if checking back in a couple of weeks is OK.",
    "more_info": "Ask what's most useful (cost, or what you'd change and why), keep it short.",
    "word_of_mouth": "Agree, then explain that referred people still check the website before calling. One question.",
    "yes": "They said yes. Deliver the value (see stage).",
    "interested": "They want it. Offer options + free mockup/call; give price only if asked.",
    "other": "Answer what they said naturally, following the stage.",
    "close": "They want it. Confirm scope, price, timeline and ONE simple next step (e.g. invite me as a collaborator). Payment details come from the human.",
}

def _first_issue(lead):
    import dm_agent
    for i in sorted(lead["scan"]["issues"], key=lambda i: dm_agent.PRIORITY.index(i["issue"]) if i["issue"] in dm_agent.PRIORITY else 99):
        s = lead.get("hook_text") if lead.get("source") == "company" else dm_agent.hook_sentence(i)
        if s: return i["issue"], s
    return None, lead.get("hook_text") or "a few things on the website could be costing you enquiries."

DIY_STEPS = {
    "empty_social_link": "in your site editor, click each social icon in the footer and paste the link to your own page",
    "broken_whatsapp_button": "edit the WhatsApp button and set the link to https://wa.me/44XXXXXXXXXX with your mobile number (no 0 at the start)",
    "no_whatsapp_button": "add a button linking to https://wa.me/44XXXXXXXXXX with your mobile number (no 0 at the start)",
    "fake_phone_number": "edit the call button and set the link to tel: followed by your real number",
    "broken_call_button": "edit the call button and set the link to tel: followed by your number",
    "fake_email": "either set up that mailbox with your email provider, or change the email on the site to one you check",
    "placeholder_text": "search the pages for the template wording and replace it with your own",
    "not_mobile_friendly": "check each page in your editor's mobile preview and fix anything that runs off the screen",
    "no_quote_option": "add a 'Request a quote' button at the top of the homepage that links to your contact form",
}

def template(lead, intent, settings, price):
    """Value-first fallback replies (used when the AI isn't available)."""
    name = settings.get("name") or "Ken"; portfolio = settings.get("portfolio") or ""
    biz = lead["business"]; st = stage(lead)
    issue, hook = _first_issue(lead)
    diy = DIY_STEPS.get(issue)
    customer_view = hook[0].upper() + hook[1:] if hook else ""
    if intent == "opt_out": return f"[DNC] No problem at all, sorry to bother you. All the best with {biz}!"
    if intent == "who":
        return (f"Fair question! I'm {name}, a web developer. Your details are on your website, which is how I spotted the issue. "
                + (f"Some of my work: {portfolio}" if portfolio else "Happy to show you some of my work if useful."))
    if intent == "diy":
        return ("Totally fair, it's a quick one" + (f": {diy}." if diy else ".") +
                " If you'd rather not spend the time on it, or get stuck, just give me a shout. Good luck with it!")
    if intent == "has_someone":
        return ("Great, it's worth passing this to them so they know exactly what to fix: " + hook +
                " Happy to help if they ever get stuck. All the best!")
    if intent == "no_worries":
        return ("No worries at all, glad it was useful" + (f". If you ever want to fix it yourself: {diy}." if diy else ".") +
                f" And if you ever want a hand with the site, you know where I am. All the best with {biz}!")
    if intent == "busy":
        return "No rush at all, I know how busy it gets. Would it be OK if I checked back in a couple of weeks?"
    if intent == "word_of_mouth":
        return ("That's the best kind of work! Most people who get your name from a friend still look at the website before they ring, "
                "so it's worth making sure it doesn't put them off. Do most of your customers contact you by phone or by message?")
    if intent == "more_info":
        return "Of course. Is it mainly the cost you'd like to know, or what I'd change and how it would help?"
    if intent == "expensive":
        return (f"Totally understand. We could start with just the most important fix, the one losing you enquiries, "
                "and leave the rest for later. Would that work better?")
    if intent == "price" or (st["price_allowed"] and intent in ("interested", "yes")):
        return (f"It depends whether you want just the quick fixes or the site set up to bring in bookings. The quick fixes are from £{price}, "
                "done within a couple of days, and you pay the second half once you've checked it. "
                "Want me to show you exactly what I'd change first? I can mock it up for free.")
    if intent == "interested":
        return ("Happy to help. I could just sort the quick fixes, or set the site up so customers can book you straight from their phone. "
                "Want me to mock up what your homepage could look like first? It's free, no obligation.")
    if not st["value_sent"]:     # yes / what is it / other → deliver value, no price
        return (f"Here's what I spotted: {customer_view}" + (f"\n\nIf you want to sort it yourself: {diy}." if diy else "") +
                "\n\nOut of interest, do most of your customers find you through Google, or mainly word of mouth?")
    return "Thanks for getting back to me! Is getting more enquiries through the website something you're focused on at the moment?"


# ---------------- your voice: how every draft should sound ----------------
DEFAULT_VOICE = {"directness": 2, "warmth": 4, "length": "short", "formality": "casual", "emoji": "rare",
                 "end_question": "natural", "notes": "", "examples": "", "banned": ""}

HUMAN_RULES = """## Sound like a real person (always)
- First respond to what THEY said, in their words, before anything else. Never ignore their message to push your point.
- Write the way a friendly local tradesperson texts: contractions (I'm, you're, it's), plain words, short sentences mixed with longer ones.
- Vary how you start. Don't open every message with "Hi" or "Thanks for getting back to me".
- No sales or corporate language: never "I hope this finds you well", "I'd love to", "leverage", "solutions", "boost your online presence",
  "don't hesitate to", "reach out", "at your earliest convenience", "game-changer", "take your business to the next level".
- No bullet points or headings in WhatsApp messages. No exclamation marks in every sentence.
- Never sound like a template. If a sentence could be sent to anyone, rewrite it so it's clearly about THEM.
- It's fine to be a little informal ("no worries", "fair enough", "makes sense"). Don't overdo it."""

def voice_prompt(v):
    v = {**DEFAULT_VOICE, **(v or {})}
    d = int(v["directness"]); w = int(v["warmth"])
    direct = {1: "Very gentle and indirect. Hint rather than state; soften everything ('might be worth…', 'no pressure at all').",
              2: "Gentle. Raise points softly, give them room, never push. Avoid blunt statements about their website.",
              3: "Balanced: clear but polite.",
              4: "Fairly direct: say what you mean plainly, still polite.",
              5: "Direct and to the point, while staying respectful."}[max(1, min(5, d))]
    warm = {1: "Neutral and businesslike.", 2: "Polite and friendly.", 3: "Friendly.",
            4: "Warm and personable: show genuine interest in them and their business.",
            5: "Very warm and personal, like chatting to a neighbour."}[max(1, min(5, w))]
    length = {"very_short": "1-2 short sentences.", "short": "2-4 short sentences. Get to the point.",
              "medium": "4-6 sentences.", "long": "Up to 8 sentences when it helps."}.get(v["length"], "2-4 short sentences.")
    formal = {"casual": "Casual, everyday UK English, like texting a customer.", "balanced": "Friendly but tidy.",
              "professional": "Professional and polished, still human."}.get(v["formality"], "")
    emoji = {"none": "Never use emoji.", "rare": "At most one emoji, and only if it fits naturally (often none).",
             "some": "One or two emoji are fine where natural."}.get(v["emoji"], "")
    q = {"always": "End with one easy question.", "natural": "Only end with a question when it's natural, not every time.",
         "rarely": "Rarely end with a question; statements are fine."}.get(v["end_question"], "")
    out = [HUMAN_RULES, "## Your voice settings (follow closely)", f"- Directness: {direct}", f"- Warmth: {warm}",
           f"- Length: {length}", f"- Tone: {formal}", f"- Emoji: {emoji}", f"- Questions: {q}"]
    if v["banned"].strip(): out.append(f"- NEVER use these words/phrases: {v['banned'].strip()}")
    if v["notes"].strip(): out.append(f"\n## The sender's own style notes (always follow, they override the playbook's examples)\n{v['notes'].strip()}")
    if v["examples"].strip():
        out.append(f"\n## Messages the sender has written themselves. Copy this voice, rhythm and wording style (not the content)\n{v['examples'].strip()}")
    return "\n".join(out)
