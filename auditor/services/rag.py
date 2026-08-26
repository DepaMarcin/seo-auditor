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
    # "text-embedding-ada-002" jako domyślny model embeddingów - jest dostępny na
    # wszystkich kontach/projektach OpenAI, w odróżnieniu od "text-embedding-3-small",
    # które bez osobno przyznanego dostępu zwraca błąd 403 model_not_found.
    EMBEDDING_MODEL = "text-embedding-ada-002"

    def __init__(self):
        self._chroma_client = None
        self._collection = None
        self._embeddings = None
        self._llm = None
        self.api_key = getattr(settings, "OPENAI_API_KEY", "") or None
        self.embedding_model = getattr(settings, "OPENAI_EMBEDDING_MODEL", "") or self.EMBEDDING_MODEL
        # Gdy embeddingi OpenAI raz zawiodą (np. 403 PermissionDenied / model_not_found),
        # nie próbujemy ich ponownie w ramach tego samego audytu - od razu fallback.
        self._embeddings_unavailable = False

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

            self._embeddings = OpenAIEmbeddings(model=self.embedding_model, api_key=self.api_key)
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

        # Dedykowany blok na wywołanie OpenAI - brak dostępu do modelu embeddingów
        # (np. 403 PermissionDeniedError / model_not_found) nie może przerwać indeksowania,
        # tylko przełączyć na domyślne (wbudowane) embeddingi ChromaDB.
        vectors = None
        if self.embeddings is not None and not self._embeddings_unavailable:
            try:
                vectors = self.embeddings.embed_documents(texts)
            except Exception as exc:
                # Wyciszone celowo do poziomu debug: brak dostępu do embeddingów OpenAI
                # (np. 403 model_not_found) to oczekiwany, obsłużony przypadek - nie błąd
                # wymagający uwagi - dlatego cicho przechodzimy na domyślne embeddingi ChromaDB.
                logger.debug("Embeddingi OpenAI niedostępne (%s) - używam domyślnych embeddingów ChromaDB.",
                             type(exc).__name__)
                self._embeddings_unavailable = True
                vectors = None

        try:
            if vectors is not None:
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
        """Wyszukuje dokumenty wiedzy powiązane z opisem problemu SEO.

        Trzypoziomowy fallback: embeddingi OpenAI -> wyszukiwanie tekstowe ChromaDB
        (query_texts) -> filtrowanie po kategorii bezpośrednio w SQLite/Django ORM.
        Żaden z tych poziomów nie może przerwać audytu ani zwrócić błędu 500.
        """
        from auditor.models import KnowledgeDocument

        query_kwargs = {"n_results": k}
        if category:
            query_kwargs["where"] = {"category": category}

        # Dedykowany blok na embed_query - błąd OpenAI (403/model_not_found) przełącza
        # na awaryjne wyszukiwanie tekstowe ChromaDB (query_texts), a nie przerywa audytu.
        query_embedding = None
        if self.embeddings is not None and not self._embeddings_unavailable:
            try:
                query_embedding = self.embeddings.embed_query(issue_description)
            except Exception as exc:
                # Wyciszone celowo do poziomu debug - patrz komentarz w index_knowledge_base().
                logger.debug("Embeddingi OpenAI niedostępne (%s) - używam wyszukiwania tekstowego w ChromaDB.",
                             type(exc).__name__)
                self._embeddings_unavailable = True
                query_embedding = None

        if query_embedding is not None:
            query_kwargs["query_embeddings"] = [query_embedding]
        else:
            query_kwargs["query_texts"] = [issue_description]

        try:
            results = self.chroma_collection.query(**query_kwargs)
            ids = results.get("ids", [[]])[0]
            if ids:
                found = KnowledgeDocument.objects.filter(pk__in=ids)
                if found:
                    return list(found)
        except Exception:
            logger.exception(
                "Błąd wyszukiwania w ChromaDB, przechodzę na filtrowanie po kategorii (SQLite)."
            )

        # Ostateczny fallback: proste dopasowanie po kategorii w Django ORM/SQLite.
        queryset = KnowledgeDocument.objects.all()
        if category:
            queryset = queryset.filter(category=category)
        return list(queryset[:k])

    # ------------------------------------------------------------------
    # Generowanie rekomendacji
    # ------------------------------------------------------------------
    def generate_recommendation(
        self, issue_description: str, category: str | None = None, current_value: str | None = None
    ) -> str:
        """Generuje rekomendację naprawy problemu SEO w oparciu o wiedzę z bazy (RAG).

        `current_value` to zastany fragment/wartość ze strony (np. obecny tekst <title>,
        lista URL-i obrazków bez ALT) - pozwala AI podać bezpośredni przykład poprawki
        zamiast ogólnikowej porady.
        """
        context_docs = self.retrieve_knowledge(issue_description, category=category)
        context_text = "\n\n".join(f"- {doc.title}: {doc.content}" for doc in context_docs)

        if self.llm is not None:
            try:
                return self._generate_with_llm(issue_description, context_text, current_value=current_value)
            except Exception:
                logger.exception("Błąd generowania rekomendacji przez LLM, używam fallbacku.")

        return self._fallback_recommendation(issue_description, context_docs, current_value=current_value)

    def _generate_with_llm(
        self, issue_description: str, context_text: str, current_value: str | None = None
    ) -> str:
        from langchain_core.messages import HumanMessage, SystemMessage

        system_prompt = (
            "Jesteś ekspertem SEO. Na podstawie wykrytego problemu, zastanego elementu ze "
            "strony oraz kontekstu z bazy wiedzy przygotuj krótką, konkretną rekomendację "
            "naprawczą w języku polskim (maksymalnie 4 zdania). Jeśli podano zastany element "
            "(np. tekst tytułu, meta description, nagłówka), ZAWSZE podaj bezpośredni przykład "
            "poprawki w formacie: 'Obecnie: [zastany tekst] -> Proponowane: [poprawiona wersja]'. "
            "Jeśli problem dotyczy brakującego atrybutu alt lub innego znacznika HTML, podaj "
            "gotowy fragment kodu HTML z prawidłową składnią (w bloku kodu)."
        )
        human_prompt = (
            f"Problem SEO: {issue_description}\n\n"
            f"Zastany element na stronie: {current_value or 'Brak zastanego fragmentu.'}\n\n"
            f"Kontekst z bazy wiedzy:\n{context_text or 'Brak dodatkowego kontekstu.'}"
        )

        response = self.llm.invoke(
            [SystemMessage(content=system_prompt), HumanMessage(content=human_prompt)]
        )
        return response.content.strip()

    def _fallback_recommendation(self, issue_description: str, context_docs, current_value: str | None = None) -> str:
        if context_docs:
            return context_docs[0].content
        if current_value:
            return f"Zalecana weryfikacja: {issue_description} (obecna wartość: {current_value})"
        return f"Zalecana weryfikacja: {issue_description}"
