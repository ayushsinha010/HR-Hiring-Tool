import importlib.util
import io
import unittest

import pymupdf

from . import _testenv as env

import app as hr_app
import db
import font_installer
import branded_report
import report_generator
import scorer
import timeline

HEADINGS_IN_ORDER = [
    "Overall Hiring Report", "Positions by Status", "Overall Hiring Funnel", "Activity Heatmap",
    "Full Funnel by Position", "Hiring Timeline and Buffer Time",
]


def pdf_pages(data):
    if hasattr(data, "getvalue"):
        data = data.getvalue()
    doc = pymupdf.open(stream=data, filetype="pdf")
    return doc, [p.get_text() for p in doc]


def seed_many():
    """Two positions, one with enough candidates that the candidate table must run over several pages."""
    p1 = env.add_position("Alpha Role", created_at="2026-09-01 12:00:00")
    p2 = env.add_position("Beta Role", created_at="2026-09-20 12:00:00")
    ids = []
    for i in range(70):
        cid = env.add_candidate(p1, f"Candidate Number {i:02d} Alpha", created_at="2026-09-03 12:00:00")
        ids.append(cid)
    env.add_candidate(p2, "Unique Beta Person", created_at="2026-09-21 12:00:00")
    db.update_stage(ids[0], "reached", True)
    iid = db.schedule_interview(ids[0], "l1", scheduled_for="2026-10-05 10:00", interviewers="Jordan", scheduled_by=1)
    db.add_interview_feedback(iid, "Jordan", "Pass", 4, "fine", saved_by=1)
    db.schedule_interview(ids[1], "l1", scheduled_for="2026-10-04 10:00", scheduled_by=1)
    db.add_task("Task that must not appear", None, 1, 1)
    return p1, p2


class ReportlabReportTests(unittest.TestCase):
    def setUp(self):
        env.fresh_db()
        self.p1, self.p2 = seed_many()

    def test_overall_report_has_everything_in_dashboard_order_with_the_timeline_last(self):
        doc, pages = pdf_pages(report_generator.generate_overall_report())
        text = "\n".join(pages)
        positions = [text.index(h) for h in HEADINGS_IN_ORDER]
        self.assertEqual(positions, sorted(positions), "sections must follow the dashboard order, timeline last")
        # the new section is the final one: nothing after it is another dashboard section heading
        tail = text[text.rindex("Hiring Timeline and Buffer Time"):]
        for later in ("Full Funnel by Position", "Positions by Status", "Activity Heatmap"):
            self.assertNotIn(later, tail)
        for expected in ("By position", "Average days per stage", "By candidate (most delayed first)", "uploaded",
                         "Feedback", "Over target", "Total Positions", "Candidates in Funnel"):
            self.assertTrue(expected in text, f"missing from the PDF: {expected}")
        # the removed Tasks cards are not in the PDF either
        for gone in ("Open Tasks", "Completed Tasks", "Task that must not appear"):
            self.assertTrue(gone not in text, f"should not be in the PDF: {gone}")
        # every candidate is in the by-candidate table: nothing cut off across pages
        for i in range(70):
            self.assertTrue(f"Candidate Number {i:02d} Alpha" in text, f"candidate {i} missing from the PDF")

    def test_wide_tables_sit_on_landscape_pages_and_headers_repeat(self):
        doc, pages = pdf_pages(report_generator.generate_overall_report())
        landscape = [i for i, p in enumerate(doc) if p.rect.width > p.rect.height]
        self.assertTrue(landscape, "wide tables need landscape pages")
        self.assertEqual(landscape[-1], len(doc) - 1)                       # the timeline section is on the last pages
        header_pages = [i for i, t in enumerate(pages) if "Entered stage on" in t]
        self.assertGreaterEqual(len(header_pages), 2, "candidate table header repeats on every page it spans")

    def test_nothing_is_clipped_outside_the_page(self):
        doc, _ = pdf_pages(report_generator.generate_overall_report())
        for page in doc:
            for x0, y0, x1, y1, *_ in page.get_text("blocks"):
                self.assertGreaterEqual(x0, -0.5)
                self.assertLessEqual(x1, page.rect.width + 0.5, f"text runs off page {page.number + 1}")
                self.assertLessEqual(y1, page.rect.height + 0.5)

    def test_graphs_are_drawn(self):
        doc, _ = pdf_pages(report_generator.generate_overall_report())
        first = doc[0]
        self.assertGreater(len(first.get_drawings()), 50)                    # pie, bars, heatmap squares
        timeline_pages = [p for p in doc if "Average days per stage" in p.get_text()]
        self.assertTrue(timeline_pages and len(timeline_pages[0].get_drawings()) > 5)   # the average-days graph

    def test_scoped_overall_report_only_has_that_positions_candidates(self):
        _, pages = pdf_pages(report_generator.generate_overall_report(position_ids=[self.p2]))
        text = "\n".join(pages)
        self.assertTrue("Unique Beta Person" in text)
        self.assertTrue("Candidate Number 00 Alpha" not in text)

    def test_position_report_has_the_scoped_section_without_the_position_column(self):
        buf, title = report_generator.generate_position_report(self.p1)
        self.assertEqual(title, "Alpha Role")
        _, pages = pdf_pages(buf)
        text = "\n".join(pages)
        for expected in ("Hiring Timeline and Buffer Time", "By position", "Average days per stage",
                         "By candidate (most delayed first)", "Candidate Number 69 Alpha"):
            self.assertTrue(expected in text, f"missing from the PDF: {expected}")
        self.assertTrue("Unique Beta Person" not in text)
        story = report_generator._timeline_story(timeline.build_timeline([self.p1]), report_generator.LANDSCAPE_WIDTH, show_position_col=False)
        tables = [f for f in story if f.__class__.__name__ == "Table"]
        self.assertEqual(len(tables[-1]._cellvalues[0]), 12)                 # 13 columns minus Position
        self.assertEqual(len(tables[-1]._cellvalues[0]), len(tables[-1]._cellvalues[1]))
        full = report_generator._timeline_story(timeline.build_timeline(), report_generator.LANDSCAPE_WIDTH)
        self.assertEqual(len([f for f in full if f.__class__.__name__ == "Table"][-1]._cellvalues[0]), 13)


