import secrets
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import HTTPException
from pwdlib import PasswordHash

from app.rbac_guard import PERSONAS, authorized_scopes

ISSUER = 'orgmind-auth'
AUDIENCE = 'orgmind-api'


class Auth:
    def __init__(self, db, settings):
        self.db, self.settings = db, settings
        self.dummy_hash = PasswordHash.recommended().hash('not-a-login-password')

    def issue(self, user):
        if not user or not user.get('active', True):
            raise HTTPException(401, 'Invalid credentials')
        now = datetime.now(timezone.utc)
        token = jwt.encode({'sub': user['id'], 'role': user['role'], 'ver': user['auth_version'],
                            'iat': now, 'exp': now + timedelta(minutes=self.settings.token_minutes),
                            'iss': ISSUER, 'aud': AUDIENCE, 'jti': secrets.token_hex(16)},
                           self.settings.jwt_secret, algorithm='HS256')
        return {'access_token': token, 'token_type': 'bearer', 'user': self.public(user)}

    def public(self, user):
        department = None
        if user.get('department_id'):
            with self.db.connect() as conn:
                row = conn.execute('SELECT id,name FROM departments WHERE id=?', (user['department_id'],)).fetchone()
                department = dict(row) if row else None
        return {'id': user['id'], 'role': user['role'], 'scopes': authorized_scopes(user), 'acl_epoch': self.db.epoch(),
                'name': user.get('name', user['id']), 'email': user.get('email'),
                'department_id': user.get('department_id'), 'department': department,
                'active': bool(user.get('active', True))}

    def verify(self, token):
        try:
            claims = jwt.decode(token, self.settings.jwt_secret, algorithms=['HS256'], issuer=ISSUER, audience=AUDIENCE,
                                options={'require': ['sub', 'role', 'ver', 'exp', 'iat', 'iss', 'aud']})
            user = self.db.user(claims['sub'])
            if not user or not user['active'] or claims['ver'] != user['auth_version'] or claims['role'] != user['role']:
                raise ValueError('Identity changed')
            return user
        except (jwt.PyJWTError, ValueError, TypeError):
            raise HTTPException(401, 'Invalid or expired authentication') from None

    def login(self, uid, password):
        user = self.db.user_by_login(uid)
        valid = PasswordHash.recommended().verify(password, user['password_hash'] if user else self.dummy_hash)
        if not valid or not user or not user['active']:
            self.db.audit(None, 'login', 'deny', rule='credentials')
            raise HTTPException(401, 'Invalid credentials')
        self.db.audit(user, 'login', 'allow', rule='credentials')
        return self.issue(user)

    def demo(self, persona_id):
        if not self.settings.allow_demo:
            raise HTTPException(403, 'Demo access disabled')
        if persona_id not in PERSONAS:
            raise HTTPException(400, 'Unknown demo persona')
        user = self.db.user(persona_id)
        if not user or not user['active']:
            raise HTTPException(401, 'Invalid credentials')
        self.db.audit(user, 'demo-login', 'allow', rule='synthetic-demo')
        return self.issue(user)
