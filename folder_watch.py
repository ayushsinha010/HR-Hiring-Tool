"""Background poller that keeps a position's candidates in sync with a linked
local folder: new resume files dropped into the folder get ingested automatically,
and files that later disappear get flagged (not deleted) for manual review - see
db.set_watch_folder for why hard-delete was deliberately avoided
(a transient sync/drive hiccup could otherwise wipe real candidate data).

Runs as a single daemon thread, polling every POLL_SECONDS. Started once from
app.py's __main__ block, guarded against Flask's debug-mode reloader spawning it
twice (see start()).
"""
import os
import time
import threading
import traceback

import db

POLL_SECONDS = 10
ALLOWED_RESUME_EXT = {".docx", ".pdf", ".txt"}


def _scan_position(app_module, position):
    folder = position.get("watch_folder")
    if not folder:
        return
    if not os.path.isdir(folder):
        # Folder temporarily unreachable (unplugged drive, sync hiccup, typo'd
        # path) - deliberately do nothing rather than treat every tracked file
        # as "missing" just because the whole folder vanished for a moment.
        return

    try:
        current_files = {
            os.path.join(folder, name)
            for name in os.listdir(folder)
            if os.path.splitext(name)[1].lower() in ALLOWED_RESUME_EXT
            and os.path.isfile(os.path.join(folder, name))
        }
    except OSError:
        return

    watched = db.get_watched_candidates(position["id"])
    known_paths = {c["watched_source_path"] for c in watched}

    new_paths = sorted(current_files - known_paths)
    if new_paths:
        pos_dir = db.position_dir(position["id"])
        resumes_dir = os.path.join(pos_dir, "resumes")
        os.makedirs(resumes_dir, exist_ok=True)
        used_names = set(os.listdir(resumes_dir)) if os.path.isdir(resumes_dir) else set()
        for src in new_paths:
            try:
                app_module.ingest_watched_file(position["id"], resumes_dir, used_names, src)
            except Exception:
                print(f"[folder_watch] failed to ingest {src!r}:")
                traceback.print_exc()

    for c in watched:
        src = c["watched_source_path"]
        is_missing = src not in current_files
        if is_missing and not c["source_missing"]:
            db.mark_source_missing(c["id"], True)
        elif not is_missing and c["source_missing"]:
            db.mark_source_missing(c["id"], False)


def _loop(app_module):
    while True:
        try:
            for position in db.positions_with_watch_folder():
                _scan_position(app_module, position)
        except Exception:
            print("[folder_watch] scan error:")
            traceback.print_exc()
        time.sleep(POLL_SECONDS)


def start(app_module):
    """Start the watcher thread. Safe to call from app.py's __main__ block even
    under the Flask debug reloader - only the actual serving process (not the
    reloader's monitor parent) will have WERKZEUG_RUN_MAIN set when debug=True,
    so this avoids running two watcher threads against the same database."""
    if app_module.app.debug and os.environ.get("WERKZEUG_RUN_MAIN") != "true":
        return None
    t = threading.Thread(target=_loop, args=(app_module,), daemon=True, name="folder-watch")
    t.start()
    return t
