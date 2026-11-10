"""On-demand, local pronunciation candidates; never correct source subtitles."""

from __future__ import annotations

from functools import lru_cache
import re


INDEX_VERSION = 1
INDEX_BUILD_LIMIT = 4000
INDEX_CHARACTER_LIMIT = 1_000_000
_HAN = r"[\u3400-\u4dbf\u4e00-\u9fff\U00020000-\U0002fa1f]"
_PARTS = re.compile(f"{_HAN}+|[A-Za-z]+|[^A-Za-z{_HAN[1:-1]}]+")
# Generic Mandarin approximations of isolated letter names, not course terms.
_LETTERS = {
    "a": ("ei",), "b": ("bi",), "c": ("xi",), "d": ("di",),
    "e": ("yi",), "f": ("ai", "fu"), "g": ("ji",), "h": ("ei", "chi"),
    "i": ("ai",), "j": ("jie",), "k": ("kei",), "l": ("ai", "er"),
    "m": ("ai", "mu"), "n": ("en",), "o": ("ou",), "p": ("pi",),
    "q": ("qiu",), "r": ("a",), "s": ("ai", "si"), "t": ("ti",),
    "u": ("you",), "v": ("wei",), "w": ("da", "bu", "liu"),
    "x": ("ai", "ke", "si"), "y": ("wai",), "z": ("zei",),
}


@lru_cache(maxsize=2048)
def pronunciation(text):
    """Return a boundary-delimited key and raw character offsets per syllable."""
    from pypinyin import Style, lazy_pinyin

    tokens, offsets = [], []
    for part in _PARTS.finditer(text):
        value = part.group()
        if re.fullmatch(f"{_HAN}+", value):
            readings = lazy_pinyin(value, style=Style.NORMAL, errors=lambda chars: list(chars))
            for index, reading in enumerate(readings):
                tokens.append(reading if re.fullmatch("[a-z]+", reading) else "|")
                offsets.append((part.start() + index, part.start() + index + 1))
        elif len(value) == 1 and value.lower() in _LETTERS:
            for reading in _LETTERS[value.lower()]:
                tokens.append(reading)
                offsets.append((part.start(), part.end()))
        elif value.isspace():
            continue
        else:
            # Do not join across punctuation, numbers or ordinary English words.
            tokens.append("|")
            offsets.append((part.start(), part.end()))
    return " " + " ".join(tokens) + " ", tuple(tokens), tuple(offsets)


def query_key(term):
    key, tokens, _ = pronunciation(term)
    # Single syllables and unrestricted long/symbolic queries are too ambiguous.
    return key if 2 <= len(tokens) <= 24 and "|" not in tokens and re.search(_HAN, term) else None


def find_matches(text, keys):
    key, tokens, offsets = pronunciation(text)
    matches = []
    for term, needle in keys.items():
        position = key.find(needle)
        size = len(needle.strip().split(" "))
        while position >= 0:
            first = key[:position].count(" ")
            last = first + size - 1
            # A multi-syllable letter name is indivisible (M -> ai mu).
            if ((first == 0 or offsets[first - 1] != offsets[first])
                    and (last + 1 == len(offsets) or offsets[last + 1] != offsets[last])):
                start, end = offsets[first][0], offsets[last][1]
                matches.append({"query_term": term, "source_text": text[start:end],
                                "start_char": start, "end_char": end,
                                "syllables": needle.strip(), "requires_verification": True})
                break
            position = key.find(needle, position + 1)
    return matches


