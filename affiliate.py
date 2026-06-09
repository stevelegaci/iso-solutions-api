"""
Affiliate link rewriter for ISO Solutions.
Detects the merchant from a product URL and appends the correct affiliate
tracking parameters before the link is served to members.

To activate real commissions, replace placeholder IDs with your actual
affiliate IDs from each program's dashboard:
  Amazon Associates  → https://affiliate-program.amazon.com
  eBay Partner Net   → https://partnernetwork.ebay.com
  Walmart Affiliates → https://affiliates.walmart.com
  Target Affiliates  → https://partners.target.com   (via CJ / Impact)
  Etsy Affiliates    → https://www.etsy.com/affiliates (via Awin)
  Best Buy Affiliates→ https://affbiz.bestbuy.com    (via CJ)

All IDs are configurable via environment variables so you never hard-code
credentials in source.
"""
import os
import re
from typing import Optional
from urllib.parse import urlparse, urlencode, quote_plus

# ── Affiliate credential env-vars ─────────────────────────────────────────────
# Amazon Associates tag (format: yourname-XX, e.g. isosolutions-20)
AMAZON_TAG = os.getenv("AFFILIATE_AMAZON_TAG", "isosolutions-20")

# eBay Partner Network campaign ID (numeric, from EPN dashboard)
EBAY_CAMPID = os.getenv("AFFILIATE_EBAY_CAMPID", "5338000000")

# Walmart affiliate ID (from Walmart Affiliates / Impact portal)
WALMART_PUB_ID = os.getenv("AFFILIATE_WALMART_PUB_ID", "")

# Target / CJ affiliate ID
TARGET_CID = os.getenv("AFFILIATE_TARGET_CID", "")

# Etsy via Awin: Awin affiliate ID + Awin merchant ID for Etsy (6220)
ETSY_AWIN_AFFID = os.getenv("AFFILIATE_ETSY_AWIN_AFFID", "")
ETSY_AWIN_MID   = os.getenv("AFFILIATE_ETSY_AWIN_MID",   "6220")

# Best Buy via CJ affiliate ID
BESTBUY_CJ_PID = os.getenv("AFFILIATE_BESTBUY_CJ_PID", "")


# ── Per-merchant rewriters ────────────────────────────────────────────────────

def _rewrite_amazon(url: str) -> str:
    """Append/replace Amazon Associates tag."""
    url = re.sub(r"([?&])tag=[^&]*", r"\1", url).rstrip("?&")
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}tag={AMAZON_TAG}&linkCode=iso&language=en_US"


def _rewrite_ebay(url: str) -> str:
    """Append eBay Partner Network tracking params."""
    if "campid=" in url:
        return url  # already tagged
    sep = "&" if "?" in url else "?"
    params = urlencode({
        "mkcid":   "1",
        "mkrid":   "711-53200-19255-0",
        "siteid":  "0",
        "campid":  EBAY_CAMPID,
        "customid": "iso",
        "toolid":  "10001",
        "mkevt":   "1",
    })
    return f"{url}{sep}{params}"


def _rewrite_walmart(url: str) -> str:
    """Append Walmart affiliate publisher ID via Impact."""
    if not WALMART_PUB_ID:
        return url  # skip until real ID is configured
    if "wmlspartner=" in url or "u1=" in url:
        return url
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}wmlspartner=lFA9FRQwFBA&affp1=iso&u1={WALMART_PUB_ID}"


def _rewrite_target(url: str) -> str:
    """Append Target CJ affiliate params."""
    if not TARGET_CID:
        return url
    if "afid=" in url or "AFID=" in url:
        return url
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}afid={TARGET_CID}&ref=tgt_adv_xasd&CPNG=ISO_Solutions&afsc=1"


def _rewrite_etsy(url: str) -> str:
    """Wrap Etsy URL through Awin deep link."""
    if not ETSY_AWIN_AFFID:
        return url
    if "awin1.com" in url:
        return url
    encoded = quote_plus(url)
    return (
        f"https://www.awin1.com/cread.php"
        f"?awinmid={ETSY_AWIN_MID}&awinaffid={ETSY_AWIN_AFFID}&ued={encoded}"
    )


def _rewrite_bestbuy(url: str) -> str:
    """Append Best Buy CJ affiliate params."""
    if not BESTBUY_CJ_PID:
        return url
    if "ref=aff" in url or "aid=" in url:
        return url
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}ref=aff&aid={BESTBUY_CJ_PID}&pid=iso_solutions"


# ── Domain → rewriter routing ─────────────────────────────────────────────────
_REWRITERS: dict[str, callable] = {
    "amazon.com":    _rewrite_amazon,
    "amazon.co.uk":  _rewrite_amazon,
    "amzn.to":       _rewrite_amazon,
    "amzn.com":      _rewrite_amazon,
    "ebay.com":      _rewrite_ebay,
    "ebay.co.uk":    _rewrite_ebay,
    "ebay.ca":       _rewrite_ebay,
    "walmart.com":   _rewrite_walmart,
    "target.com":    _rewrite_target,
    "etsy.com":      _rewrite_etsy,
    "bestbuy.com":   _rewrite_bestbuy,
}


def _get_domain(url: str) -> Optional[str]:
    try:
        return urlparse(url).netloc.lower().lstrip("www.")
    except Exception:
        return None


def rewrite_url(url: str) -> str:
    """
    Detect the merchant from `url` and append affiliate tracking params.
    Returns the original URL unchanged if:
      - URL is empty / unparseable
      - Merchant is not in the affiliate table
      - Affiliate ID for that merchant is not yet configured
    Never raises — if rewriting fails the original URL is returned.
    """
    if not url:
        return url
    domain = _get_domain(url)
    if not domain:
        return url
    for pattern, fn in _REWRITERS.items():
        if domain == pattern or domain.endswith("." + pattern):
            try:
                return fn(url)
            except Exception:
                return url
    return url


def rewrite_results(results: list) -> list:
    """
    Add an `affiliate_url` key to every result dict.

    - `url`           keeps the canonical product URL (unchanged)
    - `affiliate_url` carries the tracking link (equals `url` when no
                      affiliate program is configured for that merchant)

    This preserves the original URL for display / caching while serving
    the affiliate link when the user actually clicks through.
    Returns a new list; does not mutate the input dicts.
    """
    out = []
    for item in results:
        item = dict(item)
        original = item.get("url", "")
        rewritten = rewrite_url(original)
        item["affiliate_url"] = rewritten
        # Flag so callers know a rewrite happened
        item["affiliate_tagged"] = rewritten != original
        out.append(item)
    return out
