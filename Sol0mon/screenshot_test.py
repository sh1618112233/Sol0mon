from playwright.sync_api import sync_playwright

with sync_playwright() as p:

    browser = p.chromium.launch(
        headless=True
    )

    page = browser.new_page()

    page.goto(
        "https://chatgpt.com",
        timeout=120000
    )

    page.wait_for_timeout(10000)

    print(
        "TITLE:",
        page.title()
    )

    page.screenshot(
        path="chatgpt.png"
    )

    browser.close()
