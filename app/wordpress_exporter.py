from __future__ import annotations

import csv
import io
from typing import Iterable

from .wordpress_client import WordPressClient


def extract_meta_value(meta_list: list[dict], candidates: Iterable[str]) -> str:
    cand_lower = {c.lower() for c in candidates}
    for m in meta_list or []:
        key = (m.get("key") or "").strip().lower()
        if key in cand_lower:
            val = m.get("value")
            return str(val) if val is not None else ""
    return ""


def fetch_subscriptions(
    base_url: str,
    consumer_key: str,
    consumer_secret: str,
    status: str = "active",
) -> list[dict]:
    client = WordPressClient(base_url)
    params = {"status": status} if status else {}
    return client.wc_paginate("subscriptions", consumer_key, consumer_secret, params)


def export_subscriptions_csv_via_wc_api(
    base_url: str,
    consumer_key: str,
    consumer_secret: str,
    status: str = "active",
) -> tuple[bytes, str, str]:
    subs = fetch_subscriptions(base_url, consumer_key, consumer_secret, status)

    headers = [
        "subscription_status",
        "shipping_address_1",
        "shipping_postcode",
        "shipping_city",
        "shipping_country",
        "shipping_first_name",
        "shipping_last_name",
        "meta:Language",
    ]

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=headers, extrasaction="ignore")
    writer.writeheader()

    for s in subs:
        shipping = s.get("shipping") or {}
        meta_list = s.get("meta_data") or []
        row = {
            "subscription_status": s.get("status") or "",
            "shipping_address_1": shipping.get("address_1") or "",
            "shipping_postcode": shipping.get("postcode") or "",
            "shipping_city": shipping.get("city") or "",
            "shipping_country": shipping.get("country") or "",
            "shipping_first_name": shipping.get("first_name") or "",
            "shipping_last_name": shipping.get("last_name") or "",
            "meta:Language": extract_meta_value(meta_list, ("Language", "language", "wpml_language", "lang")),
        }
        writer.writerow(row)

    csv_text = buf.getvalue()
    buf.close()

    filename = f"subscriptions_export_{status or 'all'}.csv"
    content_type = "text/csv"
    return csv_text.encode("utf-8"), filename, content_type


def subscriptions_as_html_table(subs: list[dict]) -> str:
    """
    Construit une petite table HTML (pour rétrocompat de /wordpress/subscriptions).
    """
    rows = []
    for s in subs:
        shipping = s.get("shipping") or {}
        rows.append(
            f"<tr>"
            f"<td>{s.get('status','')}</td>"
            f"<td>{shipping.get('first_name','')}</td>"
            f"<td>{shipping.get('last_name','')}</td>"
            f"<td>{shipping.get('address_1','')}</td>"
            f"<td>{shipping.get('postcode','')}</td>"
            f"<td>{shipping.get('city','')}</td>"
            f"<td>{shipping.get('country','')}</td>"
            f"</tr>"
        )
    table = (
        "<table>"
        "<thead><tr>"
        "<th>Status</th><th>First name</th><th>Last name</th>"
        "<th>Address 1</th><th>Postcode</th><th>City</th><th>Country</th>"
        "</tr></thead>"
        f"<tbody>{''.join(rows)}</tbody>"
        "</table>"
    )
    return table
