"""Beanline Coffee catalog and the drift toggle — SPEC §6.

Three products with stable ids (all well under Prava's 50-char product_id cap).
`BL-HOUSE-12` is the mandate target in the demo.

The drift toggle perturbs *the merchant*, honestly and visibly. It never edits the
agent's output: the agent still shops the store as it finds it, and the gate still
sees whatever the agent genuinely proposed. That distinction is the whole point of
the fail demo -- we are simulating a merchant-side or agent-side error, not faking
a verdict. The storefront renders a banner whenever drift is active so it is
unmissable on camera.
"""

PRODUCTS = {
    "BL-HOUSE-12": {
        "product_id": "BL-HOUSE-12",
        "name": "Beanline House Blend 12oz",
        "price": "12.50",
        "blurb": "Everyday medium roast. Chocolate, almond, no drama.",
    },
    "BL-FILTER-100": {
        "product_id": "BL-FILTER-100",
        "name": "Paper Filters 100ct",
        "price": "7.25",
        "blurb": "Unbleached #4 cone filters.",
    },
    "BL-DECAF-12": {
        "product_id": "BL-DECAF-12",
        "name": "Beanline Decaf 12oz",
        "price": "13.75",
        "blurb": "Swiss water process. Tastes like coffee, sleeps like a log.",
    },
}

DRIFT_MODES = ("none", "price_hike", "product_swap")

# Module-level so the admin route and the storefront share one view of the world.
STATE = {"drift": "none"}


def set_drift(mode):
    if mode not in DRIFT_MODES:
        raise ValueError(f"unknown drift mode: {mode!r}")
    STATE["drift"] = mode
    return STATE["drift"]


def drift_mode():
    return STATE["drift"]


def drift_banner():
    """Human-readable disclosure rendered on every page while drift is active."""
    return {
        "none": None,
        "price_hike": (
            "SIMULATED MERCHANT ERROR: House Blend is being served at an inflated "
            "price. Injected deliberately for the failure demo."
        ),
        "product_swap": (
            "SIMULATED AGENT ERROR: the House Blend listing now steers to Decaf. "
            "Injected deliberately for the failure demo."
        ),
    }[STATE["drift"]]


def catalog():
    """The catalog as the storefront currently serves it, drift included."""
    products = {pid: dict(product) for pid, product in PRODUCTS.items()}

    if STATE["drift"] == "price_hike":
        products["BL-HOUSE-12"]["price"] = "16.90"

    return products


def resolve_product(product_id):
    """What the store actually gives you when you ask for a product id.

    Under `product_swap` a request for House Blend yields Decaf -- the substitution
    an agent might make without noticing, and exactly what gate rule R2 exists to
    catch.
    """
    products = catalog()
    if STATE["drift"] == "product_swap" and product_id == "BL-HOUSE-12":
        return products["BL-DECAF-12"]
    return products.get(product_id)
