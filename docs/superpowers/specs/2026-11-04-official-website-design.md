# Raion runs the official website: design

Date: 4 November 2026. Status: draft, waiting for Maharshi's approval.

## Why

The group's official website, maanarmadaeducation.org, was rebuilt this week as a plain static site: a gateway page plus the MNDC, MNA and MNCE sites. It comes from the GitHub repo `Maharshi-n/MNDC-Main-Website`, and Hostinger deploys it about 20 seconds after a push to `main`. Its look is fixed in `DESIGN.md` in that repo.

The content changes all the time: notices, news, faculty, messages, photos. Today only a developer can change it. The admins already run the office through Raion on WhatsApp, so they should be able to change the website the same way: "isse MNDC website pe notice daal do".

Raion also sees most of what belongs on the website before anyone thinks to post it:
- notices it makes on letterhead;
- university notices it watches;
- event photos that come in;
- newspaper cuttings;
- exam and holiday dates in the shared timeline.

It should offer to post these.

## Goals

1. **Any admin can change anything on the official site from a Raion chat**, by text, voice note, PDF or photos:
   - notices and news;
   - faculty and staff;
   - the Chairman's, Director's and Principal's messages;
   - gallery albums, photos, page text;
   - new pages from the `DESIGN.md` templates.
2. **Raion notices website-worthy things by itself**, offers them to an admin, and posts only on a yes.
3. **Every page keeps one look** (`DESIGN.md`), whoever made the change.
4. **The site grows into the IIIT Pune-style structure** in `DESIGN.md` ("Pages and menu"): notices, people, departments, messages, gallery and so on.

## Non-goals

- **Web apps and dashboards** stay on mndc.maharshinahar.in (`memory/global/WEBAPPS.md`, the `site` git folder). This feature doesn't change them.
- **No server-side code** on the official site: no logins, and no forms that store data. It stays static files on Hostinger.
- **Staff can't publish.** They can ask, and Raion takes the request to an admin.

## Anti-goals (a failure even if it works)

- The site starts to look patched together.
- Private things reach the public site:
  - money, staff matters, the admins' private memory;
  - students' personal details (marks lists, fee defaulters, admit cards), private people's phone numbers.
- An app or internal tool ends up on the official site.
- Raion pesters admins: too many suggestions, or asking again about something they skipped.
- Each change is slow and costly because Raion keeps taking screenshots on the slow office PC.

## Decided by Maharshi

- **Approval:** any admin can approve a change to any of the three sites.
- **Which site:** Raion decides which site something belongs on from its instructions and its own judgement. **No coded gate** checks or blocks what goes where.
- **Language:** notices and news go up in the language the admin approves. Hindi stays Hindi and English stays English, with no translation unless asked.
- **No repeated screenshots** for previews or checks: too slow on that PC, and too many tokens.
- **Who gets a suggestion, and when:** Raion decides from context, as it already does when it passes messages between people. It considers:
  - which admin handles that institute, event or matter;
  - who was involved;
  - how urgent it is.

  No fixed person, and no coded routing.
- **Structure and look:** follow `DESIGN.md`, including the IIIT Pune-style sections.

## Decided by Claude (Maharshi to check)

- **Way 2:** content lives in simple data files, and a page maker script builds the pages from them. Raion never hand-edits page HTML. Maharshi asked which way was better; this is the recommendation.
- **No new Raion tool:** reuse the web-apps pattern (a git folder, an instructions file, preview, yes, push), with a second deploy key.
- **`WEBSITE.md` lives with the code, not in memory,** so it updates by itself and memory is never touched.
- **Text-first previews:** at most one screenshot, and only for a brand-new page.
- **Expired notices:** they leave the ticker in the visitor's browser, so nothing is pushed without a yes.
- **First content:** taken from the `Website files/` documents and Raion's institute fact files.

## How it works

### 1. The website repo: content files and a page maker

