"""Beanline Coffee — the demo merchant. SPEC §6.

Self-hosted and openly disclosed: sandbox-issued credentials cannot clear a real
merchant's processor, and Prava support confirmed (Jul 29) that a clearly
disclosed self-built storefront is acceptable and will not be treated as a mocked
transaction. Every Prava-side step remains real sandbox.

Markup is plain server-rendered HTML with stable element ids, because the executor
drives this page with Playwright and scrapes it for the E1-E3 pre-check. The ids
below are part of the contract with the executor:

    #page-total                  the order total as rendered
    [data-line-item]             one per cart line, with data-product-id/-unit-price/-quantity
    #card-number #expiry #cvv #cardholder-name #submit-payment
    #order-number                on the confirmation page
    #authorization-code          processor auth code, forwarded to report-status
    #response-code               processor response code ("00" / "05")

Run:  .venv/bin/uvicorn storefront.app:app --port 8200
"""

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from html import escape

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from backend.money import add, multiply, to_string
from storefront.catalog import catalog, drift_banner, drift_mode, resolve_product, set_drift
from storefront.processor import authorize

app = FastAPI(title="Beanline Coffee")

CARTS = {}
ORDERS = {}
CART_COOKIE = "beanline_cart"


# --- rendering ---------------------------------------------------------------

STYLE = """
  body{font:16px/1.5 -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
       max-width:760px;margin:0 auto;padding:32px 20px;color:#1a1a1a}
  h1{font-size:28px;margin:0 0 4px} .sub{color:#666;margin:0 0 28px}
  .card{border:1px solid #e2e2e2;border-radius:10px;padding:16px;margin:0 0 12px}
  .row{display:flex;justify-content:space-between;align-items:center;gap:16px}
  .price{font-variant-numeric:tabular-nums;font-weight:600}
  .blurb{color:#666;font-size:14px;margin:4px 0 0}
  button,input[type=submit]{background:#1a1a1a;color:#fff;border:0;border-radius:8px;
       padding:10px 16px;font-size:15px;cursor:pointer}
  input[type=text]{width:100%;padding:10px;border:1px solid #ccc;border-radius:8px;
       font-size:15px;box-sizing:border-box}
  label{display:block;margin:12px 0 4px;font-size:14px;font-weight:600}
  .total{font-size:22px;font-weight:700;font-variant-numeric:tabular-nums}
  .drift{background:#fff4e5;border:1px solid #ffb547;border-radius:10px;padding:12px 16px;
       margin:0 0 20px;font-size:14px;color:#7a4a00}
  .ok{background:#e8f6ec;border:1px solid #34a853;border-radius:10px;padding:20px}
  .err{background:#fdeaea;border:1px solid #d93025;border-radius:10px;padding:12px 16px;
       margin:0 0 16px;color:#a50e0e}
  a{color:#1a1a1a}
"""


def page(title, body):
    banner = drift_banner()
    banner_html = (
        f'<div class="drift" id="drift-banner">{escape(banner)}</div>' if banner else ""
    )
    return HTMLResponse(
        f"<!doctype html><html><head><meta charset='utf-8'><title>{escape(title)}</title>"
        f"<style>{STYLE}</style></head><body>{banner_html}{body}</body></html>"
    )


# --- cart --------------------------------------------------------------------


def cart_for(request):
    return CARTS.get(request.cookies.get(CART_COOKIE, ""), {})


def cart_lines(cart):
    """Cart contents priced at the store's *current* prices."""
    lines = []
    for product_id, quantity in cart.items():
        product = catalog().get(product_id)
        if not product:
            continue
        lines.append(
            {
                "product_id": product_id,
                "name": product["name"],
                "unit_price": product["price"],
                "quantity": quantity,
                "line_total": to_string(multiply(product["price"], quantity)),
            }
        )
    return sorted(lines, key=lambda line: line["product_id"])


def cart_total(lines):
    total = Decimal("0.00")
    for line in lines:
        total = add(total, line["line_total"])
    return to_string(total)


