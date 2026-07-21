from legalrag.retrieval import retrieve


def test_retrieve_limits_results():
    assert len(retrieve("contract", ["contract law", "tort law"], limit=1)) == 1

