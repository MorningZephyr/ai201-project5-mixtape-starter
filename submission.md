# Project 5: Mixtape Bug Hunt — Submission

Branch: `bugfix/mixtape`

All five open issues were fixed, documented, and committed as separate commits.
A regression test was added for Issue #4.

---

## AI Usage

I used an AI assistant (Claude) primarily as a **navigation and comprehension aid**, not as a bug finder. Concretely:

- **Codebase orientation.** I had the AI summarize each `services/` file's responsibility and confirm the route→service call chain (e.g. `POST /songs/<id>/rate` → `routes/songs.py:rate` → `notification_service.rate_song`). I verified each claim by reading the file myself before trusting it.
- **Standard-library semantics.** I asked the AI to confirm what Python's `datetime.weekday()` returns (Monday=0 … Sunday=6) so I could reason about the streak comparison. I then verified this directly against the existing `test_streak_increments_on_sunday` test, which encodes `weekday() == 6` for Sunday.
- **Comparing two similar code paths.** For Issue #4 I asked the AI to diff `add_to_playlist` against `rate_song` structurally. It correctly pointed out that `add_to_playlist` ends with a `create_notification(...)` call and `rate_song` has no such call. I confirmed this by reading both functions top-to-bottom.

**Where I had to verify / override the AI:** For Issue #3 the AI initially assumed the `outerjoin` on `song_tags` would obviously produce duplicate rows in the result. When I actually ran `search_songs` under the installed SQLAlchemy (2.0.40), the legacy `Query.all()` **auto-deduplicates single-entity ORM results**, so the duplicates did not surface through that exact call path in this environment. The underlying defect is still real — the query fans out one row per tag via the join and relies on implicit ORM uniquing to hide it — so I fixed it explicitly with `.distinct()` rather than depending on version-specific behavior. This is a good example of the AI being *plausible but not verified*: the root-cause shape was right, but its confidence about the observable symptom was wrong until I ran the code.

---

## Codebase Map

Written before starting any bug work.

### Main files and their roles

- **`app.py`** — Flask application factory (`create_app`) and the shared `SQLAlchemy` instance (`db`). Registers four blueprints under URL prefixes: `/songs`, `/playlists`, `/users`, `/feed`. Also runs `db.create_all()` inside the app context. The DB defaults to `sqlite:///mixtape.db`.
- **`models.py`** — SQLAlchemy models and association tables:
  - Models: `User`, `Tag`, `Song`, `ListeningEvent`, `Rating`, `Playlist`, `Notification`.
  - Association tables: `friendships` (self-referential many-to-many on `User`), `song_tags` (Song↔Tag), and **`playlist_entries`** — the Playlist↔Song join table, which carries extra columns: `position` (explicit ordering, not insertion order), `added_by`, and `added_at`.
  - Notable: a `Rating` is its own table with a `UniqueConstraint(user_id, song_id)` — a user can rate a song at most once. There is no separate "listen" streak table; the streak lives directly on `User.listening_streak` / `User.last_listened_at`.
- **`routes/`** — Thin HTTP layer. Each route parses input, calls exactly one service function, and formats the JSON response. Files: `songs.py`, `playlists.py`, `users.py`, `feed.py`.
- **`services/`** — All business logic. One file per feature area: `streak_service.py`, `feed_service.py`, `search_service.py`, `notification_service.py`, `playlist_service.py`. **This is where all five bugs live.**
- **`seed_data.py`** — Drops and recreates the DB, then seeds 5 users, 25 songs (deliberately with 0/1/3-tag songs to expose the search bug), 3 playlists, listening events (some recent, some old), streak state, and one existing playlist-add notification (so you can compare the *working* notification pattern against the missing one).
- **`tests/`** — `pytest` tests for streaks, search, and playlists. Several tests are written to *fail* under the bug and pass under the fix (they encode expected behavior).

### Pattern I noticed

Every route delegates immediately to a service function and does no business logic itself — routes handle input validation and response shaping; services own the data access and rules. Services raise `ValueError` for "not found"/validation problems, and routes translate those into `404`/`400` responses. This clean split is why the README says: "if something is broken in an endpoint, trace it back to the service it calls."

### Data flow — rating a song triggers a notification (Issue #4 feature)

