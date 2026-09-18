#!/usr/bin/env python3
"""
Daily Commission Factory feed generator for Tulio (tulio.com.au).

Run in GitHub Actions. Mints a fresh Admin API access token each run via the
Shopify client credentials grant (tokens expire in ~24h so we never cache one),
then pages through the full active catalog via the GraphQL Admin API and writes
feed/tulio-commission-factory.xml.

Required environment variables (set as GitHub Actions repo secrets):
    SHOPIFY_STORE_DOMAIN   e.g. tulio-fashion.myshopify.com
    CF_FEED_CLIENT_ID      Client ID of the "Commission Factory Feed" custom app
    CF_FEED_CLIENT_SECRET  Client secret of the same app
"""

import html
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import json
from datetime import datetime
from xml.etree.ElementTree import Element, SubElement, tostring
from xml.dom import minidom

API_VERSION = "2026-07"
STORE_URL = "https://tulio.com.au"
CURRENCY = "AUD"
EXCLUDED_PRODUCT_TYPES = {"Gift Cards"}

# Shipping tiers (Stacey's policy, confirmed 2026-09-18):
#   under $100   -> $9.95, regular shipping
#   $100 - $170  -> free, regular shipping
#   over $170    -> free, express shipping
# TODO: confirm actual regular/express transit-time wording with Stacey.
REGULAR_SHIPPING_TIME = "3-7 business days"
EXPRESS_SHIPPING_TIME = "1-3 business days"

PRODUCTS_QUERY = """
query CFFeed($first: Int!, $after: String) {
  products(first: $first, after: $after, query: "status:active") {
    pageInfo { hasNextPage endCursor }
    edges {
      node {
        title
        descriptionHtml
        vendor
        productType
        handle
        updatedAt
        featuredImage { url }
        variants(first: 100) {
          edges {
            node {
              sku
              price
              compareAtPrice
              inventoryQuantity
              title
              selectedOptions { name value }
            }
          }
        }
      }
    }
  }
}
"""


def get_access_token(shop_domain: str, client_id: str, client_secret: str) -> str:
    url = f"https://{shop_domain}/admin/oauth/access_token"
    body = urllib.parse.urlencode(
        {
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
        }
    ).encode()
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read())
            return data["access_token"]
    except urllib.error.HTTPError as e:
        raise SystemExit(f"Token request failed ({e.code}): {e.read().decode()}")


def graphql(shop_domain: str, token: str, query: str, variables: dict) -> dict:
    url = f"https://{shop_domain}/admin/api/{API_VERSION}/graphql.json"
    req = urllib.request.Request(
        url,
        data=json.dumps({"query": query, "variables": variables}).encode(),
        method="POST",
    )
    req.add_header("Content-Type", "application/json")
    req.add_header("X-Shopify-Access-Token", token)
    try:
        with urllib.request.urlopen(req) as resp:
            payload = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise SystemExit(f"GraphQL request failed ({e.code}): {e.read().decode()}")
    if "errors" in payload:
        raise SystemExit(f"GraphQL errors: {payload['errors']}")
    return payload["data"]


def fetch_all_products(shop_domain: str, token: str):
    products = []
    after = None
    while True:
        data = graphql(shop_domain, token, PRODUCTS_QUERY, {"first": 100, "after": after})
        block = data["products"]
        for edge in block["edges"]:
            node = edge["node"]
            node["variants"] = [v["node"] for v in node["variants"]["edges"]]
            products.append(node)
        if not block["pageInfo"]["hasNextPage"]:
            break
        after = block["pageInfo"]["endCursor"]
    return products


def strip_html(raw_html: str) -> str:
    if not raw_html:
        return ""
    text = re.sub(r"<[^>]+>", " ", raw_html)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def option_value(selected_options, name):
    for opt in selected_options or []:
        if opt.get("name", "").lower() == name.lower():
            return opt.get("value")
    return None


def iso_timestamp(value: str) -> str:
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return value


def delivery_for_price(price: float):
    if price > 170:
        return "0.00", EXPRESS_SHIPPING_TIME
    if price >= 100:
        return "0.00", REGULAR_SHIPPING_TIME
    return "9.95", REGULAR_SHIPPING_TIME


def build_feed(products, store_url=STORE_URL):
    root = Element("products")
    for product in products:
        if product.get("productType") in EXCLUDED_PRODUCT_TYPES:
            continue
        variants = product.get("variants") or []
        if not variants:
            continue

        product_el = SubElement(root, "product")
        SubElement(product_el, "title").text = product.get("title", "")
        SubElement(product_el, "description").text = strip_html(product.get("descriptionHtml", ""))
        SubElement(product_el, "category").text = product.get("productType", "")
        SubElement(product_el, "brand").text = product.get("vendor", "")

        url = f"{store_url}/products/{product.get('handle', '')}"
        SubElement(product_el, "url").text = url[:256]

        image = (product.get("featuredImage") or {}).get("url", "")
        SubElement(product_el, "standard_image").text = image
        SubElement(product_el, "thumbnail_image").text = image

        SubElement(product_el, "last_updated").text = iso_timestamp(product.get("updatedAt", ""))
        SubElement(product_el, "gender").text = "Female"

        variants_el = SubElement(product_el, "variants")
        for v in variants:
            variant_el = SubElement(variants_el, "variant")
            SubElement(variant_el, "sku").text = v.get("sku") or ""
            price = float(v.get("price", 0))
            SubElement(variant_el, "price").text = f"{price:.2f}"
            was_price = v.get("compareAtPrice")
            if was_price:
                SubElement(variant_el, "was_price").text = f"{float(was_price):.2f}"
            SubElement(variant_el, "currency").text = CURRENCY

            qty = v.get("inventoryQuantity") or 0
            SubElement(variant_el, "stock").text = "Yes" if qty > 0 else "No"

            size = option_value(v.get("selectedOptions"), "Size")
            if size:
                SubElement(variant_el, "size").text = size

            delivery_cost, delivery_time = delivery_for_price(price)
            SubElement(variant_el, "delivery_cost").text = delivery_cost
            SubElement(variant_el, "delivery_time").text = delivery_time

    return root


def prettify(elem) -> str:
    rough = tostring(elem, encoding="utf-8")
    return minidom.parseString(rough).toprettyxml(indent="  ")


def main():
    shop_domain = os.environ["SHOPIFY_STORE_DOMAIN"]
    client_id = os.environ["CF_FEED_CLIENT_ID"]
    client_secret = os.environ["CF_FEED_CLIENT_SECRET"]

    token = get_access_token(shop_domain, client_id, client_secret)
    products = fetch_all_products(shop_domain, token)
    root = build_feed(products)
    xml_str = prettify(root)

    out_path = "feed/tulio-commission-factory.xml"
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(xml_str)

    print(f"Wrote {out_path} with {len(root)} products", file=sys.stderr)


if __name__ == "__main__":
    main()
