from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.launch(
        headless=False,
        args=[
            "--no-sandbox",
            "--disable-gpu"
        ]
    )

    page = browser.new_page()
    page.goto("https://example.com")

    input("Press Enter to close...")

    browser.close()
