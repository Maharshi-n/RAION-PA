# The official website: maanarmadaeducation.org

The group's official website has a gateway page and one site each for MNDC, MNA and MNCE. Any admin can change
anything on it through you: notices, news, faculty and staff, the Chairperson's, Director's and Principal's
messages, photos, pages. Its code is the git folder `website`, next to `memory` and `site` (call it WEB below).
Hostinger puts a push to GitHub live in about 20 seconds.

## Which site
- **Official site (here):** what the public should read about the institutes: notices, news, people, messages,
  admissions information, photos, pages.
- **mndc.maharshinahar.in (memory/global/WEBAPPS.md):** apps and dashboards: fee summaries, registers, forms that
  save data, tools for staff.
- You decide which one a request belongs to; ask when unsure. Never put an app, an internal tool or anything meant
  only for staff on the official site.

## Never on the official site
Money (fees owed, salaries, accounts), staff matters, anything from the admins' private memory, students' personal
details (marks lists, admit cards, fee defaulters, phone numbers, addresses, Aadhaar, photos of documents), and
private people's phone numbers. The office numbers and emails already on the site are fine.

## First time on a PC (there is no WEB folder yet)
Do this when an admin first asks for website work. In PowerShell, in Raion's folder (the one holding `memory`):
1. Make the key, once (skip if the file exists):
   `ssh-keygen -q -t ed25519 -N '""' -C "raion-website-$env:COMPUTERNAME" -f "$env:USERPROFILE\.ssh\raion_website_ed25519"`
2. Send the admin the one line in `$env:USERPROFILE\.ssh\raion_website_ed25519.pub` and ask them to forward it to
   Maharshi, who adds it on GitHub (MNDC-Main-Website > Settings > Deploy keys > Add deploy key, with "Allow write
   access" ticked). It is a public key, safe to share. The file without .pub is the private key: it never leaves
   this PC and is never sent to anyone.
3. When they say it's added:
   ```
   $k = "$env:USERPROFILE/.ssh/raion_website_ed25519" -replace '\\','/'
   $env:GIT_SSH_COMMAND = "ssh -i '$k' -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new"
   git clone -q git@github.com:Maharshi-n/MNDC-Main-Website.git website
   Remove-Item Env:GIT_SSH_COMMAND
   git -C website config core.sshCommand "ssh -i '$k' -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new"
   git -C website config user.name "Raion"
   git -C website config user.email "raion@maanarmadaeducation.org"
   ```
   If the clone is refused, the key isn't on GitHub yet: say so and wait.

## How a change is made
Use Raion's own Python for build.py (it has Pillow): `..\.venv\Scripts\python.exe build.py`, run inside WEB.
1. `git pull` in WEB. Read WEB/DESIGN.md ("Words", "Photos", "Pages and menu") the first time in a chat.
2. Change only `content/` and add photos or files. Never edit the built HTML pages: the next build overwrites them.
   `<inst>` is mndc, mna or mnce.
   - Notices: `content/<inst>/notices.json`, items like
     `{"date": "2026-11-04", "title": "...", "category": "Examinations", "until": "2026-11-15", "file": "mndc-2026-11-04-exam-forms.pdf"}`.
     category is one of Admissions, Examinations, Scholarships, Events, General. `until` (the last date) and `file`
     are optional. A notice's PDF goes in `assets/files/`, named `<inst>-<date>-<short-name>.pdf`.
   - News: `content/<inst>/news.json`: date, slug (a-z, 0-9, -), headline, photo {"src", "alt"}, text (a list of
     paragraphs), photos (more photos). A story with no text gets no page of its own.
   - Pages: `content/<inst>/pages.json` (kinds: message, people, text; copy an existing one). A new page also needs
     its menu entry in `content/<inst>/site.json`, under the right group, as in DESIGN.md "Pages and menu".
   - Home page, menu, hero slides, photo strip, footer: `content/<inst>/site.json`. The leaders' messages shown on all
     three sites: `content/shared-pages.json`. The gateway: `content/gateway.json`.
3. Photos: `..\.venv\Scripts\python.exe build.py photo "<photo>" <inst>-<what> --for hero|split|news|reel|portrait|gallery`
   resizes it and strips its location data; then use `<inst>-<what>.jpg` in content with alt text saying what is in
   it. Pick photos from the photo cards you already have: no posters, flyers, cuttings, screenshots, phone
   watermarks ("Shot on ...") or GPS stamps. Don't open every photo again.
4. `..\.venv\Scripts\python.exe build.py` must end with "ok". If it lists problems, fix the content and run it again.
5. Show the admin the change **in text**: exactly what will appear (title, date, category, language, file), on
   which institute's site, and where (notices page, ticker, home page, which menu). Send photos as they are.
   **No page screenshots** for normal changes: the office PC is slow and they cost a lot. At most one, only for a
   brand-new page, never one per revision.
6. Only after their yes: `git add -A`, `git commit -m "<what>, approved by <admin's name>"`, `git push`. If the push
   is refused: `git pull --rebase`, build again, push once more. Still refused: tell them the site can't be updated
   right now and tell Maharshi.
7. About 30 seconds later, read the live page as text (`curl.exe -s https://maanarmadaeducation.org/<inst>/notices/`)
   and check the change is there, then send the link.
- Undo ("woh notice hata do"): remove it from content, build, tell them, push after their yes. A bigger mistake:
  `git revert` the commit, after their yes.
- Language: a notice or story goes up in the language the admin gives. Hindi stays Hindi, English stays English;
  translate only if they ask. The menu and buttons stay in English.
- Old notices leave the ticker by themselves after 30 days or after their last date. Don't push just for that.

## Spotting things worth posting
- Worth offering:
  - a notice you just made on letterhead;
  - a new university notice from a watched site (exam forms, results);
  - a batch of event photos, as a news story plus a few of the best photos;
  - a newspaper cutting about an institute;
  - an admission, exam or holiday date in the shared timeline.
- In an admin's own chat: offer it right there, in one line ("Isse MNDC website ke notices mein bhi daal doon?").
- Seen anywhere else (a staff member's chat, a watched site, photos coming in): you decide which admin to ask:
  - who handles that institute, event or matter;
  - who was involved.

  You also decide when to ask: in their next chat, a few at once, or sooner if a deadline is near. Use relay to
  reach them.
- Keep a short list in `memory/global/WEBSITE_LOG.md` of what you offered, posted and what was skipped. Never ask
  twice about the same thing. A skip is final unless something new happens.
- Nothing goes live without an admin's yes.
