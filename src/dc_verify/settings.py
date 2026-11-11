"""Configured local paths. Reads API.env settings without changing os.environ or printing secrets."""
import os
from pathlib import Path


def runtime_root():
    value=os.getenv('DC_VERIFY_DATA_DIR')
    if not value:
        from dotenv import dotenv_values
        path=env_path()
        value=dotenv_values(path,interpolate=False).get('DC_VERIFY_DATA_DIR') if path.is_file() else None
    return Path(value or 'data/runtime').expanduser().resolve()


def stage_data(number):
    return runtime_root() / ('step' + str(number))


def env_path():
    return Path(os.getenv('DC_VERIFY_ENV_FILE', 'API.env')).expanduser().resolve()


def report_policy_path():
    return Path(__file__).resolve().parent / 'report_policy.json'
