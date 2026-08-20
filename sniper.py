import argparse
import json
import os
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

APP_ID = os.environ.get("EBAY_APP_ID")
CERT_ID = os.environ.get("EBAY_CERT_ID")
USER_TOKEN = os.environ.get("EBAY_USER_TOKEN")
REFRESH_TOKEN = os.environ.get("EBAY_REFRESH_TOKEN")

API_ROOT = "https://api.ebay.com/buy/browse/v1"
TRADING_ROOT = "https://api.ebay.com/ws/api.dll"
OAUTH_URL = "https://api.ebay.com/identity/v1/oauth2/token"
WATCHLIST_PATH = Path.home() / ".config" / "sniper" / "watchlist.json"
CACHE_DIR = Path.home() / ".cache" / "sniper"

_token_expires = 0.0
_current_token = USER_TOKEN or ""

def _token():
    global _token_expires, _current_token
    if time.time() < _token_expires - 60 and _current_token:
        return _current_token
    if not (APP_ID and CERT_ID and REFRESH_TOKEN):
        raise RuntimeError("missing EBAY_APP_ID, EBAY_CERT_ID, or EBAY_REFRESH_TOKEN")
    r = httpx.post(
        OAUTH_URL,
        auth=(APP_ID, CERT_ID),
        data={
            "grant_type": "refresh_token",
            "refresh_token": REFRESH_TOKEN,
            "scope": "https://api.ebay.com/oauth/api_scope/buy.order https://api.ebay.com/oauth/api_scope/buy.item.bid",
        },
        timeout=30,
    )
    r.raise_for_status()
    data = r.json()
    _current_token = data["access_token"]
    _token_expires = time.time() + data["expires_in"]
    return _current_token

def _headers():
    return {
        "Authorization": f"Bearer {_token()}",
        "X-EBAY-C-MARKETPLACE-ID": "EBAY_US",
        "Content-Type": "application/json",
    }

def _trading_headers(call_name):
    return {
        "X-EBAY-API-CALL-NAME": call_name,
        "X-EBAY-API-SITEID": "0",
        "X-EBAY-API-COMPATIBILITY-LEVEL": "967",
        "X-EBAY-API-IAF-TOKEN": _token(),
        "Content-Type": "text/xml",
    }

def load_watchlist():
    if WATCHLIST_PATH.exists():
        return json.loads(WATCHLIST_PATH.read_text())
    return []

def save_watchlist(wl):
    WATCHLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    WATCHLIST_PATH.write_text(json.dumps(wl, indent=2))

def _cache_path(item_id: str) -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return CACHE_DIR / f"{item_id}.json"

def fetch_item(item_id: str, use_cache: bool = False):
    cp = _cache_path(item_id)
    if use_cache and cp.exists():
        return json.loads(cp.read_text())
    url = f"{API_ROOT}/item/{item_id}"
    r = httpx.get(url, headers=_headers(), timeout=30)
    r.raise_for_status()
    data = r.json()
    cp.write_text(json.dumps(data, indent=2))
    return data

def get_current_price(data: dict) -> float:
    price_info = data.get("price", {})
    bid = data.get("currentBidPrice", price_info)
    if isinstance(bid, dict):
        return float(bid.get("value", 0))
    return float(price_info.get("value", 0))

def place_bid(item_id: str, max_bid: float):
    xml = f"""<?xml version="1.0" encoding="utf-8"?>
<PlaceOfferRequest xmlns="urn:ebay:apis:eBLBaseComponents">
  <Offer>
    <Action>Bid</Action>
    <MaxBid>{max_bid}</MaxBid>
    <ItemID>{item_id}</ItemID>
    <Quantity>1</Quantity>
  </Offer>
  <EndUserIP>127.0.0.1</EndUserIP>
</PlaceOfferRequest>"""
    r = httpx.post(TRADING_ROOT, headers=_trading_headers("PlaceOffer"), content=xml, timeout=30)
    r.raise_for_status()
    return r.text

def parse_end_time(data: dict) -> datetime:
    raw = data.get("itemEndDate")
    if not raw:
        raise ValueError("no end time in item data")
    return datetime.fromisoformat(raw.replace("Z", "+00:00"))

