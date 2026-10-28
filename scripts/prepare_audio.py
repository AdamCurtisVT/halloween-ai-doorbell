"""macOS local speech fixtures only. Never plays audio or contacts a camera."""
import argparse
from pathlib import Path
import subprocess
from app.audio.output import validate_test_wav

LINES = {
    'approved-test': 'Happy Halloween. This is a doorbell speaker test.',
    'fallback': 'Happy Halloween! Welcome to our haunted porch. Have a wonderful evening!',
    'goodbye': 'Thanks for visiting our haunted porch. Happy Halloween and goodbye!',
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, default=Path('artifacts'))
    args = parser.parse_args()
    args.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    for name, text in LINES.items():
        path = args.directory / (name + '.wav')
        if path.exists():
            validate_test_wav(path)
            print(f'{name}: existing WAV kept; review its content before use')
            continue
        subprocess.run(['say', '-o', str(path), '--data-format=LEI16@16000', text], check=True)
        print(name, validate_test_wav(path))


if __name__ == '__main__':
    main()
