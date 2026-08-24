"""Testy RAGEngine: wyszukiwanie kontekstu, generowanie rekomendacji i - najważniejsze -
odporność na błędy zewnętrznych usług (OpenAI 403/model_not_found, awaria ChromaDB,
brak klucza OPENAI_API_KEY). Żaden z tych scenariuszy nie może rzucić wyjątku ani
wywołać prawdziwego połączenia sieciowego - wszystkie zależności (ChatOpenAI,
OpenAIEmbeddings, chroma_collection) są tu w pełni zamockowane.
"""
from __future__ import annotations

from unittest.mock import MagicMock

from django.test import TestCase

from auditor.models import KnowledgeDocument
from auditor.services.rag import RAGEngine


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
