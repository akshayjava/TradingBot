"""Web server entry point.  Run with:  python web.py  or  portfolio-web"""
import argparse

from src.web import serve

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Portfolio Bot Web UI")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--reload", action="store_true", help="Auto-reload on code changes")
    args = parser.parse_args()
    serve(host=args.host, port=args.port, reload=args.reload)
