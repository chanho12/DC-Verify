#!/bin/sh
# Run one existing CLI command. No automatic install, key sourcing or API request.
set -eu

dc_verify_root=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
cd "$dc_verify_root"

if [ -n "${DC_VERIFY_PYTHON:-}" ]; then
    dc_verify_python=$DC_VERIFY_PYTHON
elif [ -x "$dc_verify_root/.venv/bin/python" ]; then
    dc_verify_python=$dc_verify_root/.venv/bin/python
elif [ -n "${VIRTUAL_ENV:-}" ] && [ -x "$VIRTUAL_ENV/bin/python" ]; then
    dc_verify_python=$VIRTUAL_ENV/bin/python
elif [ -x "$dc_verify_root/../../01_experiments/.venv/bin/python" ]; then
    dc_verify_python=$dc_verify_root/../../01_experiments/.venv/bin/python
elif command -v python3 >/dev/null 2>&1; then
    dc_verify_python=python3
else
    printf '%s\n' 'Python을 찾지 못했습니다. README의 가상환경 설치 절차를 먼저 실행하세요.' >&2
    exit 127
fi

# Make the adjacent packaged source runnable before an editable/wheel install.
# Third-party dependencies must already be installed in the chosen environment.
export PYTHONPATH="$dc_verify_root/src${PYTHONPATH:+:$PYTHONPATH}"
if [ "$#" -eq 0 ]; then
    set -- --help
elif [ "$1" = "--pdf" ]; then
    set -- verify "$@"
fi
exec "$dc_verify_python" -m dc_verify.cli "$@"