def snipe_loop(item_id: str, max_bid: float, lead_seconds: float = 5.0, dry_run: bool = False):
    first = True
    while True:
        try:
            data = fetch_item(item_id, use_cache=not first)
        except httpx.HTTPError as e:
            print(f"fetch failed: {e}", file=sys.stderr)
            time.sleep(10)
            first = False
            continue
        first = False
        
        buying_options = data.get("buyingOptions", [])
        if "AUCTION" not in buying_options:
            print(f"item {item_id} is no longer an auction (ended or buy-it-now only)")
            return
        
        current = get_current_price(data)
        if current >= max_bid:
            print(f"current bid ${current:.2f} >= max ${max_bid:.2f}, skipping")
            return
        
        end = parse_end_time(data)
        now = datetime.now(timezone.utc)
        delta = (end - now).total_seconds()
        if delta <= 0:
            print(f"auction for {item_id} already ended")
            return
        
        if delta <= lead_seconds:
            if dry_run:
                print(f"[DRY RUN] would bid ${max_bid:.2f} on {item_id} with {delta:.2f}s left")
                return
            print(f"firing bid now, {delta:.2f}s before close")
            try:
                result = place_bid(item_id, max_bid)
                print(result[:200])
            except httpx.HTTPError as e:
                print(f"bid failed: {e}", file=sys.stderr)
            break
        
        if delta > 300:
            sleep_time = delta - 300
        elif delta > 60:
            sleep_time = 10
        else:
            sleep_time = max(1.0, delta - lead_seconds - 2)
        print(f"ends in {delta:.0f}s (current ${current:.2f}), sleeping {sleep_time:.0f}s")
        time.sleep(sleep_time)

def poll_watchlist(dry_run: bool = False):
    wl = load_watchlist()
    if not wl:
        print("no items in watchlist. add one with 'sniper add <item_id> --max-bid <amount>'")
        return
    for entry in wl:
        item_id = entry["item"]
        max_bid = entry["max_bid"]
        lead = entry.get("lead", 5.0)
        print(f"watching {item_id} @ ${max_bid}")
        snipe_loop(item_id, max_bid, lead, dry_run=dry_run)

def main():
    signal.signal(signal.SIGINT, lambda *_: sys.exit(130))
    
    parser = argparse.ArgumentParser(description="eBay auction sniper")
    sub = parser.add_subparsers(dest="command")
    
    p_add = sub.add_parser("add", help="add item to watchlist")
    p_add.add_argument("item", help="eBay item ID")
    p_add.add_argument("--max-bid", type=float, required=True)
    p_add.add_argument("--lead", type=float, default=5.0)
    
    p_run = sub.add_parser("run", help="run sniper on watchlist")
    p_run.add_argument("--dry-run", action="store_true", help="show what would be bid without bidding")
    
    p_once = sub.add_parser("once", help="snipe single item")
    p_once.add_argument("item", help="eBay item ID")
    p_once.add_argument("--max-bid", type=float, required=True)
    p_once.add_argument("--lead", type=float, default=5.0)
    p_once.add_argument("--dry-run", action="store_true")
    
    p_ls = sub.add_parser("ls", help="list watchlist")
    
    p_rm = sub.add_parser("rm", help="remove item from watchlist")
    p_rm.add_argument("item", help="eBay item ID")
    
    args = parser.parse_args()
    if not USER_TOKEN and not (APP_ID and CERT_ID and REFRESH_TOKEN):
        print("set EBAY_USER_TOKEN or EBAY_APP_ID+EBAY_CERT_ID+EBAY_REFRESH_TOKEN", file=sys.stderr)
        sys.exit(2)
    
    if args.command == "add":
        wl = load_watchlist()
        wl.append({"item": args.item, "max_bid": args.max_bid, "lead": args.lead})
        save_watchlist(wl)
        print(f"added {args.item}")
    elif args.command == "run":
        poll_watchlist(dry_run=args.dry_run)
    elif args.command == "once":
        snipe_loop(args.item, args.max_bid, args.lead, dry_run=args.dry_run)
    elif args.command == "ls":
        wl = load_watchlist()
        for e in wl:
            print(f"{e['item']}  max=${e['max_bid']}  lead={e.get('lead', 5.0)}s")
        if not wl:
            print("watchlist empty")
    elif args.command == "rm":
        wl = load_watchlist()
        new_wl = [e for e in wl if e["item"] != args.item]
        if len(new_wl) == len(wl):
            print(f"item {args.item} not in watchlist")
        else:
            save_watchlist(new_wl)
            print(f"removed {args.item}")
    else:
        parser.print_usage()
        sys.exit(2)

if __name__ == "__main__":
    try:
        sys.exit(main() or 0)
    except KeyboardInterrupt:
        sys.exit(130)
