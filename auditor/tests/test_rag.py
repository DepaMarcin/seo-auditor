"""Testy RAGEngine: wyszukiwanie kontekstu, generowanie rekomendacji i - najważniejsze -
odporność na błędy zewnętrznych usług (OpenAI 403/model_not_found, awaria ChromaDB,
brak klucza OPENAI_API_KEY). Żaden z tych scenariuszy nie może rzucić wyjątku ani
wywołać prawdziwego połączenia sieciowego - wszystkie zależności (ChatOpenAI,
OpenAIEmbeddings, chroma_collection) są tu w pełni zamockowane.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from django.test import TestCase

from auditor.models import KnowledgeDocument
from auditor.services.rag import (
    COLLECTION_NAME,
    COMPLEX_METRICS,
    MODEL_PROSTY,
    MODEL_ZLOZONY,
    RAGEngine,
    get_model_for_metric,
)


class OpenAIPermissionDeniedError(Exception):
    """Symuluje openai.PermissionDeniedError (403 - brak dostępu do modelu)."""


def _make_engine(api_key="sk-test", embeddings=None, llm=None, collection=None) -> RAGEngine:
    """Buduje RAGEngine z w pełni kontrolowanymi (zamockowanymi) zależnościami -
    bez importowania/łączenia się z prawdziwym OpenAI czy ChromaDB.

    Właściwości `embeddings`/`llm` konstruują leniwie prawdziwego klienta OpenAI,
    gdy `self._embeddings`/`self._llm` wynosi None, a `self.api_key` jest ustawiony.
    Aby żaden test nie wykonał w ten sposób prawdziwego połączenia sieciowego,
    gdy wywołujący nie poda jawnie mocka embeddings/llm, a `api_key` jest ustawiony,
    domyślnie podstawiamy nieszkodliwy MagicMock zamiast zostawiać None.
    """
    engine = RAGEngine()
    engine.api_key = api_key
    engine._embeddings = embeddings if embeddings is not None else (MagicMock() if api_key else None)
    engine._llm = llm if llm is not None else (MagicMock() if api_key else None)
    engine._collection = collection if collection is not None else MagicMock()
    return engine


class RAGEngineIndexKnowledgeBaseTests(TestCase):
    def setUp(self):
        KnowledgeDocument.objects.create(title="Meta description", content="Zasady meta description.", category="seo")
        KnowledgeDocument.objects.create(title="Canonical", content="Zasady canonical.", category="technical")

    def test_index_with_working_embeddings_upserts_with_vectors(self):
        mock_embeddings = MagicMock()
        mock_embeddings.embed_documents.return_value = [[0.1, 0.2], [0.3, 0.4]]
        mock_collection = MagicMock()
        engine = _make_engine(embeddings=mock_embeddings, collection=mock_collection)

        count = engine.index_knowledge_base()

        self.assertEqual(count, 2)
        mock_collection.upsert.assert_called_once()
        call_kwargs = mock_collection.upsert.call_args.kwargs
        self.assertIn("embeddings", call_kwargs)
        self.assertEqual(call_kwargs["embeddings"], [[0.1, 0.2], [0.3, 0.4]])

    def test_index_falls_back_to_chroma_default_embeddings_on_openai_403(self):
        """KLUCZOWE: 403/PermissionDenied z OpenAI NIE MOŻE przerwać indeksowania -
        silnik ma przejść na domyślne embeddingi ChromaDB i mimo to zaindeksować dokumenty."""
        mock_embeddings = MagicMock()
        mock_embeddings.embed_documents.side_effect = OpenAIPermissionDeniedError(
            "403 - model_not_found: text-embedding-3-small"
        )
        mock_collection = MagicMock()
        engine = _make_engine(embeddings=mock_embeddings, collection=mock_collection)

        count = engine.index_knowledge_base()

        self.assertEqual(count, 2)
        self.assertTrue(engine._embeddings_unavailable)
        mock_collection.upsert.assert_called_once()
        call_kwargs = mock_collection.upsert.call_args.kwargs
        self.assertNotIn("embeddings", call_kwargs)

    def test_index_with_no_documents_returns_zero_without_touching_chroma(self):
        KnowledgeDocument.objects.all().delete()
        mock_collection = MagicMock()
        engine = _make_engine(collection=mock_collection)

        count = engine.index_knowledge_base()

        self.assertEqual(count, 0)
        mock_collection.upsert.assert_not_called()

    def test_index_chroma_failure_returns_zero_without_raising(self):
        mock_collection = MagicMock()
        mock_collection.upsert.side_effect = RuntimeError("ChromaDB niedostępne")
        engine = _make_engine(collection=mock_collection)

        count = engine.index_knowledge_base()

        self.assertEqual(count, 0)


class ChromaDimensionMismatchError(Exception):
    """Symuluje chromadb.errors.InvalidArgumentError przy zmianie modelu embeddingów."""

    def __str__(self) -> str:
        return "Collection expecting embedding with dimension of 384, got 1536"


class RAGEngineCollectionRebuildTests(TestCase):
    """Zmiana modelu embeddingów zmienia długość wektora, a ChromaDB ustala ją
    bezpowrotnie przy tworzeniu kolekcji.

    Regresja: po odzyskaniu dostępu do embeddingów OpenAI (1536 wymiarów) indeks
    zbudowany domyślnym modelem ChromaDB (384) odrzucał każdy zapis, a komenda
    raportowała "zaindeksowano 0" - wyszukiwanie po cichu zostawało na fallbacku.
    """

    def setUp(self):
        KnowledgeDocument.objects.create(title="Meta description", content="...", category="seo")
        KnowledgeDocument.objects.create(title="Canonical", content="...", category="technical")

    def test_dimension_mismatch_rebuilds_collection_and_retries(self):
        stara = MagicMock(name="kolekcja-384")
        stara.upsert.side_effect = ChromaDimensionMismatchError()
        nowa = MagicMock(name="kolekcja-1536")
        engine = _make_engine(collection=stara)

        with patch.object(RAGEngine, "_recreate_collection") as przebudowa:
            def podmien():
                engine._collection = nowa
                return nowa

            przebudowa.side_effect = podmien
            count = engine.index_knowledge_base()

        self.assertEqual(count, 2)
        przebudowa.assert_called_once()
        nowa.upsert.assert_called_once()

    def test_other_chroma_error_does_not_rebuild_collection(self):
        """Przebudowa kasuje kolekcję - nie może być reakcją na dowolny błąd zapisu."""
        mock_collection = MagicMock()
        mock_collection.upsert.side_effect = RuntimeError("dysk pełny")
        engine = _make_engine(collection=mock_collection)

        with patch.object(RAGEngine, "_recreate_collection") as przebudowa:
            count = engine.index_knowledge_base()

        self.assertEqual(count, 0)
        przebudowa.assert_not_called()

    def test_failure_after_rebuild_returns_zero_without_raising(self):
        stara = MagicMock()
        stara.upsert.side_effect = ChromaDimensionMismatchError()
        engine = _make_engine(collection=stara)

        with patch.object(RAGEngine, "_recreate_collection", side_effect=RuntimeError("brak katalogu")):
            count = engine.index_knowledge_base()

        self.assertEqual(count, 0)

    def test_recreate_collection_deletes_before_creating(self):
        klient = MagicMock()
        engine = _make_engine()
        engine._chroma_client = klient

        engine._recreate_collection()

        klient.delete_collection.assert_called_once_with(COLLECTION_NAME)
        klient.get_or_create_collection.assert_called_once_with(COLLECTION_NAME)

    def test_recreate_collection_survives_missing_collection(self):
        """Katalog chroma_db bywa czyszczony ręcznie - brak kolekcji to nie błąd."""
        klient = MagicMock()
        klient.delete_collection.side_effect = RuntimeError("Collection not found")
        engine = _make_engine()
        engine._chroma_client = klient

        engine._recreate_collection()

        klient.get_or_create_collection.assert_called_once_with(COLLECTION_NAME)


class RAGEngineRetrieveKnowledgeTests(TestCase):
    def setUp(self):
        self.doc_seo = KnowledgeDocument.objects.create(
            title="Meta description", content="Dodaj unikalny meta description.", category="seo"
        )
        self.doc_technical = KnowledgeDocument.objects.create(
            title="Canonical", content="Ustaw znacznik canonical.", category="technical"
        )

    def test_retrieve_with_working_embeddings_queries_by_vector(self):
        mock_embeddings = MagicMock()
        mock_embeddings.embed_query.return_value = [0.1, 0.2, 0.3]
        mock_collection = MagicMock()
        mock_collection.query.return_value = {"ids": [[str(self.doc_seo.pk)]]}
        engine = _make_engine(embeddings=mock_embeddings, collection=mock_collection)

        results = engine.retrieve_knowledge("Brak meta description.", category="seo")

        self.assertEqual(results, [self.doc_seo])
        call_kwargs = mock_collection.query.call_args.kwargs
        self.assertIn("query_embeddings", call_kwargs)
        self.assertNotIn("query_texts", call_kwargs)

    def test_retrieve_falls_back_to_text_search_on_openai_403(self):
        """KLUCZOWE: błąd embeddingów OpenAI przełącza na query_texts w ChromaDB,
        zamiast przerywać wyszukiwanie/audyt."""
        mock_embeddings = MagicMock()
        mock_embeddings.embed_query.side_effect = OpenAIPermissionDeniedError("403 Forbidden")
        mock_collection = MagicMock()
        mock_collection.query.return_value = {"ids": [[str(self.doc_seo.pk)]]}
        engine = _make_engine(embeddings=mock_embeddings, collection=mock_collection)

        results = engine.retrieve_knowledge("Brak meta description.", category="seo")

        self.assertEqual(results, [self.doc_seo])
        self.assertTrue(engine._embeddings_unavailable)
        call_kwargs = mock_collection.query.call_args.kwargs
        self.assertIn("query_texts", call_kwargs)
        self.assertNotIn("query_embeddings", call_kwargs)

    def test_embeddings_not_retried_again_after_first_failure(self):
        mock_embeddings = MagicMock()
        mock_embeddings.embed_query.side_effect = OpenAIPermissionDeniedError("403 Forbidden")
        mock_collection = MagicMock()
        mock_collection.query.return_value = {"ids": [[]]}
        engine = _make_engine(embeddings=mock_embeddings, collection=mock_collection)

        engine.retrieve_knowledge("Problem 1", category="seo")
        engine.retrieve_knowledge("Problem 2", category="seo")

        self.assertEqual(mock_embeddings.embed_query.call_count, 1)

    def test_retrieve_falls_back_to_sqlite_category_filter_when_chroma_query_fails(self):
        """KLUCZOWE: całkowita awaria ChromaDB (np. brak modelu ONNX) nie może przerwać
        audytu - ostateczny fallback to zwykłe filtrowanie w Django ORM/SQLite."""
        mock_collection = MagicMock()
        mock_collection.query.side_effect = RuntimeError("ChromaDB niedostępne")
        engine = _make_engine(collection=mock_collection)

        results = engine.retrieve_knowledge("Brak canonical.", category="technical")

        self.assertEqual(results, [self.doc_technical])

    def test_retrieve_falls_back_to_sqlite_when_chroma_returns_no_matches(self):
        mock_collection = MagicMock()
        mock_collection.query.return_value = {"ids": [[]]}
        engine = _make_engine(collection=mock_collection)

        results = engine.retrieve_knowledge("Cokolwiek", category="seo")

        self.assertEqual(results, [self.doc_seo])

    def test_retrieve_without_category_returns_limited_results(self):
        mock_collection = MagicMock()
        mock_collection.query.side_effect = RuntimeError("ChromaDB niedostępne")
        engine = _make_engine(collection=mock_collection)

        results = engine.retrieve_knowledge("Cokolwiek", k=1)

        self.assertEqual(len(results), 1)


class RAGEngineGenerateRecommendationTests(TestCase):
    def setUp(self):
        self.doc = KnowledgeDocument.objects.create(
            title="Meta description", content="Dodaj unikalny meta description.", category="seo"
        )
        self.mock_collection = MagicMock()
        self.mock_collection.query.return_value = {"ids": [[str(self.doc.pk)]]}

    def test_generate_recommendation_uses_llm_when_available(self):
        mock_llm_response = MagicMock()
        mock_llm_response.content = "  Dodaj meta description do każdej podstrony.  "
        mock_llm = MagicMock()
        mock_llm.invoke.return_value = mock_llm_response
        engine = _make_engine(llm=mock_llm, collection=self.mock_collection)

        recommendation = engine.generate_recommendation("Brak meta description.", category="seo")

        self.assertEqual(recommendation, "Dodaj meta description do każdej podstrony.")
        mock_llm.invoke.assert_called_once()

    def test_generate_recommendation_falls_back_when_llm_raises(self):
        """KLUCZOWE: błąd LLM (np. rate limit, 403, timeout) nie może zwrócić błędu 500 -
        recommendation zawsze wraca jako string."""
        mock_llm = MagicMock()
        mock_llm.invoke.side_effect = RuntimeError("OpenAI API error")
        engine = _make_engine(llm=mock_llm, collection=self.mock_collection)

        recommendation = engine.generate_recommendation("Brak meta description.", category="seo")

        self.assertEqual(recommendation, self.doc.content)

    def test_generate_recommendation_without_api_key_uses_fallback_directly(self):
        """Zachowanie przy braku OPENAI_API_KEY: właściwości embeddings/llm zwracają None,
        więc silnik od razu korzysta z _fallback_recommendation bez prób połączenia."""
        engine = _make_engine(api_key=None, collection=self.mock_collection)

        self.assertIsNone(engine.embeddings)
        self.assertIsNone(engine.llm)

        recommendation = engine.generate_recommendation("Brak meta description.", category="seo")

        self.assertEqual(recommendation, self.doc.content)

    def test_fallback_recommendation_without_context_docs(self):
        engine = _make_engine(api_key=None, collection=MagicMock(query=MagicMock(return_value={"ids": [[]]})))
        KnowledgeDocument.objects.all().delete()

        recommendation = engine.generate_recommendation("Jakiś nietypowy problem SEO.")

        self.assertEqual(recommendation, "Zalecana weryfikacja: Jakiś nietypowy problem SEO.")


class RoutingWylaczonyTests(TestCase):
    """Obowiązujący kontrakt: JEDEN model dla wszystkich metryk.

    Routing został wycofany po pomiarze (3 przebiegi benchmarku na wariant): przewaga
    gpt-4o wyniosła -0.7 pp przy rozrzucie losowym do 4.9 pp - patrz komentarz przy
    COMPLEX_METRICS w rag.py. Te testy pilnują, że domyślna konfiguracja nie zacznie
    po cichu sięgać po droższy model.
    """

    def test_domyslnie_zaden_klucz_nie_kieruje_na_drozszy_model(self):
        klucze = (
            "title", "meta_description", "images_alt", "h1_structure", "robots_txt",
            "lcp", "mobile_lcp", "desktop_inp", "javascript_rendering",
            "internal_linking", "schema_entity_linking", "eeat_authorship",
        )
        for klucz in klucze:
            with self.subTest(metryka=klucz):
                self.assertEqual(get_model_for_metric(klucz), MODEL_PROSTY)

    def test_zbior_metryk_zlozonych_jest_pusty(self):
        """Pusty zbiór to świadoma decyzja poparta pomiarem, nie przeoczenie."""
        self.assertEqual(COMPLEX_METRICS, set())

    def test_brak_klucza_metryki_wybiera_model_domyslny(self):
        self.assertEqual(get_model_for_metric(None), MODEL_PROSTY)
        self.assertEqual(get_model_for_metric(""), MODEL_PROSTY)

    def test_override_dziala_mimo_wylaczonego_routingu(self):
        """Ewaluator porównuje modele przez --model-override - to musi działać
        niezależnie od tego, czy routing jest włączony."""
        self.assertEqual(get_model_for_metric("title", override_model="gpt-4o"), "gpt-4o")
        self.assertEqual(get_model_for_metric("lcp", override_model="gpt-4o-mini"), MODEL_PROSTY)


class MechanizmRoutinguTests(TestCase):
    """Sam mechanizm zostaje sprawny - przywrócenie routingu to wypełnienie
    COMPLEX_METRICS. Testy działają na podstawionym zbiorze, żeby sprawdzać logikę,
    a nie obowiązującą konfigurację."""

    def test_metryka_ze_zbioru_kieruje_na_drozszy_model(self):
        with patch("auditor.services.rag.COMPLEX_METRICS", {"lcp", "javascript_rendering"}):
            self.assertEqual(get_model_for_metric("lcp"), MODEL_ZLOZONY)
            self.assertEqual(get_model_for_metric("javascript_rendering"), MODEL_ZLOZONY)

    def test_metryka_spoza_zbioru_zostaje_na_modelu_domyslnym(self):
        with patch("auditor.services.rag.COMPLEX_METRICS", {"lcp"}):
            self.assertEqual(get_model_for_metric("title"), MODEL_PROSTY)

    def test_prefiks_strategii_pagespeed_jest_odcinany(self):
        """AuditService zapisuje "mobile_lcp", nie "lcp" - bez odcięcia prefiksu
        routing pomijałby wszystkie Core Web Vitals."""
        with patch("auditor.services.rag.COMPLEX_METRICS", {"lcp", "cls", "inp", "fcp"}):
            for klucz in ("mobile_lcp", "desktop_lcp", "mobile_cls", "desktop_inp", "mobile_fcp"):
                with self.subTest(metryka=klucz):
                    self.assertEqual(get_model_for_metric(klucz), MODEL_ZLOZONY)

    def test_prefiks_nie_promuje_metryki_spoza_zbioru(self):
        with patch("auditor.services.rag.COMPLEX_METRICS", {"lcp"}):
            self.assertEqual(get_model_for_metric("mobile_pagespeed"), MODEL_PROSTY)

    def test_wielkosc_liter_i_biale_znaki_nie_maja_znaczenia(self):
        with patch("auditor.services.rag.COMPLEX_METRICS", {"lcp"}):
            self.assertEqual(get_model_for_metric("  LCP  "), MODEL_ZLOZONY)


class ModelRoutingIntegrationTests(TestCase):
    """Routing widziany przez `generate_recommendation` - klient budowany jest dla
    modelu wybranego przez router, a nie zawsze dla domyślnego."""

    def setUp(self):
        KnowledgeDocument.objects.create(title="LCP", content="Zasady LCP.", category="performance")

    def _engine_z_atrapa_klienta(self):
        """Silnik, który zamiast prawdziwego ChatOpenAI buduje atrapę - `_build_llm`
        jest jedynym miejscem tworzenia klienta, więc to naturalny szew testowy."""
        engine = RAGEngine()
        engine.api_key = "sk-test"
        engine._embeddings = MagicMock()
        engine._collection = MagicMock()
        engine._collection.query.return_value = {"ids": [[]]}
        return engine

    def _wywolaj(self, engine, **kwargs) -> str:
        odpowiedz = MagicMock()
        odpowiedz.content = "### 1. DIAGNOZA I PRZYCZYNA TECHNICZNA"
        klient = MagicMock()
        klient.invoke.return_value = odpowiedz

        with patch.object(RAGEngine, "_build_llm", return_value=klient) as budowniczy:
            engine.generate_recommendation("Problem z metryką.", **kwargs)
        return budowniczy

    def test_domyslnie_kazda_metryka_buduje_klienta_modelu_domyslnego(self):
        for klucz in ("lcp", "images_alt", "javascript_rendering"):
            with self.subTest(metryka=klucz):
                budowniczy = self._wywolaj(self._engine_z_atrapa_klienta(), metric_key=klucz)
                budowniczy.assert_called_once_with(MODEL_PROSTY)

    def test_po_wlaczeniu_routingu_metryka_zlozona_siega_po_drozszy_model(self):
        with patch("auditor.services.rag.COMPLEX_METRICS", {"lcp"}):
            budowniczy = self._wywolaj(self._engine_z_atrapa_klienta(), metric_key="lcp")

        budowniczy.assert_called_once_with(MODEL_ZLOZONY)

    def test_model_override_wymusza_model_niezaleznie_od_metryki(self):
        budowniczy = self._wywolaj(
            self._engine_z_atrapa_klienta(), metric_key="images_alt", model_override="gpt-4o"
        )

        budowniczy.assert_called_once_with("gpt-4o")

    def test_klient_modelu_jest_budowany_raz_i_cache_owany(self):
        """Audyt woła generator kilkanaście razy - budowanie klienta za każdym
        razem byłoby zbędną pracą."""
        engine = self._engine_z_atrapa_klienta()
        odpowiedz = MagicMock()
        odpowiedz.content = "tekst"
        klient = MagicMock()
        klient.invoke.return_value = odpowiedz

        with patch.object(RAGEngine, "_build_llm", return_value=klient) as budowniczy:
            for _ in range(3):
                engine.generate_recommendation("Problem.", metric_key="lcp")

        self.assertEqual(budowniczy.call_count, 1)
        self.assertEqual(klient.invoke.call_count, 3)

    def test_brak_klucza_api_nie_probuje_budowac_klienta(self):
        engine = RAGEngine()
        engine.api_key = None
        engine._collection = MagicMock()
        engine._collection.query.return_value = {"ids": [[]]}

        with patch.object(RAGEngine, "_build_llm") as budowniczy:
            wynik = engine.generate_recommendation("Problem.", metric_key="lcp")

        budowniczy.assert_not_called()
        self.assertTrue(wynik)  # fallback z bazy wiedzy, nie wyjątek


class RoutingWAuditServiceTests(TestCase):
    """Punkt integracji: bez przekazania `metric_key` z `_make_metric` routing
    nigdy by się nie uruchomił - każda metryka dostawałaby model domyślny."""

    def _service(self):
        from auditor.services.audit_service import AuditService

        service = AuditService.__new__(AuditService)
        service.rag_engine = MagicMock()
        service.rag_engine.generate_recommendation.return_value = "rekomendacja"
        return service

    def test_make_metric_przekazuje_klucz_metryki_do_generatora(self):
        service = self._service()

        service._make_metric("performance", "mobile_lcp", {"note": "LCP za wolne."}, "error")

        self.assertEqual(
            service.rag_engine.generate_recommendation.call_args.kwargs["metric_key"],
            "mobile_lcp",
        )

    def test_przekazany_klucz_domyka_lancuch_do_nazwy_modelu(self):
        """Klucz z AuditService -> router -> nazwa modelu. Przy wycofanym routingu
        obie metryki trafiają na ten sam model - łańcuch nadal działa."""
        service = self._service()

        service._make_metric("seo", "images_alt", {"note": "Brak atrybutów alt."}, "warning")
        prosty = service.rag_engine.generate_recommendation.call_args.kwargs["metric_key"]

        service._make_metric("technical", "javascript_rendering", {"note": "CSR."}, "error")
        zlozony = service.rag_engine.generate_recommendation.call_args.kwargs["metric_key"]

        self.assertEqual(get_model_for_metric(prosty), MODEL_PROSTY)
        self.assertEqual(get_model_for_metric(zlozony), MODEL_PROSTY)

        with patch("auditor.services.rag.COMPLEX_METRICS", {"javascript_rendering"}):
            self.assertEqual(get_model_for_metric(zlozony), MODEL_ZLOZONY)


class DegradacjaModeluTests(TestCase):
    """Niedostępny model złożony (403 model_not_found) nie może zrzucać rekomendacji
    do surowego dokumentu z bazy wiedzy - ten ma własne nagłówki i powiela definicję
    pokazywaną już na karcie testu."""

    def setUp(self):
        KnowledgeDocument.objects.create(
            title="LCP", content="Surowa treść dokumentu.", category="performance"
        )

    def _engine(self):
        engine = RAGEngine()
        engine.api_key = "sk-test"
        engine._embeddings = MagicMock()
        engine._collection = MagicMock()
        engine._collection.query.return_value = {"ids": [[]]}
        return engine

    def _klient(self, tresc="### 1. DIAGNOZA I PRZYCZYNA TECHNICZNA"):
        odpowiedz = MagicMock()
        odpowiedz.content = tresc
        klient = MagicMock()
        klient.invoke.return_value = odpowiedz
        return klient

    def test_brak_dostepu_do_modelu_zlozonego_schodzi_na_tanszy(self):
        zepsuty, dzialajacy = self._klient(), self._klient("### 1. DIAGNOZA z modelu taniego")
        zepsuty.invoke.side_effect = OpenAIPermissionDeniedError("403 model_not_found")

        klienci = {MODEL_ZLOZONY: zepsuty, MODEL_PROSTY: dzialajacy}
        with patch("auditor.services.rag.COMPLEX_METRICS", {"lcp"}), \
                patch.object(RAGEngine, "_build_llm", side_effect=lambda m: klienci[m]):
            wynik = self._engine().generate_recommendation("Problem.", metric_key="lcp")

        self.assertIn("DIAGNOZA z modelu taniego", wynik)
        self.assertNotIn("Surowa treść dokumentu.", wynik)

    def test_niedostepny_model_nie_jest_ponawiany_przy_kolejnych_metrykach(self):
        """Audyt liczy kilkanaście metryk złożonych - jedno 403 wystarczy za wszystkie."""
        zepsuty, dzialajacy = self._klient(), self._klient()
        zepsuty.invoke.side_effect = OpenAIPermissionDeniedError("403 model_not_found")
        klienci = {MODEL_ZLOZONY: zepsuty, MODEL_PROSTY: dzialajacy}

        engine = self._engine()
        with patch("auditor.services.rag.COMPLEX_METRICS", {"lcp"}), \
                patch.object(RAGEngine, "_build_llm", side_effect=lambda m: klienci[m]):
            for _ in range(4):
                engine.generate_recommendation("Problem.", metric_key="lcp")

        self.assertEqual(zepsuty.invoke.call_count, 1)
        self.assertEqual(dzialajacy.invoke.call_count, 4)

    def test_awaria_obu_modeli_konczy_sie_fallbackiem_z_bazy_wiedzy(self):
        zepsuty = self._klient()
        zepsuty.invoke.side_effect = RuntimeError("API niedostępne")

        with patch.object(RAGEngine, "_build_llm", return_value=zepsuty):
            wynik = self._engine().generate_recommendation("Problem.", metric_key="lcp")

        self.assertIn("Surowa treść dokumentu.", wynik)

    def test_awaria_modelu_prostego_nie_probuje_go_dwa_razy(self):
        zepsuty = self._klient()
        zepsuty.invoke.side_effect = RuntimeError("API niedostępne")

        with patch.object(RAGEngine, "_build_llm", return_value=zepsuty):
            self._engine().generate_recommendation("Problem.", metric_key="images_alt")

        self.assertEqual(zepsuty.invoke.call_count, 1)