# --- routes ------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
def index():
    cards = "".join(
        f"""<div class="card">
              <div class="row">
                <div>
                  <strong>{escape(p['name'])}</strong>
                  <p class="blurb">{escape(p['blurb'])}</p>
                  <p class="blurb">id: <code>{escape(p['product_id'])}</code></p>
                </div>
                <div style="text-align:right">
                  <div class="price" data-price-for="{escape(p['product_id'])}">${p['price']}</div>
                  <form method="post" action="/cart/add" style="margin-top:8px">
                    <input type="hidden" name="product_id" value="{escape(p['product_id'])}">
                    <button type="submit" data-add="{escape(p['product_id'])}">Add to cart</button>
                  </form>
                </div>
              </div>
            </div>"""
        for p in catalog().values()
    )
    return page(
        "Beanline Coffee",
        f"<h1>Beanline Coffee</h1><p class='sub'>Demo merchant for Mandate Guard. "
        f"Not a real shop; no real money moves.</p>{cards}"
        f"<p><a href='/cart'>View cart &rarr;</a></p>",
    )


@app.post("/cart/add")
def cart_add(request: Request, product_id: str = Form(...), quantity: int = Form(1)):
    product = resolve_product(product_id)
    if not product:
        return RedirectResponse("/", status_code=303)

    cart_id = request.cookies.get(CART_COOKIE) or str(uuid.uuid4())
    cart = CARTS.setdefault(cart_id, {})
    # Under product_swap this stores the *substituted* id -- honestly reflecting
    # what the store put in the basket, which is what the gate must catch.
    key = product["product_id"]
    cart[key] = cart.get(key, 0) + quantity

    response = RedirectResponse("/cart", status_code=303)
    response.set_cookie(CART_COOKIE, cart_id, httponly=True, samesite="lax")
    return response


@app.post("/cart/clear")
def cart_clear(request: Request):
    CARTS.pop(request.cookies.get(CART_COOKIE, ""), None)
    return RedirectResponse("/", status_code=303)


@app.get("/cart", response_class=HTMLResponse)
def cart_view(request: Request):
    lines = cart_lines(cart_for(request))
    if not lines:
        return page("Cart", "<h1>Cart</h1><p>Empty. <a href='/'>Back to the shop</a></p>")

    rows = "".join(
        f"""<div class="card" data-line-item data-product-id="{escape(l['product_id'])}"
                 data-unit-price="{l['unit_price']}" data-quantity="{l['quantity']}">
              <div class="row">
                <div><strong>{escape(l['name'])}</strong>
                     <p class="blurb">{l['quantity']} &times; ${l['unit_price']}</p></div>
                <div class="price">${l['line_total']}</div>
              </div>
            </div>"""
        for l in lines
    )
    total = cart_total(lines)
    return page(
        "Cart",
        f"<h1>Cart</h1>{rows}"
        f'<div class="row" style="margin-top:20px"><span>Total</span>'
        f'<span class="total" id="page-total">${total}</span></div>'
        f'<p style="margin-top:20px"><a href="/checkout">Continue to checkout &rarr;</a></p>',
    )


@app.get("/checkout", response_class=HTMLResponse)
def checkout_form(request: Request, error: str = ""):
    lines = cart_lines(cart_for(request))
    if not lines:
        return RedirectResponse("/", status_code=303)

    rows = "".join(
        f"""<div class="card" data-line-item data-product-id="{escape(l['product_id'])}"
                 data-unit-price="{l['unit_price']}" data-quantity="{l['quantity']}">
              <div class="row"><div>{escape(l['name'])} &times; {l['quantity']}</div>
              <div class="price">${l['line_total']}</div></div>
            </div>"""
        for l in lines
    )
    total = cart_total(lines)
    error_html = f'<div class="err" id="checkout-error">{escape(error)}</div>' if error else ""

    return page(
        "Checkout",
        f"""<h1>Checkout</h1>{error_html}{rows}
        <div class="row" style="margin:20px 0"><span>Total</span>
          <span class="total" id="page-total">${total}</span></div>
        <form method="post" action="/checkout" id="payment-form">
          <label for="cardholder-name">Name on card</label>
          <input type="text" id="cardholder-name" name="cardholder_name" autocomplete="off">
          <label for="card-number">Card number</label>
          <input type="text" id="card-number" name="card_number" autocomplete="off">
          <label for="expiry">Expiry (MM/YY)</label>
          <input type="text" id="expiry" name="expiry" autocomplete="off">
          <label for="cvv">CVV</label>
          <input type="text" id="cvv" name="cvv" autocomplete="off">
          <p style="margin-top:20px">
            <input type="submit" id="submit-payment" value="Pay ${total}">
          </p>
        </form>""",
    )


