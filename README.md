# CF Minutes

A Windows app that hears **both sides of a meeting** (you, and everyone else, from Teams or any other app), keeps a
**transcript with names**, and writes **short minutes** (summary, decisions, action items with owners, open
questions). Everything is stored **only on the laptop it runs on**.

It is a separate app from CloudFuze Assist (the live call suggestions). Different code, different repository, nothing shared at run time.

## Usage

### Install (each laptop)

1. Download this repository, or `git clone https://github.com/surattuabhinav2003/ASSIT-.git`.
2. Double-click `installer\Install.bat`. It needs Git and Python 3.11 (it offers to install them), asks once for the
   OpenAI key (kept encrypted for that Windows user, never in a file you can read), and adds a Desktop and Start menu shortcut.
3. Open **CF Minutes** from the Desktop. It waits in the tray (blue icon). It updates itself from this repository.

### Record

- **Teams:** nothing to do. When a Teams meeting starts, CF Minutes starts recording (a notification and a red dot on the
  tray icon show it). When the meeting ends it stops and writes the minutes. Turn this off in *Settings*.
- **Zoom, Google Meet, a browser, a phone call on speaker, anything else:** press **Record a meeting**, then **Stop** when done.
  When another app uses the microphone, CF Minutes also asks "Is this a meeting you want to record?".
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
2. **Separate voices:** the other side's audio is transcribed again with speaker separation, so each voice stays one person for the whole meeting.
3. **What is said:** "I'm Priya", "Thanks, Dave".
4. A voice with no evidence is shown as **Speaker 2**. It is never guessed. Rename it once and it is fixed.

### Your data

Meetings are kept in `%LOCALAPPDATA%\CFMinutes\meetings\<date-time>\` (transcript, minutes, and the two audio tracks).
*Settings* shows the folder and deletes audio after N days (the transcript and minutes stay). Each meeting has
*Delete audio* and *Delete meeting*. The audio and text are sent to OpenAI only to be transcribed and summarised.

## For the developer

```
run.bat                      start it from a clone (needs the .venv)
python -m venv .venv ; .venv\Scripts\pip install -r requirements.txt

minutes/capture.py           both sides: loopback + microphone, voice detection, two WAV tracks on one clock
minutes/stream.py            live transcription (copied from CloudFuze Assist)
minutes/names.py             Teams roster and who-is-speaking, read through UI Automation
minutes/finish.py            after the meeting: diarized transcript, name matching, clean-up, minutes
minutes/session.py           one recording; minutes/store.py the files on disk; minutes/app.py window, tray, watcher
ui/index.html                the window
knowledge/vocab.json         CloudFuze product names and speech corrections (exported from CloudFuze Assist)
tools/dump_teams.py          during a Teams meeting: writes what Teams exposes, to tune names.py
installer/                   Install.bat / Install-CFMinutes.ps1
```

Push to `main`: laptops pull it within a few hours (and restart when no meeting is being recorded).
The OpenAI key is never in this repository.
