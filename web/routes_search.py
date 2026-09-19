"""Global web search across stock, orders, partners, and employees."""
from urllib.parse import urlencode

from fastapi import APIRouter, Request

from services import auth
from services import search as search_service
from web.context import redirect, render, require_login, resolve_user

router = APIRouter()


def result_url(result, context) -> str:
    """Return the most useful existing web screen for a search result."""
    record = result.payload
    if result.kind == search_service.ITEM:
        params = {"q": record.sku}
        if not record.is_active:
            params["inactive"] = 1
        return "/inventory?" + urlencode(params)
    if result.kind == search_service.SALES_ORDER:
        return f"/shipping/{record.id}"
    if result.kind == search_service.PURCHASE_ORDER:
        return f"/receiving/{record.id}"
    if result.kind == search_service.CUSTOMER_RETURN:
        return f"/operations/returns/customer/{record.id}"
    if result.kind == search_service.SUPPLIER_RETURN:
        return f"/operations/returns/supplier/{record.id}"
    if result.kind == search_service.CUSTOMER:
        return "/shipping?" + urlencode({"q": record.name})
    if result.kind == search_service.SUPPLIER:
        return "/receiving?" + urlencode({"q": record.name})
    if result.kind == search_service.EMPLOYEE:
        if context.can(auth.PERM_MANAGE_EMPLOYEES):
            return "/settings?" + urlencode({"tab": "employees"})
        return "/"
    return "/"


@router.get("/search")
def global_search(request: Request, q: str = ""):
    context = resolve_user(request)
    blocked = require_login(context)
    if blocked is not None:
        return blocked

    term = (q or "").strip()[:100]
    error = None
    results = []
    if term:
        scanned = search_service.scanned_item(term)
        if scanned is not None:
            direct = search_service.Result(
                search_service.ITEM, scanned.name, scanned.sku, scanned, exact=True)
            return redirect(context, result_url(direct, context))
        if len(term) < 2:
            error = "Enter at least two characters."
        else:
            results = search_service.search(term, limit_per_type=8)
            exact = [result for result in results if result.exact]
            if len(exact) == 1:
                return redirect(context, result_url(exact[0], context))

    rows = [
        {"result": result, "url": result_url(result, context)}
        for result in results
    ]
    return render(
        context, "search.html", search_query=term, rows=rows,
        summary=search_service.summarise(results), error=error,
    )
