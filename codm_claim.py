#!/usr/bin/env python3
"""
CODM Web Store — daily Secret Cache (DAILY GIFT) auto-claimer.

Reverse-engineered from the live store frontend (store.callofdutymobile.com,
Nuxt bundle, July 2026). All endpoints verified read-only against production.

Flow (mirrors the browser exactly):
  1. validate      POST https://order-{REGION}.codashop.com/validate
                   resolves a Player ID / short ID to a profile
  2. productPage   POST https://shopapi.codashop.com/productPage
                   -> product info (lvtId, gvtId, voucherTypeId, ...)
  3. dynamicSkuInfo POST https://shopapi.codashop.com/productPage/dynamicSkuInfo
                   -> SKU list incl. FREEBIE skus + their claimUrlEndpoint (BuyUrl)
  4. createOrderToken POST https://shopapi.codashop.com/productPage/createOrderToken
                   -> dynamicSkuToken (JWT) for the freebie sku
  5. claim         POST <sku.BuyUrl>  (application/x-www-form-urlencoded)
                   body: shopLang, user.userId, user.zoneId, checkoutId (fresh uuid),
                         dynamicSkuToken, status, lvtId, skuId, pricingScheme,
                         gvtId, voucherTypeId, voucherTypeName, callOrderAPI

Claim RESULT_CODE values (from the store's own error map):
    0    SUCCESS
    1201 ALREADY_CLAIMED
    1202 VALIDITY_EXPIRED
    1203 NOT_ELIGIBLE
    1210 PUBLISHER_SERVICE_ERROR
    3009 INVALID_REGION

Exit codes (cron-friendly):
    0  claimed, or already claimed today (nothing to do -> not an error)
    2  no claimable freebie right now (e.g. already claimed, sold out)
    3  request/network failure or unknown response

Usage:
    python3 codm_claim.py <PLAYER_ID> [--country IN] [--region sa]
                          [--dry-run] [--json] [--status A] [--all]

Notes:
    - PLAYER_ID = your in-game Player ID or short ID (the one the store asks for).
      Only ever run this against your own account: the claim is irreversible
      and consumes that account's daily limit.
    - --dry-run does steps 1-4 and prints the exact claim request without
      POSTing it (safe to re-run).
    - --status: "A" mirrors the current frontend (first char of the SKU status).
      The older published implementation used "1"; pass --status 1 if the
      backend ever rejects "A".
    - Region "sa" is correct for India (locale en-in -> regionCode sa).
"""

import argparse
import json
import sys
import uuid
import urllib.request
import urllib.parse
import urllib.error

UA = ("Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Mobile Safari/537.36")

LEAP = "https://shopapi.codashop.com"
SHOP_LANG = "en_in"          # locale en-in -> en_in (frontend: replace('-','_'))
WHITELABEL_ID = 1            # COD:M store
VOUCHER_TYPE = "CALL_OF_DUTY_MOBILE_WL"
DAILY_REFRESH_RATE = 86400   # daily gift refresh (seconds)

RESULT_CODES = {
    0: "SUCCESS",
    1201: "ALREADY_CLAIMED",
    1202: "VALIDITY_EXPIRED",
    1203: "NOT_ELIGIBLE",
    1210: "PUBLISHER_SERVICE_ERROR",
    3009: "INVALID_REGION",
}


def http_json(url, payload, headers=None):
    h = {"Content-Type": "application/json", "User-Agent": UA}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers=h, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read().decode("utf-8", "ignore"))
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8", "ignore"))
        except Exception:
            return e.code, {}
    except Exception as e:
        return 0, {"_network_error": str(e)}


def leap_headers(country):
    return {
        "Accept-Language": "en-in",
        "X-EXPT-TOKEN": "",
        "X-EXPT-CONTEXT": "",
        "X-WHITELABEL-ID": str(WHITELABEL_ID),
        "X-SESSION-COUNTRY2NAME": country.upper(),
    }


def validate(player_id, country, region):
    """Resolve player id -> profile. Handles -200 home-base redirect."""
    url = f"https://order-{region}.codashop.com/validate"
    payload = {
        "country": country.upper(),
        "voucherTypeName": VOUCHER_TYPE,
        "whiteLabelId": str(WHITELABEL_ID),
        "deviceId": str(uuid.uuid4()),
        "userId": player_id,
        "zoneId": "",
    }
    st, data = http_json(url, payload, headers={"Accept-Language": ""})
    if st != 200:
        return None, f"validate HTTP {st}: {json.dumps(data)[:300]}"
    if data.get("errorCode") == -200:                      # wrong-region redirect
        home = data.get("homeBaseCountry2Name")
        if home:
            return validate(player_id, home, region), None
        return None, "validate: -200 but no homeBaseCountry2Name"
    if data.get("success") is False:
        return None, f"validate failed: {data.get('errorMsg') or data.get('errorCode')}"
    result = data.get("result") or {}
    return {
        "username": result.get("username") or result.get("nickname"),
        "shortId": result.get("shortId"),
        "picUrl": result.get("picUrl"),
        "levelImage": result.get("customLevelImageUrl"),
        "rank": result.get("customReadableMpRank"),
        "rankImage": result.get("customMpRankImageUrl"),
    }, None