```
MNDC-Main-Website/
  DESIGN.md
  build.py                 the page maker (Python, standard library + Pillow)
  content/
    gateway.json           the three institutes, helpline, society line
    mndc/
      site.json            name, accent, contacts, which menu pages exist, hero slides, intro facts
      notices.json         date, title, category, language, last date (optional), file (optional)
      news.json            date, headline, photos, text file
      people.json          name, post, department, other roles, qualification, subjects, photo
      departments.json
      pages/*.txt          long texts: messages, overview, vision; a blank line starts a new paragraph
      gallery/<year>-<event>.json   album: title, date, photos with captions
    mna/ ...  mnce/ ...    the same shape
  templates/               one HTML template per DESIGN.md template, plus the shared header, menu and footer
  assets/  site.css  site.js  photos/  files/   (files/ holds notice PDFs)
  index.html, mndc/..., mna/..., mnce/...      built pages, committed (Hostinger serves them as now)
```

**`python build.py`** rebuilds every page from `content/`:
- The menu, header and footer are written once in `templates/` and appear on every page.
- One notice in `notices.json` shows on the notices page, the institute's home page and the ticker together.
- Hindi text gets `lang="hi"` and the Devanagari fonts in `DESIGN.md`.
- **The ticker:** pages carry each notice's date and last date. `site.js` hides ticker items older than 30 days or past their last date, so expired notices drop off with no nightly push. (This is the builder's choice; anything that avoids a daily push is fine.)

**The check:** before writing pages, `build.py` refuses to build if:
- a field is missing or a date is wrong;
- a photo or file it points to doesn't exist;
- a photo has no alt text, is far too large, or still carries location data;
- a local link would break.

It prints what's wrong in plain words, so Raion can fix it. These are checks that the content is correct and complete, not decisions about what may be posted. Those stay with Raion.

**`python build.py photo <file> <name> --for hero|split|news|reel|portrait|gallery`** prepares a photo the `DESIGN.md` way:
- resizes it;
- strips location and other photo data;
- saves it as JPEG quality about 80;
- names it `<institute>-<what>.jpg` in `assets/photos/`.

**The first build is mine, before Raion takes over:**
1. Move today's four pages into `content/` and `templates/`. The output must look the same as today.
2. Add the `DESIGN.md` page templates.
3. Fill pages with real content where it exists:
   - the documents in `Website files/` (WebisteMNDC2025.docx, Website Messages.docx, Alumni 2022.docx, the NIRF files, MNDC Chronicle);
   - Raion's institute fact files (`memory/global/MNDC/` and friends), read only.
4. Leave pages with no real content out of the menu, as `DESIGN.md` says.

### 2. On the Raion PC

- **`setup.ps1`** clones `MNDC-Main-Website` as a `website` folder next to `memory` and `site`. It uses its own deploy key, `raion_website_ed25519`, the same way `site` uses `raion_site_ed25519`.
- **Maharshi's one manual step:** add the printed public key on GitHub, under MNDC-Main-Website, then Settings, then Deploy keys, with write access ticked.
- **Already there:** Python 3.12 and Pillow are in Raion's `.venv`.

### 3. In Raion: instructions, not new tools

