# Lava-api

## WordPress automation helper

The repository now contains a small helper that can log into a WordPress site
and retrieve pages from the admin interface.  It is especially handy when you
need to automate tasks such as downloading information from WooCommerce's
"Import Export Suite → Subscriptions" screen.

```python
from app.wordpress_client import fetch_subscriptions_page

html = fetch_subscriptions_page(
    base_url="https://lavamedia.be",
    username="votre.identifiant",
    password="votre.mot.de.passe",
)

print(html[:500])
```

The module also exposes the :class:`app.wordpress_client.WordPressClient`
class, which gives you more control if you want to navigate to other admin
pages.

## Automating the WooCommerce export with Playwright

When hosts block scripted HTTP clients the repository also ships with a
Playwright-based automation helper that drives a headless Chromium instance to
perform the login flow exactly like a user would.

```python
from app.playwright_exporter import export_subscriptions_csv_with_playwright

content, filename, content_type = export_subscriptions_csv_with_playwright(
    base_url="https://lavamedia.be",
    username="votre.identifiant",
    password="votre.mot.de.passe",
)

print(filename, len(content), content_type)
```

Make sure to install the browser binaries once in your environment with
`playwright install chromium` before running the helper.
