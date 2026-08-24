"""Testy PageSpeedService: parsowanie Core Web Vitals, obsługa błędów HTTP (400/500),
timeoutów oraz równoległych zapytań mobile/desktop. Wszystkie żądania HTTP są mockowane
(unittest.mock) - żaden test nie łączy się z prawdziwym API Google.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
from django.test import SimpleTestCase

from auditor.services.pagespeed import PageSpeedService

API_URL = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"


def _psi_response(status_code=200, score=0.85, lcp_ms=1200, cls=0.05, fcp_ms=800, inp_ms=150):
    payload = {
        "lighthouseResult": {
            "categories": {"performance": {"score": score}},
            "audits": {
                "largest-contentful-paint": {"numericValue": lcp_ms},
                "cumulative-layout-shift": {"numericValue": cls},
                "first-contentful-paint": {"numericValue": fcp_ms},
            },
        },
        "loadingExperience": {"metrics": {"INTERACTION_TO_NEXT_PAINT": {"percentile": inp_ms}}},
    }
    return httpx.Response(status_code, request=httpx.Request("GET", API_URL), json=payload)


def _error_response(status_code):
    return httpx.Response(
        status_code, request=httpx.Request("GET", API_URL), json={"error": {"code": status_code}}
    )


def _mock_client(get_return=None, get_side_effect=None):
    """Buduje mock httpx.Client wspierający `with httpx.Client(...) as client: client.get(...)`."""
    client = MagicMock()
    client.__enter__.return_value = client
    client.__exit__.return_value = False
    if get_side_effect is not None:
        client.get.side_effect = get_side_effect
    else:
        client.get.return_value = get_return
    return client


class PageSpeedServiceParsingTests(SimpleTestCase):
    """Poprawne parsowanie Core Web Vitals (LCP, CLS, FCP) i wyniku Performance."""

    @patch("auditor.services.pagespeed.httpx.Client")
    def test_analyze_mobile_parses_core_web_vitals(self, mock_client_cls):
        mock_client_cls.return_value = _mock_client(
            get_return=_psi_response(score=0.85, lcp_ms=1200, cls=0.05, fcp_ms=800, inp_ms=150)
        )
        service = PageSpeedService(max_retries=0)

        result = service.analyze("https://example.com", strategy="mobile")

        self.assertTrue(result["available"])
        self.assertIsNone(result["error"])
        self.assertEqual(result["performance_score"], 85)
        self.assertAlmostEqual(result["lcp"], 1.2)
        self.assertAlmostEqual(result["cls"], 0.05)
        self.assertAlmostEqual(result["fcp"], 0.8)
        self.assertEqual(result["inp"], 150)

    @patch("auditor.services.pagespeed.httpx.Client")
    def test_analyze_desktop_parses_core_web_vitals(self, mock_client_cls):
        mock_client_cls.return_value = _mock_client(
            get_return=_psi_response(score=0.97, lcp_ms=900, cls=0.01, fcp_ms=500, inp_ms=80)
        )
        service = PageSpeedService(max_retries=0)

        result = service.analyze("https://example.com", strategy="desktop")

        self.assertTrue(result["available"])
        self.assertEqual(result["performance_score"], 97)
        self.assertAlmostEqual(result["lcp"], 0.9)
        self.assertAlmostEqual(result["fcp"], 0.5)

    @patch("auditor.services.pagespeed.httpx.Client")
    def test_analyze_sends_correct_query_params(self, mock_client_cls):
        client = _mock_client(get_return=_psi_response())
        mock_client_cls.return_value = client
        service = PageSpeedService(max_retries=0)

        service.analyze("https://example.com", strategy="desktop")

        call_kwargs = client.get.call_args.kwargs
        self.assertEqual(call_kwargs["params"]["strategy"], "desktop")
        self.assertEqual(call_kwargs["params"]["category"], "performance")
        self.assertEqual(call_kwargs["params"]["url"], "https://example.com")

    @patch("auditor.services.pagespeed.httpx.Client")
    def test_malformed_response_returns_safe_fallback(self, mock_client_cls):
        mock_client_cls.return_value = _mock_client(
            get_return=httpx.Response(200, request=httpx.Request("GET", API_URL), json={"unexpected": True})
        )
        service = PageSpeedService(max_retries=0)

        result = service.analyze("https://example.com", strategy="mobile")

        self.assertFalse(result["available"])
        self.assertIsNotNone(result["error"])


class PageSpeedServiceUrlNormalizationTests(SimpleTestCase):
    def setUp(self):
        self.service = PageSpeedService()

    def test_adds_https_scheme_when_missing(self):
        self.assertEqual(self.service._normalize_url("example.com"), "https://example.com")

    def test_strips_surrounding_whitespace(self):
        self.assertEqual(self.service._normalize_url(" https://example.com "), "https://example.com")

    def test_keeps_valid_url_unchanged(self):
        self.assertEqual(self.service._normalize_url("https://example.com/path"), "https://example.com/path")


class PageSpeedServiceErrorHandlingTests(SimpleTestCase):
    """Obsługa błędów API (np. HTTP 400 dla strategii desktop) - bez wyjątków."""

    @patch("auditor.services.pagespeed.httpx.Client")
    def test_http_400_for_desktop_returns_safe_fallback_dict(self, mock_client_cls):
        mock_client_cls.return_value = _mock_client(get_return=_error_response(400))
        service = PageSpeedService(max_retries=0)

        result = service.analyze("https://example.com", strategy="desktop")

        self.assertEqual(
            result,
            {
                "available": False,
                "error": result["error"],
                "performance_score": None,
                "lcp": None,
                "cls": None,
                "fcp": None,
                "inp": None,
            },
        )
        self.assertIn("400", result["error"])
        self.assertIn("desktop", result["error"])

    @patch("auditor.services.pagespeed.httpx.Client")
    def test_http_500_returns_safe_fallback_without_raising(self, mock_client_cls):
        mock_client_cls.return_value = _mock_client(get_return=_error_response(500))
        service = PageSpeedService(max_retries=0)

        result = service.analyze("https://example.com", strategy="mobile")

        self.assertFalse(result["available"])

    @patch("auditor.services.pagespeed.time.sleep")
    @patch("auditor.services.pagespeed.httpx.Client")
    def test_retryable_status_code_is_retried_then_falls_back(self, mock_client_cls, mock_sleep):
        client = _mock_client(get_return=_error_response(500))
        mock_client_cls.return_value = client
        service = PageSpeedService(max_retries=1)

        result = service.analyze("https://example.com", strategy="desktop")

        self.assertFalse(result["available"])
        self.assertEqual(client.get.call_count, 2)
        mock_sleep.assert_called()

    @patch("auditor.services.pagespeed.time.sleep")
    @patch("auditor.services.pagespeed.httpx.Client")
    def test_retry_succeeds_on_second_attempt(self, mock_client_cls, mock_sleep):
        client = _mock_client()
        client.get.side_effect = [_error_response(500), _psi_response(score=0.9)]
        mock_client_cls.return_value = client
        service = PageSpeedService(max_retries=1)

        result = service.analyze("https://example.com", strategy="desktop")

        self.assertTrue(result["available"])
        self.assertEqual(result["performance_score"], 90)

    @patch("auditor.services.pagespeed.time.sleep")
    @patch("auditor.services.pagespeed.httpx.Client")
    def test_read_timeout_returns_safe_fallback_without_raising(self, mock_client_cls, mock_sleep):
        client = _mock_client()
        client.get.side_effect = httpx.ReadTimeout(
            "Timed out", request=httpx.Request("GET", API_URL)
        )
        mock_client_cls.return_value = client
        service = PageSpeedService(max_retries=1)

        result = service.analyze("https://example.com", strategy="mobile")

        self.assertFalse(result["available"])
        self.assertIn("nie odpowiedziało", result["error"])

    @patch("auditor.services.pagespeed.httpx.Client")
    def test_connection_error_returns_safe_fallback_without_raising(self, mock_client_cls):
        client = _mock_client()
        client.get.side_effect = httpx.ConnectError("Connection refused")
        mock_client_cls.return_value = client
        service = PageSpeedService(max_retries=0)

        result = service.analyze("https://example.com", strategy="mobile")

        self.assertFalse(result["available"])

    @patch("auditor.services.pagespeed.httpx.Client")
    def test_api_key_never_leaks_into_error_message(self, mock_client_cls):
        """Regresja: komunikat błędu nie może zawierać klucza API (przekazywanego przez
        PSI jako parametr URL 'key')."""
        mock_client_cls.return_value = _mock_client(get_return=_error_response(400))
        service = PageSpeedService(max_retries=0)
        service.api_key = "TAJNY_KLUCZ_API"

        result = service.analyze("https://example.com", strategy="desktop")

        self.assertNotIn("TAJNY_KLUCZ_API", result["error"])


class PageSpeedServiceAnalyzeAllTests(SimpleTestCase):
    """Równoległe zapytania dla strategii mobile i desktop."""

    def test_analyze_all_returns_both_strategies(self):
        expected = {
            "mobile": {"available": True, "performance_score": 60},
            "desktop": {"available": True, "performance_score": 95},
        }

        def fake_analyze(url, strategy="mobile"):
            return expected[strategy]

        service = PageSpeedService()
        with patch.object(PageSpeedService, "analyze", side_effect=fake_analyze):
            results = service.analyze_all("https://example.com")

        self.assertEqual(set(results.keys()), {"mobile", "desktop"})
        self.assertEqual(results["mobile"]["performance_score"], 60)
        self.assertEqual(results["desktop"]["performance_score"], 95)

    def test_analyze_all_one_strategy_failing_does_not_affect_the_other(self):
        def fake_analyze(url, strategy="mobile"):
            if strategy == "desktop":
                return {"available": False, "error": "HTTP 400", "performance_score": None}
            return {"available": True, "error": None, "performance_score": 88}

        service = PageSpeedService()
        with patch.object(PageSpeedService, "analyze", side_effect=fake_analyze):
            results = service.analyze_all("https://example.com")

        self.assertTrue(results["mobile"]["available"])
        self.assertEqual(results["mobile"]["performance_score"], 88)
        self.assertFalse(results["desktop"]["available"])
