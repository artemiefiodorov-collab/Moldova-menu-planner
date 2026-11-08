#!/usr/bin/env python3
"""
Опциональный легковесный API-сервер (Flask / FastAPI-совместимый WSGI) для отдачи
кэшированных цен и рецептов, а также запуска фонового обновления по расписанию.
Примечание: Приложение может работать полностью бесплатно и автономно на GitHub Pages
(где обновление data/prices.json выполняет GitHub Actions по cron-расписанию).
Этот сервер нужен только в том случае, если вы хотите развернуть отдельный backend
на Render.com, Railway или Fly.io.
"""

import json
import os
from http.server import HTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse
from scraper import update_catalog, DATA_FILE

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = int(os.environ.get("PORT", "8080"))


class MealPlannerHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=ROOT_DIR, **kwargs)

    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Cache-Control", "public, max-age=1800")
        super().end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/prices":
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                data = f.read().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(data)
            return
        if parsed.path == "/api/health":
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(json.dumps({"status": "ok"}).encode("utf-8"))
            return
        return super().do_GET()

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/refresh":
            catalog = update_catalog(force=False)
            payload = json.dumps({"status": "updated", "updatedAt": catalog.get("updatedAt")}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(payload)
            return
        self.send_response(404)
        self.end_headers()


if __name__ == "__main__":
    print(f"Сервер запущен на http://localhost:{PORT}")
    HTTPServer(("0.0.0.0", PORT), MealPlannerHandler).serve_forever()
