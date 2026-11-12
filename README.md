# CF Minutes

A Windows app that hears **both sides of a meeting** (you, and everyone else, from Teams or any other app), keeps a
**transcript with names**, and writes **short minutes** (summary, decisions, action items with owners, open
questions). It works in **English, Telugu, and Telugu and English mixed**. Everything is stored **only on the laptop it runs on**.

It is a separate app from CloudFuze Assist (the live call suggestions). Different code, different repository, nothing shared at run time.

## Usage

### Run it after you pull the code

```
git clone https://github.com/surattuabhinav2003/ASSIT-.git
cd ASSIT-
run.bat
```

`run.bat` does the rest. The first time it runs `setup.bat` by itself (finds Python 3.11 or newer, makes a private
environment in `.venv`, installs the packages; a few minutes, once), then opens the app. After a `git pull` just run `run.bat` again.
If Python is missing, `setup.bat` says how to install it (`winget install Python.Python.3.11`).

Then open **Settings** and paste the OpenAI key once. It is kept encrypted for your Windows account, never in a file you can read and never in this repository.

For other people's laptops, `installer\Install.bat` does the same plus the Desktop and Start menu shortcuts and the key prompt, and the app then keeps itself up to date from this repository.

### Find your way around

- **Home**: record a meeting (choose the language first), the action items from your latest meetings, your recent meetings.
- **Meetings**: every meeting, searchable by title, person or words in the minutes.
- **Settings**: language, name, automatic start in Teams, how long audio is kept, the key, **Check audio**.
- Every page that is not one of these three has a **Back** button that says where it goes ("Back to Home", "Back to Meetings").
- While recording, a red bar stays at the top of every page with **Open live view** and **Stop**. You can leave the live page; the recording goes on.

### Record

- **Teams:** nothing to do. When a Teams meeting starts, CF Minutes starts recording (a notification and a red dot on the
  tray icon show it). When the meeting ends it stops and writes the minutes. Turn this off in *Settings*.
- **Zoom, Google Meet, a browser, a phone call on speaker, anything else:** press **Record a meeting**, then **Stop** when done.
  When another app uses the microphone, CF Minutes also asks "Is this a meeting you want to record?".
- **Language:** *English*, *Telugu*, or *Telugu and English mixed* (people switch between the two). Pick it on Home before
  you record; Teams meetings that start by themselves use the one set in Settings. For Telugu and mixed meetings:
  - Telugu words are written in Telugu script, English words and names in English letters ("ఈ week లో pilot start చేద్దాం").
  - The minutes are written in **English** by default (Settings can change that to the language of the meeting).
  - Telugu is recognised less accurately than English: look through a Telugu transcript before relying on it.
- Tell everyone on the call that you are recording.

### After the meeting

The meeting opens by itself in about a minute:

- **Minutes** (short): summary, decisions, action items with owners and due days, open questions. *Edit* them,
  *Copy* them, save as **Word** or **Markdown**, or *Write again*.
- **Transcript**: every line with time and name. Search it, filter by person, **click a time to hear it**. Click a name
  to rename that person everywhere in the meeting.
- **Playback**: you and the others play together, on the same clock, at the bottom of the page.

### How people get their names

1. **Teams roster:** the participant names Teams shows on screen (read through Windows accessibility; nothing is installed into Teams).
2. **Separate voices:** the other side's audio is analysed with speaker separation, so each voice stays one person for the whole meeting.
3. **What is said:** "I'm Priya", "Thanks, Dave".
4. A voice with no evidence is shown as **Speaker 2**. It is never guessed. Rename it once and it is fixed.

### Your data

Meetings are kept in `%LOCALAPPDATA%\CFMinutes\meetings\<date-time>\` (transcript, minutes, and the two audio tracks).
*Settings* shows the folder, can delete audio after N days (the transcript and minutes stay) or right after the minutes are written.
Each meeting has *Delete audio* and *Delete meeting*. The audio and text are sent to OpenAI only to be transcribed and summarised.

## For the developer

```
run.bat / setup.bat          start it / build .venv from requirements.txt (both safe to run again)

minutes/capture.py           both sides: loopback + microphone, voice detection, two WAV tracks on one clock
minutes/stream.py            live transcription (copied from CloudFuze Assist)
minutes/names.py             Teams roster and who-is-speaking, read through UI Automation
minutes/finish.py            after the meeting: speakers, Telugu/English text, name matching, clean-up, minutes
minutes/session.py           one recording; minutes/store.py the files on disk; minutes/app.py window, tray, watcher
ui/index.html                the window (Home, Meetings, Settings, live, one meeting)
knowledge/vocab.json         CloudFuze product names and speech corrections (exported from CloudFuze Assist)
tools/dump_teams.py          during a Teams meeting: writes what Teams exposes, to tune names.py
installer/                   Install.bat / Install-CFMinutes.ps1
```

Models: live words `gpt-live-transcribe` (English) or `gpt-4o-transcribe` (Telugu, mixed); speakers `gpt-4o-transcribe-diarize`
(who spoke when; for Telugu only the timing is used, the text comes from `gpt-4o-transcribe`); names, clean-up and minutes `gpt-6-sol`.
Push to `main`: laptops pull it within a few hours (and restart when no meeting is being recorded).
The OpenAI key is never in this repository.