def product_page(country):
    st, data = http_json(
        LEAP + "/productPage",
        {"productPath": "/in/codm", "locale": "en-in", "whitelabelId": WHITELABEL_ID},
        headers=leap_headers(country),
    )
    if st != 200:
        return None, f"productPage HTTP {st}: {json.dumps(data)[:300]}"
    pi = data.get("productInfo") or {}
    if not pi:
        return None, f"productPage: no productInfo in {json.dumps(data)[:300]}"
    # first usable payment channel (needed by createOrderToken)
    pc = None
    channels = data.get("paymentChannels") or []
    if channels:
        pc = channels[0].get("id")
    if pc is None:
        for sku in data.get("skus") or []:
            prices = ((sku.get("pricing") or {}).get("paymentChannelPrices") or {})
            if prices:
                pc = next(iter(prices))
                break
    return {
        "productUrl": pi.get("productUrl", "/in/codm"),
        "lvtId": pi.get("id"),
        "gvtId": pi.get("gvtId"),
        "voucherTypeId": pi.get("voucherTypeId"),
        "voucherTypeName": pi.get("voucherTypeName"),
        "paymentChannelId": pc or 391,
    }, None


def dynamic_sku_info(country, device_id, user_id, prod):
    st, data = http_json(
        LEAP + "/productPage/dynamicSkuInfo",
        {
            "deviceId": device_id,
            "whitelabelId": WHITELABEL_ID,
            "userId": user_id,
            "serverId": "",
            "characterId": "",
            "worldId": "",
            "locale": "en-in",
            "productPath": prod["productUrl"],
        },
        headers=leap_headers(country),
    )
    if st != 200:
        return None, None, f"dynamicSkuInfo HTTP {st}: {json.dumps(data)[:300]}"
    skus = data.get("skus") or []
    freebies = []
    for s in skus:
        scheme = ((s.get("pricing") or {}).get("pricingScheme") or "").upper()
        if scheme != "FREEBIE" or "claim" not in (s.get("BuyUrl") or ""):
            continue
        lim = s.get("PurchaseLimit") or {}
        freebies.append({
            "skuId": s.get("Id"),
            "name": s.get("SkuName"),
            "status": s.get("Status"),
            "buyUrl": s.get("BuyUrl"),
            "remaining": lim.get("limitRemaining"),
            "limit": lim.get("limit"),
            "refreshRate": lim.get("refreshRate"),
            "refreshAtUnix": lim.get("refreshAtUnix"),
        })
    return data, freebies, None


def create_order_token(country, prod, sku, page_lock_token):
    st, data = http_json(
        LEAP + "/productPage/createOrderToken",
        {
            "pageLockToken": page_lock_token,
            "productPath": prod["productUrl"],
            "skuId": sku["skuId"],
            "paymentChannelId": prod["paymentChannelId"],
            "whitelabelId": WHITELABEL_ID,
        },
        headers=leap_headers(country),
    )
    if st != 200:
        return None, f"createOrderToken HTTP {st}: {json.dumps(data)[:300]}"
    token = data.get("dynamicSkuToken")
    if not token:
        return None, f"createOrderToken: no dynamicSkuToken in {json.dumps(data)[:200]}"
    return token, None


def build_claim_form(prod, sku, user_id, token, status):
    return {
        "shopLang": SHOP_LANG,
        "user.userId": user_id,
        "user.zoneId": "",
        "checkoutId": str(uuid.uuid4()),          # fresh per claim, like the browser
        "dynamicSkuToken": token,
        "status": status,                          # "A" = ACTIVE (current frontend)
        "lvtId": str(prod["lvtId"]),
        "skuId": sku["skuId"],
        "pricingScheme": "freebie",
        "gvtId": str(prod["gvtId"]),
        "voucherTypeId": str(prod["voucherTypeId"]),
        "voucherTypeName": prod["voucherTypeName"],
        "callOrderAPI": "false",
    }


