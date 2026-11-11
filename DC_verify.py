"""PDF + API.env + 검토 질문 → DC-Verify. STEP 구현은 src 아래에 유지한다."""
from pathlib import Path
import sys

if __name__ == '__main__':
    sys.path.insert(0, str(Path(__file__).resolve().parent / 'src'))
    from dc_verify.cli import main
    args = sys.argv[1:]
    if args and args[0].startswith('--') and args[0] not in {'--help', '-h'}:
        args.insert(0, 'verify')
    raise SystemExit(main(args))
