"""Supplier adapter interface (ProdSeller model).

The bot works fully on local stock. If the admin later configures a supplier
base URL + API key (Admin panel -> Settings -> Supplier), stock and prices can
be pulled from the supplier automatically. Until then the stub is used: it
logs the call and falls back to local stock so nothing breaks.
"""
import logging
from abc import ABC, abstractmethod

log = logging.getLogger(__name__)


class SupplierAdapter(ABC):
    """Interface every supplier implementation must follow."""

    name: str = "base"

    @abstractmethod
    async def get_products(self) -> list[dict] | None:
        """Return supplier catalog: [{'sku': str, 'name': str, 'price': float, 'stock': int}].
        Return None when the supplier cannot provide a catalog."""

    @abstractmethod
    async def get_price(self, sku: str) -> float | None:
        """Return current supplier price for a SKU, or None if unavailable."""

    @abstractmethod
    async def place_order(self, sku: str, qty: int) -> list[str] | None:
        """Order qty units from the supplier. Returns serials, or None if
        the supplier cannot fulfill (caller falls back to local stock)."""

    @abstractmethod
    async def check_order(self, ref: str) -> dict | None:
        """Return supplier order status dict, or None if unknown."""


class StubSupplier(SupplierAdapter):
    """Default: no supplier configured. Logs and falls back to local stock."""

    name = "stub"

    async def get_products(self) -> list[dict] | None:
        log.info("StubSupplier.get_products: no supplier configured, using local stock")
        return None

    async def get_price(self, sku: str) -> float | None:
        log.info("StubSupplier.get_price(%s): no supplier configured, using local price", sku)
        return None

    async def place_order(self, sku: str, qty: int) -> list[str] | None:
        log.info("StubSupplier.place_order(%s, %s): no supplier configured, using local stock",
                 sku, qty)
        return None

    async def check_order(self, ref: str) -> dict | None:
        log.info("StubSupplier.check_order(%s): no supplier configured", ref)
        return None


class HttpSupplier(SupplierAdapter):
    """Generic HTTP supplier using the same contract as this bot's own api.py:

        GET  {base}/products                 (X-API-Key)
        GET  {base}/products/{id}/price       (X-API-Key)
        POST {base}/orders  {product_id, qty} (X-API-Key)
        GET  {base}/orders/{id}               (X-API-Key)

    `sku` values are the remote product IDs (stored locally in products.supplier_sku).
    """

    name = "http"

    def __init__(self, base_url: str, api_key: str):
        import httpx  # local import so the stub path never needs it
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self._client = httpx.AsyncClient(timeout=20.0)

    def _headers(self) -> dict:
        return {"X-API-Key": self.api_key}

    async def get_products(self) -> list[dict] | None:
        try:
            r = await self._client.get(f"{self.base_url}/products", headers=self._headers())
            r.raise_for_status()
            items = r.json()
            return [{"sku": str(p["id"]), "name": p["name"],
                     "price": float(p["price"]), "stock": int(p.get("stock", 0))}
                    for p in items]
        except Exception as e:
            log.warning("HttpSupplier.get_products failed: %s", e)
            return None

    async def get_price(self, sku: str) -> float | None:
        try:
            r = await self._client.get(f"{self.base_url}/products/{sku}/price",
                                       headers=self._headers())
            r.raise_for_status()
            return float(r.json()["price"])
        except Exception as e:
            log.warning("HttpSupplier.get_price(%s) failed: %s", sku, e)
            return None

    async def place_order(self, sku: str, qty: int) -> list[str] | None:
        try:
            try:
                pid = int(sku)
            except (TypeError, ValueError):
                return None
            r = await self._client.post(f"{self.base_url}/orders", headers=self._headers(),
                                        json={"product_id": pid, "qty": qty})
            r.raise_for_status()
            return list(r.json().get("serials", []))
        except Exception as e:
            log.warning("HttpSupplier.place_order(%s, %s) failed: %s", sku, qty, e)
            return None

    async def check_order(self, ref: str) -> dict | None:
        try:
            r = await self._client.get(f"{self.base_url}/orders/{ref}", headers=self._headers())
            r.raise_for_status()
            return r.json()
        except Exception as e:
            log.warning("HttpSupplier.check_order(%s) failed: %s", ref, e)
            return None


def get_supplier(settings: dict) -> SupplierAdapter:
    """Pick the supplier implementation from admin settings."""
    base_url = (settings.get("supplier_base_url") or "").strip()
    api_key = (settings.get("supplier_api_key") or "").strip()
    if base_url and api_key:
        return HttpSupplier(base_url, api_key)
    return StubSupplier()


async def sync_supplier_stock(db, supplier: SupplierAdapter) -> dict:
    """Pull supplier catalog and update local price/stock for products that
    have a supplier_sku set. Returns a summary dict."""
    catalog = await supplier.get_products()
    if catalog is None:
        return {"synced": False, "reason": "no supplier configured (using local stock)"}
    by_sku = {c["sku"]: c for c in catalog}
    updated, skipped = 0, 0
    for p in await db.list_products(active_only=False):
        sku = (p.get("supplier_sku") or "").strip() if isinstance(p, dict) else ""
        # list_products doesn't include sku; fetch full product
        full = await db.get_product(p["id"])
        sku = (full.get("supplier_sku") or "").strip()
        if not sku or sku not in by_sku:
            skipped += 1
            continue
        item = by_sku[sku]
        await db.update_product(p["id"], price=item["price"])
        # top-up local stock to supplier's reported stock level
        local = await db.count_stock(p["id"])
        if item["stock"] > local:
            await db.add_stock(p["id"], [f"SUP-{sku}-{i}" for i in range(local + 1, item["stock"] + 1)])
        updated += 1
    return {"synced": True, "updated": updated, "skipped": skipped,
            "supplier": supplier.name}
