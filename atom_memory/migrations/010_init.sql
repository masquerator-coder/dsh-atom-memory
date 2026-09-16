-- The user profile becomes a table the *user* owns.
--
-- It used to be a projection: `user_profile` was re-derived from active facts
-- on every read (list_profile / user_md), so the table was a cache of a view
-- and the panel could only edit rows that the next read would overwrite. That
-- model had two problems the panel could not work around:
--
--   * deletion was impossible — deleting a row only removed the cache entry,
--     and the still-active fact re-derived it on the very next read;
--   * nothing was ever removed — the projection only ever upserted, so a fact
--     that was later retracted left its profile row behind for good.
--
-- The profile is now an independent, persistent table: entries enter it only
-- when the user accepts a generated suggestion or types one, and leave it only
-- when the user deletes one. Nothing derives it from facts any more.
--
-- `pinned` is dropped rather than kept: it froze a row against automatic
-- writes, and there are no automatic writes left to freeze against. The guards
-- that implemented it (profile.py's pinned check, worker.py's pinned-section
-- archive protection) were removed in the same change.
--
-- Existing rows are cleared: they were projection output, and the user's
-- stored intent was only ever "these are what the facts implied", not a
-- curated list. The facts themselves are untouched, so the first
-- "生成画像" run can offer them back for the user to keep or drop.
DELETE FROM user_profile;

ALTER TABLE user_profile DROP COLUMN pinned;
