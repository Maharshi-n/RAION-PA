# Setting up Raion on this PC (for Claude Code on this PC)

Raion is the WhatsApp office operator of Maa Narmada Group of Institutes, Dhamnod. This folder is its code (from
GitHub, Maharshi-n/RAION-PA). It runs here day and night: the Python server (app.py), an ngrok tunnel that brings
WhatsApp messages in, and Claude Code (this login) as its brain. Maharshi built it and is away in Pune.
You set it up once, then the watchdog keeps it running and pulls Maharshi's code updates by itself.

## Rules while you work here
- Never print, open or change .env (it holds the WhatsApp, Sarvam and other secrets). Check only that it exists.
- Don't edit the code here: updates come from GitHub every 10 minutes and replace local changes. Tell Maharshi
  what needs changing instead.
- memory\ is Raion's knowledge and everyone's chats: copy it, never rewrite or delete things in it.
- Ask the user before changing any Windows setting. The user types passwords and tokens themselves; never ask them
  to paste one into this chat.

## Steps
1. **Pen drive.** Ask the user for the pen drive letter. Copy its `RAION\memory` folder and `RAION\.env` file into
   this folder (next to app.py). Check the copy: `memory\global\INDEX.md` exists and the folder sizes match. Then
   remind the user to delete RAION\ from the pen drive (it has student data and secrets).
2. **Setup script.** Run `powershell -ExecutionPolicy Bypass -File setup.ps1` (without -Start: nothing runs yet).
   - It installs Git, Python 3.12, FFmpeg, LibreOffice and Node.js if they are missing, makes .venv, clones the
     website code into site\ and runs check_tools.py (must say ok).
   - Installs may pop up Windows' "Do you want to allow this app to make changes?": tell the user to click Yes.
   - If it prints a key to add on GitHub (raion-custom-dashboards, with write access), show it to the user, wait until
     they say it's added, then run setup.ps1 again. If it asks to reopen PowerShell or run as Administrator, do that.
3. **Claude login.** `claude auth status` must show loggedIn true with the paid plan account. If not, the user runs
   `claude auth login` themselves.
4. **ngrok.** ngrok must be logged in to the same ngrok account as Maharshi's laptop, so the address stays
   https://strewn-require-worrisome.ngrok-free.dev (WhatsApp sends messages there). `ngrok config check` shows if a
   token is set. If it isn't, or it's another account, the user gets the authtoken from dashboard.ngrok.com (same
   login as Maharshi) and runs `ngrok config add-authtoken <token>` themselves. watchdog.ps1 finds ngrok.exe on
   PATH or under D:\ngrok, C:\ngrok.
5. **Windows, with the user's yes.**
   - Never sleep: `powercfg /change standby-timeout-ac 0` and `powercfg /change hibernate-timeout-ac 0`.
   - After a power cut the PC must log in by itself, or Raion won't start. That is the user's own step (it stores
     the Windows password): explain simply, they press Win+R, type `netplwiz`, untick "Users must enter a user name
     and password", and enter the password.
   - Optional: Chrome Remote Desktop (remotedesktop.google.com/access), installed and signed in by the user, so
     Maharshi can reach this PC from Pune.
6. **Switch day only.** Start Raion only after Maharshi says his laptop's Raion and ngrok are stopped (two Raions at
   once would send every reminder twice).
   - If memory\ was copied on an earlier day, copy RAION\memory from the pen drive again now (a fresh copy from the
     stopped laptop): replace the old memory\ folder.
   - Run `powershell -ExecutionPolicy Bypass -File setup.ps1 -Start`. It starts Raion and adds the auto-start task
     "Raion" (it starts at every login and restarts if it stops). If it says to run as Administrator, do that.
   Check:
   - logs\watchdog.log says "ngrok started" and "server started";
   - `curl.exe -s -o NUL -w "%{http_code}" http://127.0.0.1:8000/` gives 200;
    - `curl.exe -s http://127.0.0.1:4040/api/tunnels` shows strewn-require-worrisome.ngrok-free.dev;
   - the user sends Raion a WhatsApp message and gets a reply.

## Everyday
- Logs: logs\watchdog.log (starts, restarts, updates), logs\server.log and server.err.log (the server).
- Restart Raion: `Stop-ScheduledTask -TaskName Raion; Start-ScheduledTask -TaskName Raion`.
- Updates: every 10 minutes the watchdog pulls new code from GitHub when no chat is mid-reply, runs check_tools.py,
  and restarts Raion. If the check fails it keeps the old code and writes "FAILED" in watchdog.log.
- Raion stops replying and server.err.log says Claude is not logged in: the user runs `claude auth login`.
- 1:00-2:00 AM is the maintenance hour: chats get a short "maintenance" reply, nothing runs, code updates wait, and
  Raion backs up memory\ and tidies it. memory\CHANGELOG.md lists what each night did.
- memory\MIGRATION_PLAN.md: the plan of the one-time memory move (written the first night after the update). Nothing
  changes until Maharshi approves it from his laptop.
- Tally: admins' Raion reads it (never writes) through RaionGate on the accountant's PC. RaionGate\ is for that PC
  only: there, setup.bat (double-click, Yes) installs it and shows a code for Raion; remove.bat undoes it. Nothing of
  it runs on this PC. Raion keeps the code and the gate's address in .runtime\tally.json.

## The office PCs' files (filewatch.py)
Raion reads the operators' work folders over the LAN and keeps copies of the documents in pcfiles\ (never in git,
never in memory\). Nothing is installed on the other PCs: their folders are only shared, read-only.

On each operator PC, once (Command Prompt as administrator; replace the password, the Windows user name and the drive):
1. A read-only account only Raion uses, hidden from the sign-in screen:
   `net user raionread THE-PASSWORD /add /passwordchg:no /expires:never`
   `reg add "HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon\SpecialAccounts\UserList" /v raionread /t REG_DWORD /d 0 /f`
2. Share the work folders, read-only, to that account only (a whole data drive is fine; never all of C:):
   `net share Work=D:\ /grant:raionread,READ`
   `net share Desktop="C:\Users\NAME\Desktop" /grant:raionread,READ` (the same for Documents and Downloads)
   `icacls "C:\Users\NAME\Desktop" /grant raionread:(OI)(CI)RX` (folders inside C:\Users need this too)
3. Let the PC be seen on the LAN: the network set to Private (Home/Work on Windows 7), and
   `netsh advfirewall firewall set rule group="File and Printer Sharing" new enable=Yes`

Then, in the admin's WhatsApp chat with Raion:
1. "probe \\192.168.1.50\Work with user raionread, password ...": Raion answers with the file counts and how long
   one listing took. Past 20 seconds the reading goes easy on that PC by itself (see SLOW_SECS in filewatch.py).
2. "add it as PC2 with these folders": from then on it is read every 15 minutes, 8:00-20:00. The first reading
   copies every document, 10 minutes of copying per run until it is through.
3. Raion's pc_files tool: find, read, changes, pcs. The account's password stays in .runtime\pcfiles.json.

Check without Raion: `.venv\Scripts\python.exe -X utf8 filewatch.py probe \\192.168.1.50\Work raionread THE-PASSWORD`,
and `filewatch.py test` (ends with `ok`). Kept as copies: documents up to 20 MB saved in the year before a PC was
first read, and everything after (plus one earlier copy of a file that changes). Older and bigger ones are only
listed, and fetched from their PC when asked (COPY_MAX, RECENT_DAYS in filewatch.py). pcfiles\ grows with the documents: put it on a drive with room by setting
PC_FILES_DIR in .env before the first PC is added.
