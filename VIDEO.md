# Video editing: photos and clips into a finished video; reels, event videos, cutting, joining, music

You can't watch a video. You get numbers (activity, beats) and still frames (sheets). Never place a cut without
looking at the frames around it.

This file and its helper come with Raion's code: they are written over at every start, so don't edit them. What an
admin wants done differently (their taste, a correction) goes in global/VIDEO_STYLE.md: read it before every video.

## The helper: global/tools/video.py (command line in your instructions)
- `probe FILE...`  duration, size as displayed, fps, rotation, audio
- `activity FILE [--step 0.5]`  motion and loudness over time: where the action and the sound start and end,
  quiet gaps, scene changes, strong hits (claps, beats)
- `sheet FILE... [--from A --to B --every S | --at T1,T2] [--out work/sheets/x.jpg]`  frames with their timestamps
  in one image; then Read it. Several files and no times: one frame of each file (to see what every clip is).
  Photos work too.
- `sync A B`  how much later the same sound happens in B than in A (same event filmed on two phones; a clean
  song file vs the room recording)
- `beats SONG`  the song's tempo and the time of every beat
- `render EDL.json OUT.mp4 [--preview]`  builds the video from the edit list. --preview: smaller, always under
  WhatsApp's 16 MB video limit. One render runs at a time on this computer: a second one waits its turn.

## They send photos and clips and want a video: make it, don't interview them
A finished video in one go is the job. Ask at most one thing, and only when the files can't tell you (what the
event was, when). No song from them: make it with the clips' own sound or silent, and say in one line that a song
can be added if they send one. Defaults: Instagram reel, 9:16, 20-45 s, order of the event.

1. **See everything.** probe the clips; one sheet of all clips and photos. Leave out the weak ones: blurred, dark,
   near-copies of a better one, tiny (render lists `small_photos`), backs of heads, a thumb on the lens.
2. **Tell a small story.** Opening (the best, most striking shot, with the title on it) → the event in order
   (arrival, lamp, guests, the main thing, faces, prizes) → the closing (a group photo or a happy moment) → the end
   card. Don't put two near-alike shots together; change between wide, medium and close.
3. **Pace.** A photo 2-3 s (a group photo with many faces, or the last one, 3-4 s). A clip only its best 2-5 s,
   unless its sound matters (a speech line, a performance). With a song: `"snap": true` puts every change of clip
   on the beat. Faster for sports, dance, a celebration; slower for a farewell, a tribute, a prayer.
4. **Transitions: few and quiet.** One main one for the whole video, usually `fade` (0.4-0.5 s). `cut` inside a
   run of the same moment and for anything on a strong beat. One or two special ones at a change of chapter
   (`slideleft`, `circleopen`, `fadeblack`, `zoomin`). Many different transitions is what makes a video look
   home-made.
5. **Words on screen.** Title over the opening shot (event, institute, date), names under a guest or a winner for
   3-4 s, an end card (institute name, logo, place, phone). Make each with make_image (see 'Text, titles, logo').
   The institute's logo in a top corner for the whole video.
6. **Look.** `"look": "clean"` for phone footage, `warm` for festivals and stage light, `film` or `bw` for a
   tribute or memories. One look for the whole video. Cards and anything already designed: `"look": "none"` on
   that clip.
7. **Finish.** `"fade_in": 0.4`, `"fade_out": 0.8`, the song with `"fade_in": 0.5`.
8. Render the preview, check it (below), send it with one line: what it is and the length.

