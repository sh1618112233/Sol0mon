from playwright.sync_api import sync_playwright
from urllib.parse import quote_plus
from datetime import datetime
import os
import json


LOG_DIR = "logs"

LOG_FILE = os.path.join(
    LOG_DIR,
    "verification_research.log"
)


DOMAIN_SCORES = {
    ".gov": 100,
    ".edu": 90,
    "python.org": 100,
    "gvanrossum.github.io": 95,
    "arxiv.org": 90,
    "scholar.google": 90,
    "wikipedia.org": 75,
    "britannica.com": 75,
    "github.com": 70,
    "github.io": 50,
    "pythoninstitute.org": 80,
    "reuters.com": 70,
    "bbc.com": 70,
    "stackexchange.com": 50,
    "youtube.com": -50,
    "youtu.be": -50,
    "brainly": -60,
    "quora": -60,
    "pinterest": -70,
    "facebook": -70,
    "instagram": -70,
    "cloudflare": -100,
    "captcha": -100
}


def ensure_log_dir():
    os.makedirs(
        LOG_DIR,
        exist_ok=True
    )


def write_log(message):
    ensure_log_dir()

    timestamp = datetime.now().strftime(
        "%Y-%m-%d %H:%M:%S"
    )

    with open(
        LOG_FILE,
        "a",
        encoding="utf-8"
    ) as f:
        f.write(
            f"[{timestamp}] {message}\n"
        )


def canonicalize_url(url):
    url = url.split("#")[0]

    if url.endswith("/"):
        url = url[:-1]

    return url


def score_url(url):
    score = 0
    url_lower = url.lower()

    for key, value in DOMAIN_SCORES.items():
        if key in url_lower:
            score += value

    return score


def is_rejected_url(url):
    url = url.lower()

    reject_patterns = [
        "youtube.com",
        "youtu.be",
        "brainly",
        "quora",
        "cloudflare",
        "captcha",
        "facebook",
        "instagram",
        "pinterest"
    ]

    return any(
        pattern in url
        for pattern in reject_patterns
    )


def collect_urls(page):
    urls = []

    links = page.locator(
        "a"
    ).all()

    for link in links:
        try:
            href = link.get_attribute(
                "href"
            )

            if not href:
                continue

            if not href.startswith(
                "http"
            ):
                continue

            if "google." in href:
                continue

            href = canonicalize_url(
                href
            )

            if href not in urls:
                urls.append(
                    href
                )

        except Exception:
            pass

    return urls[:20]


def extract_source(
    context,
    url
):
    try:
        page = context.new_page()

        page.goto(
            url,
            wait_until="domcontentloaded",
            timeout=60000
        )

        page.wait_for_timeout(
            3000
        )

        title = page.title()

        content = page.locator(
            "body"
        ).inner_text()

        page.close()

        return {
            "url": url,
            "title": title,
            "score": score_url(
                url
            ),
            "content": content[:8000]  # <--- Fix 2: Expanded limit to capture deep technical data
        }

    except Exception as e:
        write_log(
            f"FAILED {url} : {str(e)}"
        )

        return None


def research_claim(claim):
    evidence = []

    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(
            "http://127.0.0.1:9222"
        )

        context = browser.contexts[0]

        page = context.new_page()

        search_url = (
            "https://www.google.com/search?q="
            + quote_plus(claim)
        )

        write_log(
            f"SEARCH {claim}"
        )

        page.goto(
            search_url,
            wait_until="domcontentloaded",
            timeout=120000
        )

        page.wait_for_timeout(
            3000
        )

        urls = collect_urls(
            page
        )

        page.close()

        # <--- Fix 1: Search success/failure telemetry
        write_log(
            f"COLLECTED_URL_COUNT={len(urls)}"
        )
        
        if len(urls) == 0:
            write_log(
                "WARNING: ZERO URLS FOUND"
            )

        ranked_urls = []

        for url in urls:
            if is_rejected_url(
                url
            ):
                continue

            ranked_urls.append(
                {
                    "url": url,
                    "score": score_url(
                        url
                    )
                }
            )

        ranked_urls.sort(
            key=lambda x: x["score"],
            reverse=True
        )

        ranked_urls = [
            x
            for x in ranked_urls
            if x["score"] > 0
        ][:10]

        write_log(
            f"SELECTED {len(ranked_urls)} HIGH QUALITY URLS"
        )

        for item in ranked_urls:
            source = extract_source(
                context,
                item["url"]
            )

            if source:
                evidence.append(
                    source
                )

                write_log(
                    f"EXTRACTED {item['url']} SCORE={item['score']}"
                )

    return {
        "claim": claim,
        "sources": evidence
    }


if __name__ == "__main__":
    result = research_claim(
        "Who invented Python?"
    )

    print(
        json.dumps(
            result,
            indent=4
        )
    )
