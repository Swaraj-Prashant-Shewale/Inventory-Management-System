"""Start the web application.

    .venv\\Scripts\\python.exe run_web.py            (development, http://127.0.0.1:8000)

In production run it behind the host's HTTPS with:
    uvicorn web.app:app --host 0.0.0.0 --port $PORT --workers 1
and set WEB_COOKIE_SECURE=1 plus a stable WEB_SECRET_KEY in the environment.
"""
import os

import uvicorn


def main():
    uvicorn.run(
        "web.app:app",
        host=os.environ.get("WEB_HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "8000")),
        reload=os.environ.get("WEB_RELOAD", "").lower() in ("1", "true"),
    )


if __name__ == "__main__":
    main()
