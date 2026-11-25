# The Long Salvage – Battle Log

Necromunda campaign log for The Long Salvage. Sign in with an allowed Google account at https://satchymoto.github.io/Necromunda-Long-Salvage-Log/

## What's here

- `index.html`, `version.txt`: the live log as GitHub Pages serves it (built, don't edit by hand). Open copies show a "new version available" bar when `version.txt` changes.
- `src/long-salvage-log.html`: **the source**. All the log's markup, styles and script are in this one file.
- `src/images/`: the map and gang logos the page uses.
- `data/equipment.json`: the Equipment Lists & Trading Post database, in a readable form (a copy of it is built into the page).
- `tools/`: build scripts (below).
- `tests/`: browser tests (below).
- `firestore.rules.example`: the shape of the database security rules. The live list of players is kept in Firebase.

## Building

```
python3 tools/build_hosted.py              # the live site: writes index.html + version.txt here, then commit and push
python3 tools/build_claude_copy.py         # the Claude copy: build/claude-copy/ (index.html + images) to publish as the artifact
python3 tools/build_laptop.py backup.json  # a laptop copy that runs from a file, starting from a backup: build/
```

Equipment data: edit `data/equipment.json`, run `python3 tools/equipment.py apply` to put it into the page, then build.
`python3 tools/equipment.py check` says whether the two match.

## Testing

```
pip install playwright && playwright install chromium
python3 tests/run_tests.py backup.json                 # everything
python3 tests/run_tests.py backup.json two-editors     # one test
```

`backup.json` is any file from the log's Backup button; it is only read. The tests build both versions, open every tab,
battle and post-cycle at desktop and phone width, and use a stand-in database (`tests/fbmock.js`) to check saving: typing
while a save is under way, and two people editing the same record at once. Keep backups and `build/` out of the repository:
they hold campaign data.
