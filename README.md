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