## Clips: find the real in/out
Run activity, then look: `sheet --from T-3 --to T+3 --every 0.5` around each candidate (0.1 steps if the edge still
isn't clear). A clip starts where its content starts (the performance, the speech, the play, the moment) and not at
the walking in, setting up, waiting, camera adjusting. It ends just after the content ends (a short tail: bow,
applause, a smile). Motion and sound often start at different times: the picture decides.

## Sound
- Clips whose own sound matters (speech, performance, live moment): `"audio": "clips"` (default).
- A montage of photos and unrelated clips: a song they send or name, with `"snap": true`. `clips_volume` 0.1-0.3
  keeps some of the room (applause, cheering) under the song.
- One song running through several clips of the same performance (different phones, or a clean copy of the song):
  sync each recording to the song, place every clip on the song's timeline so the picture stays in time with the
  music, and use `"audio": {"src": "song", "start": ...}` **without snap** (snap moves the cuts and breaks the sync).
  Check a sync with a sheet on a clear moment (a clap, a hit, a jump).
- Famous film songs can get muted on Instagram: say so once when it matters.
- The loudness is evened out by render: every video goes out at the same level.

## Text, titles, logo (overlays and cards)
- **Over the picture** (title, a name, a caption, the logo): an HTML page exactly the video's size (9:16 is
  1080 x 1920) with no page background, only the text or logo where it should sit; make_image with that width and
  height and `"transparent": true`; then list it in `overlays` with its time on screen. Keep clear of the top 250 px
  and the bottom 350 px of a reel (Instagram's own buttons cover them) and 60 px from the sides. Text needs
  something behind it to be read on any picture: a soft dark strip, a coloured band or a shadow. Big: nothing
  under 40 px.
- **A full card** (opening card, end card, a quote, a chapter heading): the same page with its background, made
  without transparent, used as a photo clip (`"move": "none"` or a slow `"in"`).
- Same fonts, colours and logo as the institute's posters. Look at the made image before using it.

## Check before sending
`render ... --preview` returns `cuts_at` and `check_at`: run `sheet OUT --at <check_at>` and look at the first
frame, both sides of every change of clip, the words on screen (readable, not over a face, right time) and the end.
Fix the edit list and render again (only the changed clips are rendered again) until it's right, a few rounds at
most. After their ok: render without --preview to outbox/<name>.mp4 (full quality). WhatsApp takes videos up to
16 MB: if the final is bigger, say it's ready on the computer at full quality and send the preview-quality one if
they need it on the phone.

## Edit list (work/<name>.edl.json)
```json
{
  "format": "9:16",
  "transition": "fade",
  "look": "clean",
  "clips": [
    {"src": "files/2026-10-24/photo1.jpg", "dur": 3, "move": "in"},
    {"src": "files/2026-10-24/clip1.mp4", "in": 4.2, "out": 8.0, "volume": 1.0},
    {"src": "files/2026-10-24/photo2.jpg", "transition": "slideleft"},
    {"src": "files/2026-10-24/clip2.mp4", "in": 0.8, "out": 3.5, "speed": 0.5, "fit": "crop", "focus": 0.4},
    {"src": "work/endcard.png", "dur": 3.5, "move": "none", "look": "none", "transition": "fadeblack"}
  ],
  "overlays": [
    {"src": "work/title.png", "from": 0.4, "to": 3.5, "move": "up"},
    {"src": "work/logo.png"}
  ],
  "audio": {"src": "files/2026-10-24/song.mp3", "start": 12.0, "snap": true, "fade_in": 0.5, "clips_volume": 0.15},
  "fade_in": 0.4,
  "fade_out": 0.8
}
```
- format: 9:16 (reels, stories), 16:9 (YouTube, TV), 1:1, 4:5 (feed), source (the first clip's shape)
- A clip is a video (`in`, `out`: seconds in that file, exact to the frame) or a photo (`dur`, default 3 s).
- fit (whole list or one clip): `auto` (default) fills the frame when little is lost, and otherwise shows the
  whole picture on a blurred copy of itself (a wide group photo or a horizontal clip in a reel). `crop` always
  fills (`focus` picks what stays: 0 left, 0.5 centre, 1 right; check with a sheet that the subject is in frame).
  `blur` always shows it whole. `pad` gives black bars.
- move (photos): in, out (slow zoom), left, right, up, down (slow pan), none. Left out: they take turns. Zoom in on
  a face or a moment, zoom out or pan on a group or a place.
- speed (clips): 0.25-4. 0.5 is slow motion (good on a jump, a throw, colours flying, a smile); the sound slows too,
  so use it under a song.
- transition (whole list, or on a clip: how that clip comes in): cut, fade, fadeblack, fadewhite, dissolve,
  slideleft, slideright, slideup, slidedown, smoothleft, smoothright, wipeleft, wiperight, circleopen, circleclose,
  zoomin, radial, pixelize, hblur. `transition_dur` 0.5 s by default. Each one overlaps the two clips, so the video
  gets that much shorter.
- look (whole list or one clip): none, clean, warm, vivid, film, bw
- overlays: `from`/`to` in seconds of the finished video (left out: the whole video), `fade` 0.3 s, `move` up, down,
  left, right slides it into place as it fades in.
- audio: "clips", or {"src", "start": seconds into the song where the video starts (negative = the song comes in
  later), "volume": 1, "clips_volume": 0, "fade_in": 0, "snap": false}. Start a song at its hook, not its slow
  intro: `beats` and `activity` on the song show where it gets going.
- fade_in, fade_out: seconds at the very start and end

## Rules
- Originals are never changed; work in work/, results in outbox/.
- Long renders: give Bash a long timeout (up to 3600000 ms) and send a short progress note on long jobs.
- Phone clips and photos differ in size, rotation and frame rate: render evens all that out, so don't pre-convert.
- Never cut with ffmpeg `-c copy`: it jumps to the nearest keyframe, often a second or more off.
- When an admin corrects a video ("too fast", "no slide", "logo bigger"), fix it and add one line to
  global/VIDEO_STYLE.md so the next video starts from there.
