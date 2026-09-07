# Sample Takeout export (synthetic)

These CSVs are **hand-written test fixtures**, not a real Google Takeout export.
They reproduce the three schemas Takeout is known to emit — a library export
(`Title/Album/Artist`), a YouTube Music playlist (`Song Title/Artist Names/Video ID`),
and a plain YouTube playlist (`Video ID/Video Title`) — plus the awkward cases the
matching has to survive: `Artist - Topic` channel names, `(Official Music Video)` /
`(2011 Remaster)` title noise, accented and non-Latin titles, a live version, and a
deliberately unmatchable track.

The video IDs are placeholders and are not resolved anywhere in the pipeline.
Point `TAKEOUT_PATH` at your own export to get numbers that mean something.
