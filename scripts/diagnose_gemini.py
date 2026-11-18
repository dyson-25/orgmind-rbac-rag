"""Prompt-only Gemini access/completion check; never prints provider content or keys."""

import getpass
from pathlib import Path
import sys
import warnings

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.llm_service import (  # noqa: E402
    _completion_text,
    _diagnostics,
    _provider_http_request,
)


MODEL = 'gemini-3.8-flash'
BASE_URL = 'https://generativelanguage.googleapis.com/v1beta/openai/'
TIMEOUT_SECONDS = 15.0


def _report(stage, diagnostic):
    status = diagnostic['http_status']
    print(
        f"stage={stage} provider={diagnostic['provider']} model={diagnostic['model']} "
        f"http_status={status if status is not None else 'none'} "
        f"error_category={diagnostic['error_category'] or 'none'} "
        f"succeeded={str(diagnostic['succeeded']).lower()} "
        f"fallback_used={str(diagnostic['fallback_used']).lower()}"
    )


def _check(stage, method, url, **kwargs):
    diagnostic = _diagnostics('gemini', MODEL)
    try:
        response = _provider_http_request(
            method, url, diagnostic, timeout=TIMEOUT_SECONDS, **kwargs,
        )
        try:
            if stage == 'access':
                if response.json()['name'] != f'models/{MODEL}':
                    raise ValueError('Unexpected model')
            else:
                _completion_text(response)
        finally:
            response.close()
        diagnostic['succeeded'] = True
    except Exception:
        diagnostic['error_category'] = diagnostic['error_category'] or 'malformed'
    _report(stage, diagnostic)
    return diagnostic['succeeded']


def main(argv=None):
    """No key arguments, environment lookup, plaintext input fallback, or key persistence."""
    arguments = sys.argv[1:] if argv is None else argv
    diagnostic = _diagnostics('gemini', MODEL)
    if arguments:
        diagnostic['error_category'] = 'config'
        _report('input', diagnostic)
        return 2
    try:
        with warnings.catch_warnings():
            # getpass otherwise warns and falls back to echoing stdin on a non-TTY.
            warnings.simplefilter('error', getpass.GetPassWarning)
            key = getpass.getpass('Gemini API key (hidden; not saved): ').strip()
        if not key:
            raise ValueError('Missing key')
    except (Exception, KeyboardInterrupt):
        diagnostic['error_category'] = 'config'
        _report('input', diagnostic)
        return 2

    try:
        access = _check(
            'access', 'get', f'https://generativelanguage.googleapis.com/v1beta/models/{MODEL}',
            headers={'x-goog-api-key': key},
        )
        if not access:
            return 1
        completion = _check(
            'completion', 'post', BASE_URL + 'chat/completions',
            headers={'Authorization': f'Bearer {key}'},
            json={'model': MODEL, 'messages': [{'role': 'user', 'content': 'Reply with OK.'}],
                  'max_tokens': 128},
        )
        return 0 if completion else 1
    except KeyboardInterrupt:
        diagnostic['error_category'] = 'config'
        _report('cancelled', diagnostic)
        return 2
    finally:
        # Drop the local reference; Python cannot guarantee erasure from process memory.
        key = None


if __name__ == '__main__':
    raise SystemExit(main())
