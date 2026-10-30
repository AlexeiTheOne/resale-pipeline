"""llm.LazyClient: Gemini clients are built on first use, so imports need no key."""
import llm


def test_lazy_client_builds_once_on_first_use(monkeypatch):
    built = []

    class Fake:
        models = "models-api"

    def fake_make_client(**kwargs):
        built.append(kwargs)
        return Fake()

    monkeypatch.setattr(llm, "make_client", fake_make_client)
    c = llm.LazyClient(api_key="k")
    assert built == []
    assert c.models == "models-api"
    assert c.models == "models-api"
    assert built == [{"api_key": "k"}]


def test_pipeline_modules_import_without_keys():
    import identify
    from pipeline import draft
    assert isinstance(identify.client, llm.LazyClient)
    assert isinstance(draft.client, llm.LazyClient)
