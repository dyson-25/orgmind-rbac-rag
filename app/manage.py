"""Local password provisioning for existing accounts."""
import argparse
from getpass import getpass

from app.config import Settings
from app.database import Database


def set_password(db, uid, password):
    user = db.user_by_login(uid)
    if not user:
        raise ValueError('Unknown account')
    db.update_account(user['id'], {'password': password})
    db.audit(None, 'local-password-provision', 'allow', rule='local-operator')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['set-password'])
    parser.add_argument('username', help='Existing username or email')
    args = parser.parse_args()
    password = getpass('New password (12+ characters): ')
    if password != getpass('Confirm password: '):
        parser.error('Passwords do not match')
    db = Database(Settings())
    db.initialize()
    try:
        set_password(db, args.username, password)
    except ValueError as exc:
        parser.error(str(exc))
    print('Password updated; previous tokens for this account are invalidated.')


if __name__ == '__main__':
    main()
