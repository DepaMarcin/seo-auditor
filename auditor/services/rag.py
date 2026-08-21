from __future__ import annotations

import logging
from pathlib import Path

from django.conf import settings

logger = logging.getLogger(__name__)

CHROMA_DIR = Path(settings.BASE_DIR) / "chroma_db"
COLLECTION_NAME = "seo_knowledge"


class RAGEngine:
    """
    Silnik RAG (Retrieval-Augmented Generation) odpowiedzialny za:
      1. Indeksowanie KnowledgeDocument w wektorowej bazie ChromaDB.
      2. Wyszukiwanie wiedzy powiązanej z wykrytymi problemami SEO.
      3. Generowanie rekomendacji przy użyciu modelu gpt-4o-mini (LangChain).

    Wszystkie zależności (ChromaDB, LangChain/OpenAI) są ładowane leniwie,
    a brak klucza OPENAI_API_KEY nie powoduje błędu - silnik przechodzi
    wtedy na dopasowanie po kategorii/słowach kluczowych i szablonowe
    rekomendacje z bazy wiedzy.
    """

    MODEL_NAME = "gpt-4o-mini"
    EMBEDDING_MODEL = "text-embedding-3-small"

    def __init__(self):
        self._chroma_client = None
        self._collection = None
        self._embeddings = None
        self._llm = None
        self.api_key = getattr(settings, "OPENAI_API_KEY", "") or None

    # ------------------------------------------------------------------
    # Leniwe inicjalizatory
    # ------------------------------------------------------------------
    @property
    def chroma_collection(self):
        if self._collection is None:
            import chromadb

            self._chroma_client = chromadb.PersistentClient(path=str(CHROMA_DIR))
            self._collection = self._chroma_client.get_or_create_collection(COLLECTION_NAME)
        return self._collection

    @property
    def embeddings(self):
        if self._embeddings is None and self.api_key:
            from langchain_openai import OpenAIEmbeddings

            self._embeddings = OpenAIEmbeddings(model=self.EMBEDDING_MODEL, api_key=self.api_key)
        return self._embeddings

    @property
    def llm(self):
        if self._llm is None and self.api_key:
            from langchain_openai import ChatOpenAI

            self._llm = ChatOpenAI(model=self.MODEL_NAME, api_key=self.api_key, temperature=0.3)
        return self._llm

    # ------------------------------------------------------------------
    # Indeksowanie
    # ------------------------------------------------------------------
    def index_knowledge_base(self) -> int:
        """Indeksuje wszystkie KnowledgeDocument w ChromaDB. Zwraca liczbę zaindeksowanych dokumentów."""
        from auditor.models import KnowledgeDocument

        documents = list(KnowledgeDocument.objects.all())
        if not documents:
            return 0

        ids = [str(doc.pk) for doc in documents]
        texts = [f"{doc.title}\n{doc.content}" for doc in documents]
        metadatas = [{"category": doc.category, "title": doc.title} for doc in documents]

        try:
            if self.embeddings is not None:
                vectors = self.embeddings.embed_documents(texts)
                self.chroma_collection.upsert(
                    ids=ids, embeddings=vectors, documents=texts, metadatas=metadatas
                )
            else:
                self.chroma_collection.upsert(ids=ids, documents=texts, metadatas=metadatas)
        except Exception:
            logger.exception("Nie udało się zaindeksować bazy wiedzy w ChromaDB.")
            return 0

        return len(documents)

    # ------------------------------------------------------------------
    # Wyszukiwanie (retrieval)
    # ------------------------------------------------------------------
    def retrieve_knowledge(self, issue_description: str, category: str | None = None, k: int = 3):
        """Wyszukuje dokumenty wiedzy powiązane z opisem problemu SEO."""
        from auditor.models import KnowledgeDocument

        try:
            query_kwargs = {"n_results": k}
            if category:
                query_kwargs["where"] = {"category": category}

            if self.embeddings is not None:
                query_kwargs["query_embeddings"] = [self.embeddings.embed_query(issue_description)]
            else:
                query_kwargs["query_texts"] = [issue_description]

            results = self.chroma_collection.query(**query_kwargs)
            ids = results.get("ids", [[]])[0]
            if ids:
                found = KnowledgeDocument.objects.filter(pk__in=ids)
                if found:
                    return list(found)
        except Exception:
            logger.exception("Błąd wyszukiwania w ChromaDB, używam fallbacku słów kluczowych.")

        # Fallback: proste dopasowanie po kategorii
        queryset = KnowledgeDocument.objects.all()
        if category:
            queryset = queryset.filter(category=category)
        return list(queryset[:k])

    # ------------------------------------------------------------------
    # Generowanie rekomendacji
    # ------------------------------------------------------------------
    def generate_recommendation(self, issue_description: str, category: str | None = None) -> str:
        """Generuje rekomendację naprawy problemu SEO w oparciu o wiedzę z bazy (RAG)."""
        context_docs = self.retrieve_knowledge(issue_description, category=category)
        context_text = "\n\n".join(f"- {doc.title}: {doc.content}" for doc in context_docs)

        if self.llm is not None:
            try:
                return self._generate_with_llm(issue_description, context_text)
            except Exception:
                logger.exception("Błąd generowania rekomendacji przez LLM, używam fallbacku.")

        return self._fallback_recommendation(issue_description, context_docs)

    def _generate_with_llm(self, issue_description: str, context_text: str) -> str:
        from langchain_core.messages import HumanMessage, SystemMessage

        system_prompt = (
            "Jesteś ekspertem SEO. Na podstawie wykrytego problemu oraz kontekstu "
            "z bazy wiedzy przygotuj krótką, konkretną rekomendację naprawczą w "
            "języku polskim (maksymalnie 3 zdania)."
        )
        human_prompt = (
            f"Problem SEO: {issue_description}\n\n"
            f"Kontekst z bazy wiedzy:\n{context_text or 'Brak dodatkowego kontekstu.'}"
        )

        response = self.llm.invoke(
            [SystemMessage(content=system_prompt), HumanMessage(content=human_prompt)]
        )
        return response.content.strip()

    def _fallback_recommendation(self, issue_description: str, context_docs) -> str:
        if context_docs:
            return context_docs[0].content
        return f"Zalecana weryfikacja: {issue_description}"
