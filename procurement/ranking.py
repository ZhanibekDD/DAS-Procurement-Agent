from __future__ import annotations

from decimal import Decimal


def rank_quotes(rows: list[dict]) -> list[dict]:
    """Rank compliant quotes by the approved price, delivery and VAT policy."""
    eligible = [row for row in rows if row["compliant"] and row["total_cost"] > 0]
    min_total = min((Decimal(str(row["total_cost"])) for row in eligible), default=None)

    ranked: list[dict] = []
    for row in rows:
        result = dict(row)
        if not result["compliant"] or min_total is None or result["total_cost"] <= 0:
            result["score"] = 0.0
            result["rank_status"] = "disqualified"
        else:
            total = Decimal(str(result["total_cost"]))
            price_score = float(min_total / total) * 60.0
            delivery_score = max(
                0.0, 25.0 - min(float(result["lead_days"]), 60.0) * (25.0 / 60.0)
            )
            vat_score = 15.0 if result["vat_included"] else 0.0
            result["score_breakdown"] = {
                "price": round(price_score, 2),
                "delivery": round(delivery_score, 2),
                "vat": vat_score,
            }
            result["score"] = round(price_score + delivery_score + vat_score, 2)
            result["rank_status"] = "eligible"
        ranked.append(result)

    ranked.sort(key=lambda row: (row["rank_status"] != "eligible", -row["score"], row["total_cost"]))
    for index, row in enumerate((r for r in ranked if r["rank_status"] == "eligible"), start=1):
        row["rank"] = index
    return ranked
