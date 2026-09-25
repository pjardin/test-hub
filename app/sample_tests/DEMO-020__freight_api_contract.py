"""API testing, beside the browser tests: no page to look at, just requests
and JSON -- the promises a partner integration depends on.

  auth      no key -> 401; a valid key -> 200
  contract  a shipment carries the fields clients read (id, status, eta ...)
  money     an API quote with SPRING10 takes exactly 10% off
  limits    the trial key allows 5 calls a minute; the 6th gets 429 with a
            Retry-After header

It checks EVERYTHING and reports every broken promise together (soft
assertions): when a release breaks the API three ways, you want to read all
three in one run, not fix-rerun-fix-rerun. page.request is Playwright's HTTP
client -- same cookies and proxy as the browser, no page load needed.

The key comes from Settings -> Credentials ("freight_api_key") when set, so
a real key never has to be written into a test file.
"""
from _lib.freight import Freight

DEMO_KEY = "acme-demo-7f3a91c2"          # published on the demo's developers page
TRIAL_KEY = "acme-trial-2b91e4d0"        # 5 requests per minute
CONTRACT = {"id": str, "status": str, "origin": str, "destination": str,
            "service": str, "weight_kg": (int, float), "eta": str, "events": list}


def run(page, ctx):
    api = Freight(page, ctx).url("api/v1/")
    key = {"X-API-Key": ctx.secret("freight_api_key", DEMO_KEY)}
    problems = []

    with ctx.timed("auth: no key"):
        r = page.request.get(api + "shipments/AF-100001")
    if r.status != 401:
        problems.append(f"auth: a request with no key should get 401, got {r.status}")

    with ctx.timed("GET a shipment"):
        r = page.request.get(api + "shipments/AF-100001", headers=key)
    if r.status != 200:
        problems.append(f"GET shipments/AF-100001 answered {r.status}: {r.text()[:200]}")
    else:
        body = r.json()
        for field, kind in CONTRACT.items():
            if field not in body:
                problems.append(f"contract: field '{field}' is missing "
                                f"(the response has: {', '.join(sorted(body))})")
            elif not isinstance(body[field], kind):
                problems.append(f"contract: '{field}' should be {kind}, got {body[field]!r}")

    with ctx.timed("POST a quote with SPRING10"):
        r = page.request.post(api + "quotes", headers=key, data={
            "origin": "San Francisco", "destination": "San Diego",
            "weight_kg": 120, "service": "standard", "promo": "SPRING10"})
    if r.status != 200:
        problems.append(f"POST quotes answered {r.status}: {r.text()[:200]}")
    else:
        quote = r.json()
        subtotal, discount = float(quote["subtotal"]), float(quote["discount"])
        if abs(discount - round(subtotal * 0.10, 2)) > 0.011:
            problems.append(f"money: SPRING10 took ${discount:.2f} off a ${subtotal:.2f} "
                            f"subtotal -- 10% is ${subtotal * 0.10:.2f}")

    with ctx.timed("rate limit: 6 calls on the trial key"):
        answers = [page.request.get(api + "shipments/AF-100002",
                                    headers={"X-API-Key": TRIAL_KEY}) for _ in range(6)]
    codes = [a.status for a in answers]
    if codes[-1] != 429:
        problems.append(f"limits: the trial key allows 5 calls a minute, but 6 in a row "
                        f"answered {codes}")
    elif not answers[-1].headers.get("retry-after"):
        problems.append("limits: a 429 must say when to retry (Retry-After header)")

    ctx.log(f"{len(problems)} broken promise(s)")
    assert not problems, "the API broke its promises:\n  - " + "\n  - ".join(problems)