def claim(sku, form):
    body = urllib.parse.urlencode(form).encode()
    req = urllib.request.Request(
        sku["buyUrl"], data=body, method="POST",
        headers={
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "User-Agent": UA,
            "Accept": "application/json, text/plain, */*",
            "Referer": "https://store.callofdutymobile.com/",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read().decode("utf-8", "ignore")
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "ignore")
    except Exception as e:
        return 0, {"_network_error": str(e)}
    try:
        return 200, json.loads(raw)
    except Exception:
        return 200, {"_raw": raw[:500]}


def pick_freebie(freebies, claim_all):
    """Priority: daily gift (refresh 86400) first, then any other available."""
    avail = [f for f in freebies if f["status"] == "ACTIVE" and f["remaining"]]
    avail.sort(key=lambda f: (f["refreshRate"] != DAILY_REFRESH_RATE, f["remaining"] == 0))
    if claim_all:
        return avail
    return avail[:1]


def main():
    ap = argparse.ArgumentParser(description="Claim CODM store DAILY GIFT (secret cache)")
    ap.add_argument("player_id", help="your in-game Player ID or short ID")
    ap.add_argument("--country", default="IN", help="home country code (default IN)")
    ap.add_argument("--region", default="sa", help="codashop region (default sa)")
    ap.add_argument("--dry-run", action="store_true", help="verify + print, do not POST claim")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--status", default=None, help="claim status param (default: first char of SKU status; repo-era value: 1)")
    ap.add_argument("--all", action="store_true", help="claim every available freebie, not just the daily gift")
    args = ap.parse_args()

    out = {"player_id": args.player_id, "region": args.region, "country": args.country.upper()}
    exit_code = 0

    # 1) resolve player
    profile, err = validate(args.player_id, args.country, args.region)
    if err:
        out.update({"ok": False, "step": "validate", "error": err})
        print(json.dumps(out, indent=2) if args.json else err)
        return 3
    out["profile"] = profile
    claim_user = profile["shortId"] or args.player_id

    # 2) product info
    prod, err = product_page(args.country)
    if err:
        out.update({"ok": False, "step": "productPage", "error": err})
        print(json.dumps(out, indent=2) if args.json else err)
        return 3

    # 3) skus
    device_id = str(uuid.uuid4())
    sku_user = profile["shortId"] or args.player_id.upper()  # browser uppercases raw input
    data, freebies, err = dynamic_sku_info(args.country, device_id, sku_user, prod)
    if err:
        out.update({"ok": False, "step": "dynamicSkuInfo", "error": err})
        print(json.dumps(out, indent=2) if args.json else err)
        return 3

    targets = pick_freebie(freebies, args.all)
    out["freebies"] = freebies
    if not targets:
        msg = "no claimable freebie: "
        if not freebies:
            msg += "no FREEBIE skus returned by the store"
        else:
            states = ", ".join(f"{f['name']} ({f['status']}, remaining {f['remaining']})" for f in freebies)
            msg += states
        out.update({"ok": False, "step": "select", "error": msg})
        print(json.dumps(out, indent=2) if args.json else msg)
        return 2

    # 4) token + 5) claim
    results = []
    for sku in targets:
        token, err = create_order_token(args.country, prod, sku, data.get("pageLockToken") or "")
        if err:
            results.append({"skuId": sku["skuId"], "ok": False, "step": "createOrderToken", "error": err})
            exit_code = exit_code or 3
            continue
        form = build_claim_form(prod, sku, claim_user, token,
                                args.status or (sku["status"] or "ACTIVE")[0])
        if args.dry_run:
            results.append({
                "skuId": sku["skuId"], "name": sku["name"], "ok": True,
                "dry_run": True, "claim_url": sku["buyUrl"], "form": form,
                "next_refresh_unix": sku["refreshAtUnix"],
            })
            continue
        _, resp = claim(sku, form)
        code = resp.get("RESULT_CODE")
        code_name = RESULT_CODES.get(code, f"UNKNOWN({code})")
        results.append({
            "skuId": sku["skuId"], "name": sku["name"], "ok": code == 0,
            "result_code": code, "result": code_name,
            "response": {k: v for k, v in resp.items() if k in ("RESULT_CODE", "errorMsg", "errorCode", "message", "orderId")},
        })
        if code == 0:
            continue
        if code == 1201:      # already claimed -> not an error for cron
            exit_code = exit_code or 0
        else:
            exit_code = 3

    out.update({"ok": all(r.get("ok", False) for r in results), "claims": results})
    if args.json:
        print(json.dumps(out, indent=2))
    else:
        for r in results:
            if r.get("dry_run"):
                print(f"[dry-run] would claim {r['name']} ({r['skuId']}) -> {r['claim_url']}")
                print("          form:", json.dumps(r["form"]))
            else:
                print(f"{r['name']}: {r['result']}" + (f" ({r['response']})" if r["response"] else ""))
        if profile:
            print(f"player: {profile.get('username') or '?'}" + (f" [{profile['shortId']}]" if profile.get("shortId") else ""))
        if results and results[0].get("next_refresh_unix"):
            import datetime
            print("next daily refresh (UTC):",
                  datetime.datetime.fromtimestamp(results[0]["next_refresh_unix"], datetime.timezone.utc).isoformat())
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
