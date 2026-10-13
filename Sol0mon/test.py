from playwright.sync_api import sync_playwright

with sync_playwright() as p:

    # Connect to the already-running Chrome instance
    browser = p.chromium.connect_over_cdp(
        "http://127.0.0.1:9222"
    )

    print("Connected to Chrome")

    if not browser.contexts:
        raise Exception("No browser contexts found")

    context = browser.contexts[0]

    print(f"Contexts found: {len(browser.contexts)}")

    # -----------------------------
    # ChatGPT Test
    # -----------------------------
    chatgpt = context.new_page()

    print("Opening ChatGPT...")

    chatgpt.goto(
        "https://chatgpt.com",
        wait_until="domcontentloaded",
        timeout=120000
    )

    chatgpt.wait_for_timeout(5000)

    print("ChatGPT URL:", chatgpt.url)
    print("ChatGPT Title:", chatgpt.title())

    # -----------------------------
    # Gemini Test
    # -----------------------------
    gemini = context.new_page()

    print("Opening Gemini...")

    gemini.goto(
        "https://gemini.google.com",
        wait_until="domcontentloaded",
        timeout=120000
    )

    gemini.wait_for_timeout(10000)

    print("Gemini URL:", gemini.url)
    print("Gemini Title:", gemini.title())

    print("\n================================")
    print("Check both tabs manually.")
    print("Are ChatGPT and Gemini logged in?")
    print("================================\n")

    input("Press Enter to close the test...")

    browser.close()
