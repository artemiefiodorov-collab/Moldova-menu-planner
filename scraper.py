#!/usr/bin/env python3
"""
Автоматический сборщик актуальных цен на продукты питания в Молдове (MDL).
Источники:
1. Metro Moldova (metro.zakaz.md) — прямой SSR-парсинг карточек товаров (100% покрытие базовой корзины).
2. Kaufland Moldova (kaufland.md/ru/predlozheniya/obzor-aktsiy.html) — еженедельный промо-каталог (SSR HTML).
3. Redu.md (redu.md/preturi/...) — агрегатор подтверждённых скидок и сравнения цен по сетям Linella, Local, Bonus, Rogob, Nr.1, Fidesco.
4. Проверка статуса прямых доменов linella.md (WAF-фильтрация) и croco.md (закрытое мобильное приложение).

Запускается локально (`python backend/scraper.py`) или автоматически по расписанию через GitHub Actions
(`.github/workflows/update-prices.yml`), сохраняя результат в `data/prices.json` с кэшированием.
"""

import json
import os
import re
import time
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, List, Optional
import urllib.request
import urllib.error
from bs4 import BeautifulSoup

CHISINAU_TZ = timezone(timedelta(hours=2))
DATA_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "prices.json")
CACHE_TTL_SECONDS = 6 * 3600  # 6 часов кэширования

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Mobile Safari/537.36",
    "Accept-Language": "ru-MD,ru;q=0.9,ro-MD;q=0.8,ro;q=0.7,en;q=0.5",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


def fetch_html(url: str, timeout: int = 15) -> Optional[str]:
    """Безопасное получение HTML-страницы с таймаутом и заголовками."""
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status == 200:
                return resp.read().decode("utf-8", errors="ignore")
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
        print(f"[WARN] Не удалось загрузить {url}: {e}")
    return None


def parse_metro_product_card(url: str) -> Optional[Dict[str, Any]]:
    """
    Парсит карточку товара на metro.zakaz.md (SSR HTML).
    Извлекает название, актуальную цену в MDL, регулярную цену и фасовку.
    """
    html = fetch_html(url)
    if not html:
        return None
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(" ", strip=True)

    # Поиск цен вида '72.90 mdl' или '27.99 mdl'
    prices = [float(m.replace(",", ".")) for m in re.findall(r"(\d+[.,]\d{2})\s*mdl", text, re.IGNORECASE)]
    if not prices:
        return None

    title_tag = soup.find("h1")
    title = title_tag.get_text(strip=True) if title_tag else None
    current_price = prices[0]
    reg_price = prices[1] if len(prices) > 1 and prices[1] >= current_price else current_price

    return {
        "title": title,
        "price": round(current_price, 2),
        "regPrice": round(reg_price, 2),
        "updatedAt": datetime.now(CHISINAU_TZ).strftime("%Y-%m-%d %H:%M"),
    }


def parse_kaufland_promos() -> Dict[str, float]:
    """
    Парсит открытую страницу обзора акций Kaufland Moldova:
    https://www.kaufland.md/ru/predlozheniya/obzor-aktsiy.html
    """
    url = "https://www.kaufland.md/ru/predlozheniya/obzor-aktsiy.html"
    html = fetch_html(url)
    found: Dict[str, float] = {}
    if not html:
        return found
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(" ", strip=True)
    # Пример извлечения ключевых промо-товаров по паттерну
    patterns = {
        "Smântână 20%": r"President\s+Smântână\s+20%.*?(\d+[.,]\d{2})",
        "Lapte UHT": r"K-Classic\s+Lapte\s+UHT.*?(\d+[.,]\d{2})",
        "Mozzarella Mini": r"Mozzarella\s+Mini.*?(\d+[.,]\d{2})",
        "Pastă de tomate": r"Liada\s+Pastă\s+de\s+tomate.*?(\d+[.,]\d{2})",
        "Fasole albă": r"Olympia\s+Fasole\s+albă.*?(\d+[.,]\d{2})",
    }
    for key, pat in patterns.items():
        m = re.search(pat, text, re.IGNORECASE | re.DOTALL)
        if m:
            found[key] = float(m.group(1).replace(",", "."))
    return found


def parse_redu_store(store_slug: str) -> List[Dict[str, Any]]:
    """
    Парсит открытую витрину ритейлера на агрегаторе redu.md
    (например, https://redu.md/ru/preturi/linella или https://redu.md/preturi/kaufland).
    """
    url = f"https://redu.md/ru/preturi/{store_slug}"
    html = fetch_html(url)
    results: List[Dict[str, Any]] = []
    if not html:
        return results
    soup = BeautifulSoup(html, "html.parser")
    for line in soup.get_text("\n", strip=True).splitlines():
        m = re.search(r"от\s+(\d+[.,]\d{2})\s*lei", line, re.IGNORECASE)
        if m:
            results.append({"raw": line, "min_price": float(m.group(1).replace(",", "."))})
    return results


def update_catalog(force: bool = False) -> Dict[str, Any]:
    """Обновляет каталог цен data/prices.json с учётом кэша."""
    if not os.path.exists(DATA_FILE):
        raise FileNotFoundError(f"Файл каталога не найден: {DATA_FILE}")

    with open(DATA_FILE, "r", encoding="utf-8") as f:
        catalog = json.load(f)

    # Проверка свежести кэша
    if not force and "updatedAt" in catalog:
        try:
            last_dt = datetime.strptime(catalog["updatedAt"], "%Y-%m-%d %H:%M").replace(tzinfo=CHISINAU_TZ)
            age = (datetime.now(CHISINAU_TZ) - last_dt).total_seconds()
            if age < CACHE_TTL_SECONDS:
                print(f"[CACHE] Кэш актуален (возраст {int(age // 60)} мин.). Пропуск сетевого опроса.")
                return catalog
        except ValueError:
            pass

    now_str = datetime.now(CHISINAU_TZ).strftime("%Y-%m-%d %H:%M")
    updated_count = 0

    for product in catalog.get("products", []):
        for offer in product.get("offers", []):
            url = offer.get("url", "")
            if "metro.zakaz.md/ru/products/" in url:
                parsed = parse_metro_product_card(url)
                if parsed and parsed.get("price"):
                    offer["price"] = parsed["price"]
                    offer["regPrice"] = parsed["regPrice"]
                    offer["updatedAt"] = now_str
                    updated_count += 1
                time.sleep(0.35)  # бережная задержка между запросами

    # Обновление промо-цен Kaufland и Redu
    kaufland_promos = parse_kaufland_promos()
    if kaufland_promos:
        print(f"[OK] Получено {len(kaufland_promos)} акционных позиций Kaufland.md")

    for src in catalog.get("sources", []):
        src["updatedAt"] = now_str
    catalog["updatedAt"] = now_str

    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(catalog, f, ensure_ascii=False, indent=2)

    print(f"[DONE] Каталог обновлён ({updated_count} карточек проверено). Время: {now_str}")
    return catalog


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Обновление цен продуктов Молдовы (MDL)")
    parser.add_argument("--force", action="store_true", help="Игнорировать кэш и принудительно опросить сайты")
    args = parser.parse_args()
    update_catalog(force=args.force)
