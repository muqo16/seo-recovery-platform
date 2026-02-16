import io
import time
import unittest

from fastapi.testclient import TestClient

import app.main as app_main


class GoldenFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app_main.app)

    def test_migration_flow_with_comparison(self) -> None:
        old_csv = io.BytesIO(
            b"url,type\nhttps://eski-site.com/urunler/siyah-elbise,product\nhttps://eski-site.com/kategori/kadin-giyim,category\n"
        )
        new_csv = io.BytesIO(
            b"url,type\nhttps://yeni-site.com/products/siyah-elbise,product\nhttps://yeni-site.com/collections/kadin-giyim,category\n"
        )
        before_csv = io.BytesIO(
            b"url,clicks,impressions,position\nhttps://eski-site.com/urunler/siyah-elbise,200,2000,3\nhttps://eski-site.com/kategori/kadin-giyim,100,1000,4\n"
        )
        after_csv = io.BytesIO(
            b"url,clicks,impressions,position\nhttps://yeni-site.com/products/siyah-elbise,140,2100,4\nhttps://yeni-site.com/collections/kadin-giyim,120,1200,3\n"
        )

        start = self.client.post(
            "/analyze/start",
            files={
                "old_urls": ("old.csv", old_csv.read(), "text/csv"),
                "new_urls": ("new.csv", new_csv.read(), "text/csv"),
                "gsc_before_pages": ("before.csv", before_csv.read(), "text/csv"),
                "gsc_after_pages": ("after.csv", after_csv.read(), "text/csv"),
            },
        )
        self.assertEqual(start.status_code, 200)
        job_id = start.json()["job_id"]

        status = None
        for _ in range(50):
            status_resp = self.client.get(f"/analyze/status/{job_id}")
            self.assertEqual(status_resp.status_code, 200)
            status_data = status_resp.json()
            status = status_data["status"]
            if status in {"done", "error", "cancelled"}:
                break
            time.sleep(0.1)

        self.assertEqual(status, "done")
        result = self.client.get(f"/result/{job_id}")
        self.assertEqual(result.status_code, 200)
        self.assertIn("Duzeltme Once/Sonra Kiyas Raporu", result.text)
        self.assertIn("Duzeltildi Ama Toparlanmadi URL Listesi", result.text)

    def test_scan_flow_filters_assets(self) -> None:
        original_discover = app_main.discover_site_urls
        original_audit = app_main.run_quick_audit
        original_robots = app_main.check_robots_and_sitemap
        try:
            app_main.discover_site_urls = lambda site_url, limit=200: [
                "https://demo.com/",
                "https://demo.com/products/a",
            ]
            app_main.run_quick_audit = lambda urls: [
                {"url": urls[0], "status_code": "200", "severity": "info", "issues": "ok", "canonical": "", "final_url": urls[0]},
                {"url": urls[1], "status_code": "404", "severity": "critical", "issues": "404", "canonical": "", "final_url": urls[1]},
            ]
            app_main.check_robots_and_sitemap = lambda site_url: []

            start = self.client.post(
                "/analyze/start",
                data={"crawl_site": "true", "site_url": "demo.com", "run_audit": "true"},
            )
            self.assertEqual(start.status_code, 200)
            job_id = start.json()["job_id"]

            status = None
            for _ in range(50):
                status_resp = self.client.get(f"/analyze/status/{job_id}")
                self.assertEqual(status_resp.status_code, 200)
                status_data = status_resp.json()
                status = status_data["status"]
                if status in {"done", "error", "cancelled"}:
                    break
                time.sleep(0.1)

            self.assertEqual(status, "done")
            result = self.client.get(f"/result/{job_id}")
            self.assertEqual(result.status_code, 200)
            self.assertIn("Site tarama modu ozeti.", result.text)
            self.assertIn("Teknik Audit Sonucu", result.text)
        finally:
            app_main.discover_site_urls = original_discover
            app_main.run_quick_audit = original_audit
            app_main.check_robots_and_sitemap = original_robots


if __name__ == "__main__":
    unittest.main()
