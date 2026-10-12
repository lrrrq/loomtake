"""Real package extraction, gallery preview and adoption retain both row sources."""
import json
import os
from pathlib import Path
import sqlite3
import shutil
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import expect, sync_playwright
from tests import test_mvp_v2_single_shot_library_journey as gallery
from tests import test_mvp_v2_formal_zero_image_browser_journeys as zero


class MultiSourceLibraryBrowserTests(unittest.TestCase):
    def test_one_content_two_rows_preview_adoption_and_reload_preserve_sources(self):
        self._adopt_sources(include_row_peer=False)

    def test_two_distinct_images_from_one_row_adopt_together_and_keep_sources(self):
        self._adopt_sources(include_row_peer=True)

    def _adopt_sources(self, *, include_row_peer):
        root = Path(tempfile.mkdtemp(prefix='multi-source-', dir=os.environ.get('MPLUS_V3_EVIDENCE_ROOT') or None))
        if not os.environ.get('MPLUS_V3_EVIDENCE_ROOT'):
            self.addCleanup(shutil.rmtree, root)
        server = gallery._GalleryServer(root)
        evidence = {'status': 'started'}
        try:
            with patch.dict(os.environ, {'MPLUS_SINGLE_SHOT_MULTI_SOURCE': '1'}):
                server.start()
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True)
                page = browser.new_page()
                helper = gallery.SingleShotLibraryBrowserJourneyTests('runTest')
                project = helper._create_project(page, server.base_url)
                zero.FormalZeroImageBrowserJourneyTests('runTest')._confirm_zero_image_brief(page, server.base_url, root, project)
                page.goto(server.base_url + '/projects/' + project + '/materials')
                with page.expect_response(lambda r: '/images/default-candidates?' in r.url) as initial:
                    page.locator('[data-load-default-images]').click()
                self.assertEqual(initial.value.status, 200)
                panel = page.locator('[data-default-image-panel]')
                if initial.value.json().get('view') != 'library':
                    with page.expect_response(lambda r: '/images/default-candidates?' in r.url
                            and parse_qs(urlparse(r.url).query).get('view') == ['library']):
                        panel.locator('[data-library-view="library"]').click()
                panel.locator('[data-library-search]').fill('匿名图库29')
                with page.expect_response(lambda r: '/images/default-candidates?' in r.url
                        and parse_qs(urlparse(r.url).query).get('query') == ['匿名图库29']) as loaded:
                    panel.locator('[data-library-search-form] button[type="submit"]').click()
                model = loaded.value.json()
                candidates = model['library_candidates']
                self.assertEqual(len(candidates), 1, candidates)
                item = candidates[0]
                evidence['candidate'] = item
                self.assertEqual({s['row'] for s in item['sources']}, {7, 8})
                card = panel.locator('.ui00-library-image-card')
                expect(card.locator('img')).to_be_visible()
                card.locator('summary').first.click()
                expect(card).to_contain_text('第 7 行')
                expect(card).to_contain_text('第 8 行')
                card.locator('[data-default-library-candidate]').check()
                if include_row_peer:
                    panel.locator('[data-library-search]').fill('匿名图库28')
                    with page.expect_response(lambda r: '/images/default-candidates?' in r.url
                            and parse_qs(urlparse(r.url).query).get('query') == ['匿名图库28']) as peer_loaded:
                        panel.locator('[data-library-search-form] button[type="submit"]').click()
                    peer = peer_loaded.value.json()['library_candidates'][0]
                    self.assertNotEqual(peer['content_sha256'], item['content_sha256'])
                    self.assertEqual({source['row'] for source in peer['sources']}, {7})
                    panel.locator('[data-default-library-candidate]').check()
                    evidence['same_row_peer'] = peer

                with page.expect_response(lambda r: r.request.method == 'POST' and '/images/default-proposals?' in r.url) as proposed:
                    panel.locator('[data-propose-default-images]').click()
                self.assertEqual(proposed.value.status, 200, proposed.value.text())
                with page.expect_response(lambda r: r.request.method == 'POST' and '/api/agent/tasks/' in r.url and r.url.endswith('/confirm')) as adopted:
                    panel.locator('[data-confirm-default-images]').click()
                self.assertEqual(adopted.value.status, 200, adopted.value.text())
                response = adopted.value.json()['response']
                evidence['adoption'] = response
                selected = response['adopted_selections']
                self.assertEqual(len(selected), 2 if include_row_peer else 1)
                image = next(row['image_key'] for row in selected if row['content_sha256'] == item['content_sha256'])
                if include_row_peer:
                    peer_image = next(row['image_key'] for row in selected if row['content_sha256'] == peer['content_sha256'])
                    self.assertNotEqual(image, peer_image)
                    with sqlite3.connect(root/'workspace.sqlite3') as connection:
                        peer_links = connection.execute('SELECT content_sha256, source_sheet, source_row FROM project_image_package_source_links WHERE project_image_id=?',
                            (peer_image,)).fetchall()
                    self.assertEqual(peer_links, [(peer['content_sha256'], '模版', 7)])
                    evidence['peer_source_links'] = peer_links
                def links():
                    with sqlite3.connect(root/'workspace.sqlite3') as c:
                        return c.execute('SELECT media_id, content_sha256, source_file, source_sheet, source_row FROM project_image_package_source_links WHERE project_image_id=? ORDER BY source_row', (image,)).fetchall()
                before = links()
                self.assertEqual(len(before), 2)
                self.assertEqual({row[4] for row in before}, {7, 8})
                self.assertEqual({row[1] for row in before}, {item['content_sha256']})
                page.reload()
                adopted_card = page.locator(f'[data-formal-image-key="{image}"]')
                expect(adopted_card).to_be_visible()
                adopted_card.locator('.ui00-image-sources summary').click()
                expect(adopted_card.locator('.ui00-image-sources')).to_contain_text('第 7 行')
                expect(adopted_card.locator('.ui00-image-sources')).to_contain_text('第 8 行')
                if include_row_peer:
                    peer_card = page.locator(f'[data-formal-image-key="{peer_image}"]')
                    expect(peer_card).to_be_visible()
                    peer_card.locator('.ui00-image-sources summary').click()
                    expect(peer_card.locator('.ui00-image-sources')).to_contain_text('第 7 行')
                self.assertEqual(links(), before)
                evidence.update(status='passed', project_id=project, source_links=before)
                page.screenshot(path=str(root/'adopted-multi-source.png'), full_page=True)
                browser.close()
        except Exception as error:
            evidence.update(status='failed', error=str(error))
            raise
        finally:
            server.stop()
            (root/'result.json').write_text(json.dumps(evidence, ensure_ascii=False, indent=2)+'\n')
