SCOPES = ('general', 'hr', 'finance', 'engineering', 'executive')
ROLE_SCOPES = {
    'admin': SCOPES,
    'finance': ('general', 'finance'),
    'hr': ('general', 'hr'),
    'engineering': ('general', 'engineering'),
    'employee': ('general',),
    'executive': ('general', 'executive'),
}
PERSONAS = {
    'admin_user': 'admin', 'finance_user': 'finance', 'hr_user': 'hr',
    'eng_user': 'engineering', 'general_user': 'employee', 'executive_user': 'executive',
}
DENIAL = 'Insufficient authorized evidence in your permitted workspace scopes to answer this request.'


def authorized_scopes(user):
    return ROLE_SCOPES[user['role']]


def validate_scopes(scopes):
    if not scopes or not set(scopes) <= set(SCOPES):
        raise ValueError('Invalid scopes')
    return sorted(set(scopes))


def authorized_chunk_ids(conn, user):
    scopes = authorized_scopes(user)
    return {row[0] for row in conn.execute(
        'SELECT c.id FROM chunks c WHERE EXISTS (SELECT 1 FROM document_scopes s '
        f'WHERE s.document_id=c.document_id AND s.scope IN ({",".join("?" for _ in scopes)}))', scopes)}


def validate_citations(citations, allowed):
    if any(c['chunk_id'] not in allowed for c in citations):
        raise ValueError('Citation authorization failed')
