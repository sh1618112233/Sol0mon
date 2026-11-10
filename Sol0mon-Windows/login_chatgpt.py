from playwright.sync_api import sync_playwright

with sync_playwright() as p:

    context = p.chromium.launch_persistent_context(
        user_data_dir="./chatgpt_profile",
        headless=False
    )

    page = context.new_page()

    page.goto(
        "https://chatgpt.com"
    )

    print(
        "Login manually."
    )

    input(
        "Press ENTER after login..."
    )

    context.close()