class ReportRouteTests(unittest.TestCase):
    def setUp(self):
        env.fresh_db()
        self.p1, self.p2 = seed_many()
        self.client = hr_app.app.test_client()
        self.client.post("/login", data={"username": "admin", "password": env.ADMIN_PASSWORD})

    def test_generate_report_downloads_include_the_new_section(self):
        for url in ("/reports/overall.pdf", f"/positions/{self.p1}/report.pdf"):
            r = self.client.get(url)
            self.assertEqual(r.status_code, 200, url)
            self.assertEqual(r.data[:4], b"%PDF")
            _, pages = pdf_pages(r.data)
            text = "\n".join(pages)
            self.assertTrue("Hiring Timeline and Buffer Time" in text, url)
            self.assertTrue("Open Tasks" not in text and "Completed Tasks" not in text)


BRANDED_READY = (
    branded_report.is_available() and importlib.util.find_spec("matplotlib") is not None and font_installer.is_installed()
)


@unittest.skipUnless(BRANDED_READY, "Template-report path needs the external template renderer (REPORT_TEMPLATE_DIR), matplotlib, LibreOffice and Open Sans")
class BrandedReportTests(unittest.TestCase):
    def setUp(self):
        env.fresh_db()
        self.p1, self.p2 = seed_many()

    def test_overall_pdf_has_all_sections_timeline_last_and_repeating_headers(self):
        doc, pages = pdf_pages(branded_report.generate_overall_report())
        text = "\n".join(pages)
        body = text[text.index("Summary", text.index("Table of Contents") + 30):]
        order = [body.index(h) for h in ("Positions by Status", "Overall Hiring Funnel", "Activity Heatmap", "Full Funnel by Position", "Hiring Timeline and Buffer Time")]
        self.assertEqual(order, sorted(order))
        for expected in ("By position", "Average days per stage", "By candidate (most delayed first)", "Elapsed days per candidate"):
            self.assertIn(expected, text)
        for gone in ("Open Tasks", "Completed Tasks", "Task that must not appear"):
            self.assertNotIn(gone, text)
        for i in range(70):
            self.assertIn(f"Candidate Number {i:02d} Alpha", text)
        self.assertGreaterEqual(len([t for t in pages if "Next interview" in t]), 2)    # header repeats across pages
        self.assertGreaterEqual(sum(len(p.get_images()) for p in doc), 6)                  # the graphs are embedded

    def test_position_pdf_has_scoped_section(self):
        data, title = branded_report.generate_position_report(self.p1)
        _, pages = pdf_pages(data)
        text = "\n".join(pages)
        self.assertIn("Hiring Timeline and Buffer Time", text)
        self.assertIn("Average days per stage", text)
        self.assertNotIn("Unique Beta Person", text)


if __name__ == "__main__":
    unittest.main()
