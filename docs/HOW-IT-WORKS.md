# How it works

1. **Channel.** The app keeps one long-poll (`GET /phone/api/next?wait=25`) open to the owner's computer over
   Tailscale HTTPS (`tailscale serve`). The phone always calls out, so it works on mobile data and opens no port.
   Every request needs the owner's Tailscale identity (the `Tailscale-User-Login` header; empty config = refused),
   then the paired device key (only its hash is stored), then a size and shape check.
2. **Pairing.** `phone.py pair` prints an 8-digit code (one use, 15 min, 5 wrong tries kill it). The app sends it
   once and gets its device id and key.
3. **Find.** `phone_find_contact` sends a `find` order; the phone matches the spoken name against its contacts
   (one-letter slips allowed for longer names) and returns name, label, last 3 digits and whether the number may
   ever be rung or texted (the number rules). The computer hands Claude a one-use token per match (60 s).
4. **Call or text.** `phone_call(token)` / `phone_text(token, words)` queue the order. The phone waits until any
   Claude voice call has ended (audio back to normal for 2.5 s; gives up after 60 s), re-checks the contact and the
   number rules, then dials, or sends with SmsManager (sent and delivery reports). Without SMS permission it
   shows a "Tap to text" notification that opens Messages pre-filled.
5. **Outcomes.** The phone reports what happened (kept and resent if it had no signal); `phone_rundown` says each
   outcome once: sent, delivered, cancelled, refused.
6. **Inbox.** New incoming texts and missed calls go to the computer (sender = saved name, sender name, or the last
   3 digits) and `phone_inbox` reads them. Words stay in `~/.phone-helper/inbox.json` (600), 7 days, never logged.
7. **Log.** Every event is one line in `~/.phone-helper/phone.log`; anything with 6+ digits in a row is refused.
