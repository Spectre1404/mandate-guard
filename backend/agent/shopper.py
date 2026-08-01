"""Shopping agent — SPEC §2.

Proposes a transaction. Holds no credentials, cannot call Prava, and cannot
approve anything. Its output is a *proposal*: an input to the gate, never an
instruction to spend.

Fenced Generation applies here in a specific way. Deciding **what to buy** is
mechanical -- match the mandate's product ids against the merchant's catalog and
read the prices off it -- so code does that, and the numbers in the proposal are
copied from the catalog rather than written by a model. The LLM contributes one
thing: a short natural-language `rationale` explaining the choice, which is the
part a human actually wants to read and the part a model is good at.

That split matters for the demo. When the drift toggle perturbs the store, the
agent genuinely proposes the perturbed cart -- because it genuinely read those
prices -- and the gate genuinely catches it. Nothing is staged.
"""

import uuid
from datetime import timezone

from backend.money import to_string, total_of

FALLBACK_RATIONALE = "Selected the mandated items at the merchant's listed prices."


class ShoppingError(RuntimeError):
    """The agent could not build a proposal at all."""


def _iso(dt):
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def propose(mandate, catalog, created_at, rationale_writer=None, model="deterministic"):
    """Build a proposed transaction from the mandate and the merchant's catalog.

    `catalog` is `{product_id: {product_id, name, price}}` as the store currently
    serves it -- drift included. The agent reports what it found; it does not
    correct the store, and it does not check the result against the mandate. That
    is the gate's job, and keeping it there is the whole point.
    """
    if not catalog:
        raise ShoppingError("merchant catalog is empty")

    line_items = []
    missing = []
    for item in mandate["constraints"]["items"]:
        product = catalog.get(item["product_id"])
        if product is None:
            # v1 has no substitution policy: report the gap rather than improvise.
            missing.append(item["product_id"])
            continue
        line_items.append(
            {
                "product_id": product["product_id"],
                "description": product["name"],
                "unit_price": product["price"],
                "quantity": item["quantity"],
            }
        )

    if not line_items:
        raise ShoppingError(f"no mandated products are available: {missing}")

    proposed_total = to_string(total_of(line_items))
    merchant = mandate["constraints"]["merchant"]

    return {
        "proposal_id": str(uuid.uuid4()),
        "mandate_id": mandate["mandate_id"],
        "created_at": _iso(created_at),
        "merchant": {"name": merchant["name"], "url": merchant["url"]},
        "line_items": line_items,
        "proposed_total": proposed_total,
        "agent_meta": {
            "model": model,
            "rationale": _rationale(
                mandate, line_items, proposed_total, missing, rationale_writer
            ),
            "unavailable_product_ids": missing,
        },
    }


def _rationale(mandate, line_items, proposed_total, missing, rationale_writer):
    """One or two sentences of plain English. Never load-bearing.

    A failure here must not stop a purchase being proposed, so any error from the
    model falls back to a fixed sentence. The rationale is commentary on a
    decision already made deterministically.
    """
    if rationale_writer is None:
        return FALLBACK_RATIONALE
    try:
        text = rationale_writer(
            intent_text=mandate["intent_text"],
            line_items=line_items,
            proposed_total=proposed_total,
            currency=mandate["constraints"]["currency"],
            unavailable_product_ids=missing,
        )
    except Exception:
        return FALLBACK_RATIONALE
    return (text or "").strip() or FALLBACK_RATIONALE


class OpenAIRationaleWriter:
    """Writes the rationale sentence. The only model call in the agent."""

    SYSTEM = (
        "You explain, in at most two plain sentences, why an autonomous shopping "
        "agent selected these items. State only what the data shows. Do not claim "
        "the purchase is approved, verified, safe, or within budget -- a separate "
        "verification step decides that, and it has not run yet."
    )

    def __init__(self, api_key=None, model="gpt-5-mini", client=None):
        self.model = model
        self._client = client
        self._api_key = api_key

    def _get_client(self):
        if self._client is None:
            from openai import OpenAI

            from backend.config import require

            self._client = OpenAI(api_key=self._api_key or require("openai_api_key"))
        return self._client

    def __call__(self, intent_text, line_items, proposed_total, currency, unavailable_product_ids):
        lines = "\n".join(
            f"- {item['description']} x{item['quantity']} at {item['unit_price']}"
            for item in line_items
        )
        user = (
            f"User asked for:\n{intent_text}\n\n"
            f"Agent selected:\n{lines}\n\n"
            f"Total: {proposed_total} {currency}\n"
            f"Unavailable: {unavailable_product_ids or 'none'}"
        )
        response = self._get_client().chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": self.SYSTEM},
                {"role": "user", "content": user},
            ],
        )
        return response.choices[0].message.content
