"""Write the API's OpenAPI schema as JSON (for the web app's generated client).

Run: make api-types
"""

import json
import sys
from pathlib import Path

from app.main import app

if __name__ == "__main__":
    out = Path(sys.argv[1])
    out.write_text(json.dumps(app.openapi(), indent=2, sort_keys=True) + "\n")
    print(f"wrote {out}")
