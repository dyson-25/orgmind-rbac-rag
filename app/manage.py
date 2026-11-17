"""Local account provisioning for the synthetic demonstration."""
import argparse
from getpass import getpass

from pwdlib import PasswordHash

from app.config import Settings
from app.database import Database
from app.rbac_guard import PERSONAS


def set_password(db, uid, password):
    if uid not in PERSONAS or len(password) < 12:
        raise ValueError('Use a predefined persona and a password of at least 12 characters')
    with db.lock, db.connect() as conn:
        changed = conn.execute('UPDATE users SET password_hash=?, auth_version=auth_version+1 WHERE id=?',
                               (PasswordHash.recommended().hash(password), uid)).rowcount
        if not changed:
            raise ValueError('Unknown persona')
        db.bump_epoch(conn)
    db.audit(None, 'local-password-provision', 'allow', rule='local-operator')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['set-password'])
    parser.add_argument('persona', choices=PERSONAS)
    args = parser.parse_args()
    password = getpass('New password (12+ characters): ')
    if password != getpass('Confirm password: '):
        parser.error('Passwords do not match')
    db = Database(Settings())
    db.initialize()
    try:
        set_password(db, args.persona, password)
    except ValueError as exc:
        parser.error(str(exc))
    print('Password updated; previous tokens for this persona are invalidated.')


if __name__ == '__main__':
    main()
