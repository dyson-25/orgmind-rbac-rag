"""Explicit test credentials, installed before application modules are imported."""

import os


os.environ['JWT_SECRET'] = 'orgmind-pytest-only-key-not-for-deployment-2026'