- **New file `WEBSITE.md` at the root of Raion's code repo, next to `wa_agent.py`.**
  - It ships with the code: the watchdog pulls it like any update, and it is checked by `check_tools.py` before it is kept.
  - It is not in `memory/`, so Raion's memory is never edited or copied over.
  - Raion's code lives on GitHub; `memory/` stays only on the PC.

  It holds:
  - **The two sites:**
    - official site: notices, news, people, messages, gallery, pages;
    - mndc.maharshinahar.in: apps and dashboards.
    - There are examples both ways. Raion decides, and asks when unsure.
  - **Who:** admins approve; a staff member's request goes to an admin.
  - **The steps:**
    1. `git pull` in `website/`.
    2. Edit `content/`, using `build.py photo` for photos.
    3. Run `python build.py` and fix anything it reports.
    4. Show the admin the change.
    5. After their yes, commit and push. The commit message names the change and the approving admin. If the push is refused, because someone else pushed in between, pull and try once more. If it still fails, tell the admin.
    6. About 30 seconds later, fetch the live page as text and confirm the change is there.
    7. Send the link.
  - **The preview is text first:**
    - exactly what will appear: title, date, category, language, the file, which institute, and where it shows (notices page, ticker, home page);
    - photos sent as they are.
    - **No page screenshots for normal changes.** At most one screenshot, only for a brand-new page or a new kind of section, and never one per revision. Raion trusts `build.py` and the templates for the layout.
  - **Photo judgement:** use the photo cards Raion already makes to skip posters, flyers, cuttings, screenshots, watermarks and GPS stamps. Don't open every photo again.
  - **Writing:** follow `DESIGN.md` "Words": plain facts, the dates and phone formats, the language rule.
  - **Never public:** the anti-goal list above.
  - **Undo:** "woh notice hata do" removes it from `content/`, rebuilds and pushes after a yes. `git revert` is there for anything bigger.
  - **Spotting:** what is worth offering:
    - a notice Raion just made on letterhead;
    - a new university notice from a watched site;
    - a batch of event photos, which become a news story plus a gallery album with the best few photos;
    - a newspaper cutting about the institute;
    - an admission, exam or holiday date in the shared timeline.
  - **How to offer:**
    - something from the admin's own chat: ask right there ("Isse MNDC website ke notices mein bhi daal doon?");
    - things seen elsewhere: Raion chooses which admin to ask from what it knows (who handles that institute, event or matter, and who was involved). It also chooses when: at a natural moment in their next chat, a few at once, or sooner if a deadline is near.
    - Remember what was posted or skipped, so nothing is asked twice. Raion keeps that list in a file it chooses under memory.
- **`wa_agent.py` prompt:** a short "Official website (admins only)" section next to "Web apps". It:
  - points to `WEBSITE.md`;
  - says which site is which;
  - says staff requests go to an admin;
  - says to offer website-worthy things.

  Add one line to the "Web apps" section too: official content (notices, news, people) belongs on the official site, not mndc.maharshinahar.in. No new tool is needed: admins' sessions can already read, write and run commands on the computer.

## How it reaches the Raion PC

1. **Website repo:** I push the page maker, templates, content and new pages to `MNDC-Main-Website`. Hostinger deploys it as now. This needs nothing on the Raion PC.
2. **Raion's code repo:** I push `WEBSITE.md`, the prompt lines in `wa_agent.py` and the `setup.ps1` step. The office PC picks them up by itself within about 10 minutes, when no chat or call is on. If the self-check fails, it keeps the old code.
3. **One time on the Raion PC (Maharshi, about 2 minutes on AnyDesk):**
   1. Run `setup.ps1`. It makes the website key and prints it.
   2. Add the key on GitHub: MNDC-Main-Website, then Settings, then Deploy keys, with write access ticked.
   3. Run `setup.ps1` again. It clones the website into the `website` folder.

   From then on, Raion can publish.

## Done means

- `build.py` rebuilds the current site with no visible change.
- It refuses a photo with no alt text, a photo with location data, and a bad date.
- One notice added to `notices.json` shows on the notices page, the home page and the ticker, and leaves the ticker after its last date.
- On the Raion PC, in an admin's chat, each of these goes live within a minute, previewed in text with no page screenshot:
  - a notice from a PDF;
  - a notice in Hindi;
  - a faculty photo swap;
  - a news story with photos;
  - removing a notice.
- A staff member's "website pe daal do" reaches an admin and doesn't go live by itself.
- "Fees ka dashboard bana do" still goes to mndc.maharshinahar.in.
- After Raion makes a letterhead notice for an admin, it offers to put it on the site.

## Left to the builder

- Exact field names in the content files, and slug rules.
- How templates are filled (`string.Template` or plain f-strings), and whether a `sitemap.xml` is worth making.
- How large notice PDFs are handled (link as they are, or shrink them).
- Where Raion keeps its list of website ideas, and how it remembers what was posted or skipped.
- Whether `check_tools.py` should also run `build.py` on a copy before Raion updates itself.
