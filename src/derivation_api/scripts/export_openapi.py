from __future__ import annotations

import json
from pathlib import Path

from derivation_api.application import create_app
from derivation_api.fake_service import FakeDerivationService

PROJECT_ROOT = Path(__file__).resolve().parents[1]
OUTPUT = PROJECT_ROOT / "openapi.json"


def main() -> None:
    document = create_app(FakeDerivationService()).openapi()
    OUTPUT.write_text(
        json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    print(OUTPUT)


if __name__ == "__main__":
    main()
