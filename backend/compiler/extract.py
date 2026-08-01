"""The fence: the ONE place an LLM turns natural language into draft constraints.

Fenced Generation, concretely:
  * The model's only job is language understanding -- reading "a bag of house
    blend and two boxes of filters, under $30" and naming the fields.
  * Its output is a *draft*. Nothing downstream trusts it. `validate_constraints`
    decides whether it becomes a mandate, and the gate re-checks everything again
    against the confirmed mandate later.
  * The model is never asked to make a judgment call about whether a purchase is
    allowed. That is arithmetic, and arithmetic belongs in code.

The extractor is injected, so tests run fully offline and the network path is a
single swappable object rather than an import buried in a function.
"""

import json

DEFAULT_MODEL = "gpt-5-mini"

SYSTEM_PROMPT = """\
You extract purchase constraints from a natural-language request. You are a parser, \
not an assistant: you do not decide whether the purchase is wise, and you never \
invent limits the user did not state.

Rules:
- Money is a decimal string with two places, e.g. "14.00". Never a number, never a \
currency symbol.
- Copy product ids EXACTLY from the provided catalog. If the user names something \
not in the catalog, omit it rather than guessing a close match.
- max_unit_price: if the user gave a per-item limit, use it. If they only gave a \
total budget, use the catalog price for that item.
- price_ceiling_total: the total the user is willing to spend. If they gave no \
total, sum the per-item limits times quantities.
- quantity: an integer, default 1.
- currency: a 3-letter ISO code. Default "USD" when the user did not say.
- effective_minutes: how long the authorization should stay valid. Default 15.
- substitution_policy: always "none".
- Merchant url must be the https url from the catalog context.

Return only the JSON object described by the schema."""

# Mirrors the v1 mandate `constraints` block. The validator re-checks all of it;
# the schema exists to make the model's output well-shaped, not to be trusted.
CONSTRAINTS_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "merchant",
        "items",
        "price_ceiling_total",
        "currency",
        "effective_minutes",
        "substitution_policy",
    ],
    "properties": {
        "merchant": {
            "type": "object",
            "additionalProperties": False,
            "required": ["name", "url", "country_code_iso2"],
            "properties": {
                "name": {"type": "string"},
                "url": {"type": "string"},
                "country_code_iso2": {"type": "string"},
            },
        },
        "items": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["product_id", "description", "max_unit_price", "quantity"],
                "properties": {
                    "product_id": {"type": "string"},
                    "description": {"type": "string"},
                    "max_unit_price": {"type": "string"},
                    "quantity": {"type": "integer"},
                },
            },
        },
        "price_ceiling_total": {"type": "string"},
        "currency": {"type": "string"},
        "effective_minutes": {"type": "integer"},
        "substitution_policy": {"type": "string"},
    },
}


class ExtractionError(RuntimeError):
    """The model did not return usable JSON."""


def build_user_prompt(intent_text, merchant=None, catalog=None):
    """The prompt carries the request verbatim -- never a paraphrase."""
    parts = [f"User request (verbatim):\n{intent_text}\n"]
    if merchant:
        parts.append(
            "Merchant:\n"
            f"  name: {merchant['name']}\n"
            f"  url: {merchant['url']}\n"
            f"  country_code_iso2: {merchant.get('country_code_iso2', 'US')}\n"
        )
    if catalog:
        lines = "\n".join(
            f"  {p['product_id']} | {p['name']} | {p['price']}" for p in catalog
        )
        parts.append(f"Catalog (product_id | name | price):\n{lines}\n")
    return "\n".join(parts)


class OpenAIExtractor:
    """Calls gpt-5-mini and returns parsed JSON. The only networked object here."""

    def __init__(self, api_key=None, model=DEFAULT_MODEL, client=None):
        self.model = model
        self._client = client
        self._api_key = api_key

    def _get_client(self):
        if self._client is None:
            from openai import OpenAI

            from backend.config import require

            self._client = OpenAI(api_key=self._api_key or require("openai_api_key"))
        return self._client

    def __call__(self, system, user):
        response = self._get_client().chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "mandate_constraints",
                    "strict": True,
                    "schema": CONSTRAINTS_SCHEMA,
                },
            },
        )
        content = response.choices[0].message.content
        try:
            return json.loads(content)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ExtractionError(f"model did not return JSON: {exc}") from exc


def extract_constraints(intent_text, merchant=None, catalog=None, extractor=None):
    """Natural language -> DRAFT constraints. Unvalidated and untrusted by design.

    The caller must pass the result through `validate_constraints`; this function
    deliberately does not, so the fence is visible at the call site rather than
    hidden inside the thing being fenced.
    """
    if not intent_text or not intent_text.strip():
        raise ExtractionError("intent_text is empty")

    extractor = extractor or OpenAIExtractor()
    draft = extractor(SYSTEM_PROMPT, build_user_prompt(intent_text, merchant, catalog))

    if not isinstance(draft, dict):
        raise ExtractionError(f"expected a JSON object, got {type(draft).__name__}")
    return draft