@app.post("/checkout")
def checkout_submit(
    request: Request,
    cardholder_name: str = Form(""),
    card_number: str = Form(""),
    expiry: str = Form(""),
    cvv: str = Form(""),
):
    lines = cart_lines(cart_for(request))
    if not lines:
        return RedirectResponse("/", status_code=303)

    decision = authorize(card_number, expiry, cvv, cardholder_name)
    if not decision["approved"]:
        return RedirectResponse(f"/checkout?error={decision['reason']}", status_code=303)

    order_number = f"BL-{uuid.uuid4().hex[:8].upper()}"
    ORDERS[order_number] = {
        "order_number": order_number,
        "placed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "line_items": lines,
        "total": cart_total(lines),
        "authorization_code": decision["authorization_code"],
        "response_code": decision["response_code"],
        # Last 4 only. The full number is never stored, per SPEC §12.
        "card_last4": card_number.replace(" ", "").replace("-", "")[-4:],
    }
    CARTS.pop(request.cookies.get(CART_COOKIE, ""), None)
    return RedirectResponse(f"/order/{order_number}", status_code=303)


@app.get("/order/{order_number}", response_class=HTMLResponse)
def order_confirmation(order_number: str):
    """The completed-checkout proof: the executor screenshots this page."""
    order = ORDERS.get(order_number)
    if not order:
        return page("Not found", "<h1>No such order</h1>")

    rows = "".join(
        f"<div class='row'><div>{escape(l['name'])} &times; {l['quantity']}</div>"
        f"<div class='price'>${l['line_total']}</div></div>"
        for l in order["line_items"]
    )
    return page(
        f"Order {order_number}",
        f"""<div class="ok">
              <h1>Order confirmed</h1>
              <p>Order number: <strong id="order-number">{escape(order_number)}</strong></p>
              <p class="blurb">Authorization
                 <span id="authorization-code">{escape(order['authorization_code'])}</span>
                 &middot; response
                 <span id="response-code">{escape(order['response_code'])}</span>
                 &middot; card ending {escape(order['card_last4'])}</p>
            </div>
            <div style="margin-top:20px">{rows}</div>
            <div class="row" style="margin-top:16px"><span>Total paid</span>
              <span class="total" id="page-total">${order['total']}</span></div>""",
    )


# --- admin (visible on camera) ----------------------------------------------


@app.get("/_admin", response_class=HTMLResponse)
def admin():
    buttons = "".join(
        f"""<form method="post" action="/_admin/drift" style="display:inline-block;margin-right:8px">
              <input type="hidden" name="mode" value="{mode}">
              <button type="submit" id="drift-{mode}">{mode}</button>
            </form>"""
        for mode in ("none", "price_hike", "product_swap")
    )
    return page(
        "Beanline admin",
        f"""<h1>Drift injection</h1>
        <p class='sub'>Perturbs the <em>merchant</em>, never the agent's output.
        Whatever the agent proposes after this is genuinely what it proposed.</p>
        <p>Current mode: <strong id="drift-mode">{escape(drift_mode())}</strong></p>
        <p>{buttons}</p>""",
    )


@app.post("/_admin/drift")
def admin_drift(mode: str = Form(...)):
    try:
        set_drift(mode)
    except ValueError:
        pass
    return RedirectResponse("/_admin", status_code=303)


@app.get("/_admin/state")
def admin_state():
    """Machine-readable view, for tests and the demo control panel."""
    return {"drift": drift_mode(), "orders": len(ORDERS)}
