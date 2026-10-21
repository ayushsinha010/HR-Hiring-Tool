"""Set a new password for an existing user, e.g. when the generated first-run admin password was lost.

Run from the project folder (stop the app first so the database is not in use):

    python scripts/reset_password.py            # resets the user 'admin'
    python scripts/reset_password.py someone    # resets another user

The new password is typed at a hidden prompt. It honours HR_AGENT_DATA_DIR like the app does.
"""
import getpass
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from werkzeug.security import generate_password_hash  # noqa: E402

import db  # noqa: E402


def main():
    username = (sys.argv[1] if len(sys.argv) > 1 else "admin").strip().lower()
    if not os.path.exists(db.DB_PATH):
        sys.exit(f"No database found at {db.DB_PATH} - start the app once first.")
    user = db.get_user_by_username(username)
    if not user:
        sys.exit(f"No user named '{username}'.")
    new_password = getpass.getpass(f"New password for '{username}' (min 6 characters): ")
    if len(new_password) < 6:
        sys.exit("Password must be at least 6 characters.")
    if new_password != getpass.getpass("Repeat the new password: "):
        sys.exit("The two entries do not match.")
    db.update_user_password(user["id"], generate_password_hash(new_password))
    print(f"Password updated for '{username}'.")


if __name__ == "__main__":
    main()
