"""Minimal T0 retrieval fixture."""


def retrieve(query: str, documents: list[str], limit: int = 3) -> list[str]:
    terms = set(query.lower().split())
    ranked = sorted(documents, key=lambda doc: len(terms & set(doc.lower().split())), reverse=True)
    return ranked[:limit]