def initialize_phonetic_search(db):
    """Install empty derived tables; old libraries are not converted at startup."""
    db.execute(
        "CREATE TABLE IF NOT EXISTS video_subtitle_phonetics("
        "row_id INTEGER PRIMARY KEY REFERENCES video_transcript_segments(row_id) ON DELETE CASCADE,"
        "pronunciation TEXT NOT NULL, version INTEGER NOT NULL)")
    db.execute(
        "CREATE VIRTUAL TABLE IF NOT EXISTS video_subtitle_phonetic_fts USING fts5("
        "pronunciation, content='video_subtitle_phonetics', content_rowid='row_id', tokenize='trigram')")
    db.execute(
        "CREATE TRIGGER IF NOT EXISTS video_phonetic_insert AFTER INSERT ON video_subtitle_phonetics "
        "BEGIN INSERT INTO video_subtitle_phonetic_fts(rowid,pronunciation) "
        "VALUES(new.row_id,new.pronunciation); END")
    db.execute(
        "CREATE TRIGGER IF NOT EXISTS video_phonetic_delete AFTER DELETE ON video_subtitle_phonetics "
        "BEGIN INSERT INTO video_subtitle_phonetic_fts(video_subtitle_phonetic_fts,rowid,pronunciation) "
        "VALUES('delete',old.row_id,old.pronunciation); END")
    db.execute(
        "CREATE TRIGGER IF NOT EXISTS video_phonetic_update AFTER UPDATE ON video_subtitle_phonetics "
        "BEGIN INSERT INTO video_subtitle_phonetic_fts(video_subtitle_phonetic_fts,rowid,pronunciation) "
        "VALUES('delete',old.row_id,old.pronunciation); "
        "INSERT INTO video_subtitle_phonetic_fts(rowid,pronunciation) VALUES(new.row_id,new.pronunciation); END")
    db.execute(
        "CREATE TRIGGER IF NOT EXISTS video_phonetic_source_update AFTER UPDATE OF text "
        "ON video_transcript_segments WHEN old.text != new.text BEGIN "
        "DELETE FROM video_subtitle_phonetics WHERE row_id=old.row_id; END")
    db.execute(
        "CREATE TRIGGER IF NOT EXISTS video_phonetic_source_delete AFTER DELETE "
        "ON video_transcript_segments BEGIN DELETE FROM video_subtitle_phonetics WHERE row_id=old.row_id; END")


def pending_query(scope_sql, time_filter):
    return (f"WITH scope AS ({scope_sql}) SELECT s.row_id,s.text "
            "FROM video_transcript_segments s JOIN scope v ON v.id=s.video_id "
            "LEFT JOIN video_subtitle_phonetics p ON p.row_id=s.row_id "
            f"WHERE (p.row_id IS NULL OR p.version!={INDEX_VERSION}) {time_filter}")


def pending_count(db, scope_sql, parameters, time_filter):
    return db.execute("SELECT COUNT(*) FROM (" + pending_query(scope_sql, time_filter) + ")",
                      parameters).fetchone()[0]


def prepare_phonetic_index(db, scope_sql, parameters, time_filter):
    """Bound work per request; compute outside writes and reject stale source rows."""
    processed, characters = 0, 0
    while processed < INDEX_BUILD_LIMIT and characters < INDEX_CHARACTER_LIMIT:
        rows = db.execute(
            pending_query(scope_sql, time_filter) + " ORDER BY s.row_id LIMIT ?",
            [*parameters, min(128, INDEX_BUILD_LIMIT - processed)],
        ).fetchall()
        if not rows:
            break
        values = []
        for row in rows:
            text = row["text"]
            values.append((pronunciation(text)[0], INDEX_VERSION, row["row_id"], text))
            processed += 1
            characters += len(text)
            if characters >= INDEX_CHARACTER_LIMIT:
                break
        try:
            db.execute("BEGIN IMMEDIATE")
            db.executemany(
                "INSERT INTO video_subtitle_phonetics(row_id,pronunciation,version) "
                "SELECT s.row_id,?,? FROM video_transcript_segments s WHERE s.row_id=? AND s.text=? "
                "ON CONFLICT(row_id) DO UPDATE SET pronunciation=excluded.pronunciation,version=excluded.version",
                values)
            db.commit()
        except Exception:
            db.rollback()
            raise