1. Client sends `POST /songs/<song_id>/rate` with JSON `{user_id, score}`.
2. `routes/songs.py:rate` parses `user_id`/`score`, validates presence, and calls `rate_song(user_id, song_id, int(score))`.
3. `services/notification_service.py:rate_song` validates the score range (1–5), loads the `Song` and rating `User`, then upserts a `Rating` (updates the score if one already exists thanks to the unique constraint, otherwise inserts a new one) and commits.
4. **Intended:** it then calls `create_notification(...)` for `song.shared_by` so the original sharer is told their song was rated — mirroring `add_to_playlist`, which notifies the sharer when their song is added to a playlist. (This final step was the missing piece — see Issue #4.)
5. The sharer later reads `GET /users/<id>/notifications` → `get_notifications`, which returns notifications newest-first.

---

## Root Cause Analysis

### Issue #1 — My listening streak keeps resetting

**How I reproduced it.** Ran the existing suite: `test_streak_increments_on_sunday` fails (`assert 1 == 2`). It calls `update_listening_streak` on a Saturday (`weekday() == 5`) then the following Sunday (`weekday() == 6`); the streak resets to 1 instead of incrementing to 2. This exactly matches kenji's report that the reset always happened on a Sunday.

**How I found the root cause.** Traced `GET /users/<id>/streak` → `routes/users.py:streak` → `get_streak` (read-only). Since the report was about the streak being *thrown away when a new day is recorded*, the write path is `POST /songs/<id>/listen` → `record_listening_event` → `update_listening_streak`. Reading that function, the branch on line 73 stood out: `elif days_since_last == 1 and today.weekday() != 6:`. The `weekday() != 6` clause is the only weekday-specific logic in the file, and 6 is Sunday. That was the moment I was confident.

**The root cause.** Python's `datetime.weekday()` returns `6` for Sunday. The increment branch was guarded with `days_since_last == 1 and today.weekday() != 6`, so when a user listened on a Sunday one day after their previous listen, the "consecutive day" branch was skipped and execution fell through to the `else`, which sets `listening_streak = 1`. Sundays were being treated as a forced reset even though the user hadn't skipped a day. (There is no legitimate reason for a streak to care about the weekday at all — a streak is about *consecutive calendar days*.)

**Fix and side-effect check.** Removed the `and today.weekday() != 6` guard so the branch is just `elif days_since_last == 1:`. Side-effects checked: the other streak tests still pass — new user → 1, same-day repeat → no change, consecutive weekday → increment, skipped day → reset. Both sides of the day boundary now behave (Saturday→Sunday increments; skipping a day still resets). Commit isolated to `streak_service.py`.

---

### Issue #2 — Friends Listening Now shows people from yesterday

**How I reproduced it.** Seeded a friend (`darius`) whose only listening event is at 11pm the previous calendar day, and another (`simone`) who listened earlier today, then called `get_friends_listening_now`. With the original code, `darius` still appeared in the morning — matching nova's report. Confirmed via a scripted check that the pre-fix 24-hour rolling cutoff includes last night's 11pm event.

**How I found the root cause.** Traced `GET /feed/<id>/listening-now` → `routes/feed.py:listening_now` → `get_friends_listening_now`. The recency filter is `ListeningEvent.listened_at >= cutoff`, where `cutoff = datetime.now(timezone.utc) - RECENT_THRESHOLD` and `RECENT_THRESHOLD = timedelta(hours=24)`. The 24-hour value is the defect.

**The root cause.** "Listening now" is meant to show friends who listened *today* (this calendar day), but the cutoff was a **rolling 24-hour window** anchored to the current instant. At 9am, a rolling 24h window reaches back to 9am *yesterday*, so an event from 11pm the previous night is still inside the window and keeps showing until 11pm today. The window boundary moved with the clock instead of resetting at midnight, which is exactly the "hangs around until the same time the next day" symptom.

**Fix and side-effect check.** Replaced the rolling threshold with a calendar-day boundary: `cutoff = datetime.combine(now.date(), time.min, tzinfo=timezone.utc)` (start of today, UTC), and removed the now-unused `RECENT_THRESHOLD`/`timedelta` import. Verified with a scripted repro that a friend whose last listen was 11pm yesterday no longer appears while a friend who listened at 1am today does. Checked `get_activity_feed` in the same file — it intentionally is *not* recency-filtered (returns most-recent N regardless of date), so it is unaffected. Commit isolated to `feed_service.py`.

---

### Issue #3 — The same song keeps showing up twice in search

**How I reproduced it.** The search path is `db.session.query(Song).outerjoin(song_tags, ...)`. Because `Crown Heights Anthem` has 3 tags, the outer join produces 3 rows for that one song, which is the mechanism behind simone seeing it three times.

> Environment note (verified, disclosed per the AI-usage guidance): under the installed SQLAlchemy 2.0.40, the legacy `Query.all()` **auto-deduplicates single-entity ORM results**, so the duplicate rows are collapsed before they reach the caller and the symptom does not surface through this exact call in this version. The join is still fanning out one row per tag; the code was only being saved by version-specific implicit uniquing. I fixed it explicitly rather than relying on that.

**How I found the root cause.** Traced `GET /songs/search?q=...` → `routes/songs.py:search` → `search_songs`. The `.outerjoin(song_tags, Song.id == song_tags.c.song_id)` is the only thing in the query that can multiply rows — and crucially, the join is not used for filtering (the `WHERE` clause only touches `Song.title`/`Song.artist`). A join that widens the result set but contributes nothing to the filter is the classic duplicate-rows-in-search cause.

**The root cause.** The query joins `Song` to the `song_tags` association table but filters only on song fields. Each matching song is emitted once per associated tag row: a 3-tag song yields 3 identical `Song` rows, a 1-tag song yields 1, a 0-tag song yields 1 (outer join). The join exists at all only because tags are displayed in the results — but tags are already loaded lazily by `Song.to_dict()` (`tags` relationship is `lazy="subquery"`), so the join is unnecessary *and* harmful.

**Fix and side-effect check.** Added `.distinct()` to the query so each `Song` row is returned once regardless of tag count. (I kept the join minimal-change rather than restructuring; `.distinct()` on the single-entity select collapses the fan-out at the SQL level, which is robust across SQLAlchemy versions and doesn't depend on ORM uniquing.) Verified all `test_search.py` cases pass: 0-tag, 1-tag, and 3-tag songs each appear exactly once, matching search still returns the right song, and a no-match query returns `[]`. Commit isolated to `search_service.py`.

---

### Issue #4 — Notified on playlist-add but not on rating

**How I reproduced it.** Called `rate_song(rater_id, song_id, 5)` for a song shared by a different user, then `get_notifications(sharer_id)` — the list was empty, even though the `Rating` row was created. This matches aaliya's report: the rating saves and shows on the song, but no notification is ever created. (Codified as the new regression test `tests/test_notifications.py::test_rating_a_song_notifies_the_sharer`.)

**How I found the root cause.** The hint said the cause is architectural, not a typo — so I diffed the two sibling functions in `notification_service.py`. `add_to_playlist` ends with an `if song.shared_by != added_by_user_id: create_notification(...)` block. `rate_song` performs the upsert and `db.session.commit()`, then simply `return rating` — there is **no** `create_notification` call anywhere in it. Reading both top-to-bottom made it certain: the notification step was never implemented for ratings.

**The root cause.** `rate_song` persists the `Rating` but omits the notification side-effect entirely. The two "friend interacts with your song" events (added-to-playlist and rated) were supposed to follow the same pattern — create a notification for `song.shared_by` — but only the playlist path had it. So ratings were saved without ever producing a `Notification` record.

**Fix and side-effect check.** After the commit in `rate_song`, added a notification mirroring `add_to_playlist`: if `song.shared_by != user_id`, call `create_notification(user_id=song.shared_by, notification_type="song_rated", body=f"{rater.username} rated your song '{song.title}' {score} stars.")`. Side-effects checked: rating your *own* song must not self-notify (the `!= user_id` guard — covered by `test_rating_own_song_does_not_notify`); the rating upsert path is unchanged so re-rating still updates the score; `add_to_playlist` and `get_notifications` are untouched. Commit covers `notification_service.py` plus the new test file.

---

### Issue #5 — The last song in a playlist never shows up

**How I reproduced it.** `test_playlist_returns_all_songs` fails: a playlist seeded with 5 songs returns only 4, and `test_playlist_returns_songs_in_order` shows the missing one is always the last by position. This matches darius's report that the most recently added (highest-position) song is the one hidden.

**How I found the root cause.** Traced `GET /playlists/<id>/songs` → `routes/playlists.py:get_songs` → `get_playlist_songs`. The query correctly orders by `playlist_entries.c.position` ascending and fetches all rows into `songs`, but the return statement is `return [song.to_dict() for song in songs[:-1]]`. The `[:-1]` slice drops the final element.

**The root cause.** `songs[:-1]` returns every element *except the last*. Since the query is ordered ascending by `position`, the last element is always the highest-position (most recently added) song — so exactly one song, the newest, is always omitted. Adding another song pushes the previously-last song into the "kept" range and makes the brand-new one the dropped last element, which is precisely the "adding a song frees the previous one and hides the new one" behavior in the report. The function's own docstring says "returns all songs," so the slice directly contradicts intent.

**Fix and side-effect check.** Changed `songs[:-1]` to `songs` so every ordered song is returned. Side-effects checked: all `test_playlists.py` tests pass — full count (5), correct ascending order, and an empty playlist still returns `[]` (note `[:-1]` on an empty list is also `[]`, so the empty case was never broken and remains correct). Commit isolated to `playlist_service.py`.

---

## Regression Test

`tests/test_notifications.py` was added for Issue #4. `test_rating_a_song_notifies_the_sharer` fails against the pre-fix code (0 notifications) and passes after the fix (1 `song_rated` notification for the sharer). `test_rating_own_song_does_not_notify` guards the self-notification edge case. The existing `test_streaks.py`, `test_search.py`, and `test_playlists.py` files already contained tests that fail under Issues #1, #3, and #5 and pass after the fixes.

Full suite after all fixes: **15 passed.**

---

## Commit History

See `git log --oneline` on `bugfix/mixtape` — one commit per fix, conventional-commit format. (Screenshot below.)

<!-- Paste screenshot of `git log --oneline` here -->
![alt text](image.png)